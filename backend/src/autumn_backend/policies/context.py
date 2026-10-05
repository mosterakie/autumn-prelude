"""来源与旧执行上下文的纯失效判定，不替代执行器的事务 fencing。"""

from dataclasses import replace

from autumn_backend.policies.access import (
    ALLOW,
    NOT_FOUND,
    owner_denial,
    private_resource,
    publication_live,
)
from autumn_backend.policies.actor import ActorContext
from autumn_backend.policies.decision import Decision, DenialCode
from autumn_backend.policies.facts import (
    ConversationMode,
    PolicyFacts,
    SourceFacts,
    SourceScope,
    WebSourceFacts,
)

CONTEXT_INVALIDATED = Decision(code=DenialCode.ACL_CONTEXT_INVALIDATED)


def source_decision(
    actor: ActorContext, facts: PolicyFacts, source: SourceFacts | WebSourceFacts
) -> Decision:
    """由调用方先校验 Run/引用归属；本函数只校验当前来源访问权限。"""
    if isinstance(source, WebSourceFacts):
        if source.owner_id != actor.user_id:
            return NOT_FOUND
        return owner_denial(actor, facts, conceal=True) or ALLOW
    resource, revision = source.resource, source.revision
    if (
        resource is None
        or revision is None
        or source.resource_id != resource.resource_id
        or source.resource_id != revision.resource_id
        or source.revision_id != revision.revision_id
        or source.acl_version != resource.acl_version
        or resource.is_archived
    ):
        return NOT_FOUND
    if source.scope is SourceScope.PUBLIC:
        if not publication_live(resource, facts):
            return NOT_FOUND
        publication = resource.publication
        assert publication is not None
        if (
            not publication.ai_enabled
            or publication.publication_id != source.publication_id
            or publication.revision_id != source.revision_id
        ):
            return NOT_FOUND
        return ALLOW
    # private chunk 即使资源后来公开也不能改称 public 继续使用。
    if facts.target is None or facts.target.mode is not ConversationMode.OWNER:
        return NOT_FOUND
    private_facts = replace(
        facts,
        target=None,
        resource=resource,
        requested_resource_id=resource.resource_id,
        source=None,
        context=None,
    )
    return private_resource(actor, private_facts)


def context_decision(actor: ActorContext, facts: PolicyFacts) -> Decision:
    context, target = facts.context, facts.target
    if (
        context is None
        or target is None
        or context.run_id != target.object_id
        or context.mode is not target.mode
        or not context.sources_complete
        or actor.scope_epoch != facts.current_scope_epoch
        or context.captured_scope_epoch != facts.current_scope_epoch
        or context.captured_generation != context.current_generation
    ):
        return CONTEXT_INVALIDATED
    for source in context.sources:
        decision = source_decision(actor, facts, source)
        if decision.code is DenialCode.STEP_UP_REQUIRED:
            return decision
        if not decision.allowed:
            return CONTEXT_INVALIDATED
    return ALLOW

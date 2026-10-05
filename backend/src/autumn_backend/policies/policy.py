"""evaluate(actor, facts) -> Decision；没有数据库、网络或隐式时钟读取。"""

from typing import assert_never

from autumn_backend.policies.access import (
    ALLOW,
    NOT_FOUND,
    authentication_denial,
    own_chat,
    owner_denial,
    private_resource,
    publication_live,
    target_owned,
    verified_denial,
)
from autumn_backend.policies.actor import ActorContext
from autumn_backend.policies.context import context_decision, source_decision
from autumn_backend.policies.decision import Decision, DenialCode
from autumn_backend.policies.facts import (
    ConversationMode,
    Operation,
    PolicyFacts,
    SearchMode,
    SourceFacts,
    TargetKind,
)


def _public_comment(facts: PolicyFacts) -> Decision:
    target = facts.target
    if (
        target is None
        or target.kind is not TargetKind.COMMENT
        or target.is_deleted
        or not target.comment_approved
        or (target.expires_at is not None and target.expires_at <= facts.now)
    ):
        return NOT_FOUND
    if target.resource_id is not None and not publication_live(facts.resource, facts):
        return NOT_FOUND
    return ALLOW


def _create_comment(actor: ActorContext, facts: PolicyFacts) -> Decision:
    denial = verified_denial(actor, facts)
    if denial is not None:
        return denial
    if facts.requested_resource_id is not None or facts.resource is not None:
        if not publication_live(facts.resource, facts):
            return NOT_FOUND
    parent = facts.target
    if facts.requested_parent_id is not None and (
        parent is None or parent.object_id != facts.requested_parent_id
    ):
        return NOT_FOUND
    if parent is not None:
        if (
            parent.kind is not TargetKind.COMMENT
            or parent.is_deleted
            or (parent.expires_at is not None and parent.expires_at <= facts.now)
            or parent.resource_id != facts.requested_resource_id
            or (not parent.comment_approved and parent.owner_id != actor.user_id)
        ):
            return NOT_FOUND
    return ALLOW


def _ai_in_conversation(actor: ActorContext, facts: PolicyFacts) -> Decision:
    decision = own_chat(actor, facts, TargetKind.CONVERSATION)
    if not decision.allowed:
        return decision
    denial = verified_denial(actor, facts)
    if denial is not None:
        return denial
    if facts.search_mode is SearchMode.WEB:
        denial = owner_denial(actor, facts)
        if denial is not None:
            return denial
    auth = facts.authentication
    assert auth is not None
    if auth.ai_cooldown_until is not None and auth.ai_cooldown_until > facts.now:
        return Decision(code=DenialCode.AI_COOLDOWN, retry_at=auth.ai_cooldown_until)
    return ALLOW


def _action(actor: ActorContext, facts: PolicyFacts) -> Decision:
    # 已到期 action 仍可查看真实状态。到期禁止执行属于 Service 的状态机前置条件。
    if not target_owned(actor, facts, TargetKind.ACTION, ignore_expiry=True):
        return NOT_FOUND
    denial = authentication_denial(actor, facts)
    if denial is not None:
        return denial
    assert facts.target is not None
    if facts.target.requires_step_up:
        return owner_denial(actor, facts, conceal=True) or ALLOW
    return ALLOW


def evaluate(actor: ActorContext, facts: PolicyFacts) -> Decision:
    match facts.operation:
        case Operation.READ_PUBLIC_RESOURCE | Operation.READ_PUBLIC_FILE:
            if not publication_live(facts.resource, facts):
                return NOT_FOUND
            assert facts.resource is not None and facts.resource.publication is not None
            if (
                facts.operation is Operation.READ_PUBLIC_FILE
                and not facts.resource.publication.raw_download_enabled
            ):
                return NOT_FOUND
            return ALLOW
        case Operation.READ_PUBLIC_COMMENT:
            return _public_comment(facts)
        case Operation.CREATE_COMMENT:
            return _create_comment(actor, facts)
        case Operation.REPORT_COMMENT:
            return verified_denial(actor, facts) or _public_comment(facts)
        case Operation.EDIT_COMMENT:
            if not target_owned(actor, facts, TargetKind.COMMENT):
                return NOT_FOUND
            return authentication_denial(actor, facts) or ALLOW
        case Operation.DELETE_COMMENT:
            if target_owned(actor, facts, TargetKind.COMMENT):
                return authentication_denial(actor, facts) or ALLOW
            comment = facts.target
            if (
                comment is None
                or comment.kind is not TargetKind.COMMENT
                or comment.is_deleted
                or (comment.expires_at is not None and comment.expires_at <= facts.now)
            ):
                return NOT_FOUND
            return owner_denial(actor, facts, conceal=True) or ALLOW
        case Operation.CREATE_CONVERSATION:
            denial = verified_denial(actor, facts)
            if denial is not None:
                return denial
            if facts.requested_mode is ConversationMode.OWNER:
                return owner_denial(actor, facts) or ALLOW
            if facts.requested_mode is ConversationMode.PUBLIC:
                return ALLOW
            return Decision(code=DenialCode.FORBIDDEN)
        case (
            Operation.READ_CONVERSATION
            | Operation.UPDATE_CONVERSATION
            | Operation.DELETE_CONVERSATION
        ):
            return own_chat(actor, facts, TargetKind.CONVERSATION)
        case Operation.ASK:
            return _ai_in_conversation(actor, facts)
        case Operation.READ_RUN | Operation.CANCEL_RUN:
            return own_chat(actor, facts, TargetKind.RUN)
        case Operation.RESUME_RUN | Operation.CONTINUE_RUN | Operation.EMIT_RUN_OUTPUT:
            decision = own_chat(actor, facts, TargetKind.RUN)
            if not decision.allowed:
                return decision
            denial = verified_denial(actor, facts)
            if denial is not None:
                return denial
            # 恢复必须检查旧上下文，但不重用新 ask 的冷却/额度/速率判断。
            return context_decision(actor, facts)
        case Operation.SEARCH_PUBLIC_KNOWLEDGE:
            denial = verified_denial(actor, facts)
            if denial is not None:
                return denial
            if facts.resource is not None or facts.requested_resource_id is not None:
                if not publication_live(facts.resource, facts):
                    return NOT_FOUND
                assert facts.resource is not None and facts.resource.publication is not None
                if not facts.resource.publication.ai_enabled:
                    return NOT_FOUND
            return ALLOW
        case Operation.SEARCH_PRIVATE_KNOWLEDGE:
            if facts.resource is not None or facts.requested_resource_id is not None:
                decision = private_resource(actor, facts)
                if not decision.allowed:
                    return decision
                assert facts.resource is not None
                if facts.resource.is_archived:
                    return NOT_FOUND
                return ALLOW
            return owner_denial(actor, facts) or ALLOW
        case (
            Operation.SEARCH_WEB
            | Operation.CREATE_RESOURCE
            | Operation.INGEST_RESOURCE
            | Operation.MANAGE_SETTINGS
            | Operation.MANAGE_MODERATION
            | Operation.READ_AUDIT
            | Operation.CREATE_MEMORY
        ):
            return owner_denial(actor, facts) or ALLOW
        case (
            Operation.READ_PRIVATE_RESOURCE
            | Operation.UPDATE_RESOURCE
            | Operation.PUBLISH_RESOURCE
            | Operation.REVOKE_PUBLICATION
        ):
            return private_resource(actor, facts)
        case Operation.READ_ACTION | Operation.EXECUTE_ACTION | Operation.CANCEL_ACTION:
            return _action(actor, facts)
        case Operation.READ_MEMORY | Operation.UPDATE_MEMORY | Operation.DELETE_MEMORY:
            if not target_owned(actor, facts, TargetKind.MEMORY):
                return NOT_FOUND
            return owner_denial(actor, facts, conceal=True) or ALLOW
        case Operation.READ_CITATION:
            decision = own_chat(actor, facts, TargetKind.CITATION)
            if not decision.allowed:
                return decision
            assert facts.target is not None
            if facts.source is None or facts.source.source_id != facts.target.object_id:
                return NOT_FOUND
            if isinstance(facts.source, SourceFacts):
                if facts.target.resource_id != facts.source.resource_id:
                    return NOT_FOUND
            return source_decision(actor, facts, facts.source)
        case _ as unsupported:
            # 新增操作必须同时定义规则；strict 类型检查禁止遗漏枚举分支。
            assert_never(unsupported)

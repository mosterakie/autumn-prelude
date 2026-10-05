"""evaluate(actor, facts) -> Decision；没有数据库、网络或隐式时钟读取。"""

from autumn_backend.policies.access import (
    ALLOW,
    NOT_FOUND,
    own_chat,
    private_resource,
    publication_live,
)
from autumn_backend.policies.actor import ActorContext
from autumn_backend.policies.decision import Decision, DenialCode
from autumn_backend.policies.facts import ConversationMode, Operation, PolicyFacts, TargetKind


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
        case Operation.READ_CONVERSATION:
            return own_chat(actor, facts, TargetKind.CONVERSATION)
        case Operation.READ_RUN:
            return own_chat(actor, facts, TargetKind.RUN)
        case Operation.READ_PRIVATE_RESOURCE:
            return private_resource(actor, facts)
        case Operation.READ_CITATION:
            decision = own_chat(actor, facts, TargetKind.CITATION)
            if not decision.allowed:
                return decision
            assert facts.target is not None
            resource = facts.resource
            if publication_live(resource, facts):
                assert resource is not None and resource.publication is not None
                if resource.publication.ai_enabled:
                    return ALLOW
            if facts.target.mode is ConversationMode.OWNER:
                return private_resource(actor, facts)
            return NOT_FOUND
        case _:
            # 未实现或不支持的操作默认拒绝，不能通过无 facts 获得权限。
            return Decision(code=DenialCode.FORBIDDEN)

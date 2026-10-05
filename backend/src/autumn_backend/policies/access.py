"""纯访问条件；所有时间均使用 PolicyFacts.now。"""

from autumn_backend.policies.actor import ActorContext, ActorRole
from autumn_backend.policies.decision import Decision, DenialCode
from autumn_backend.policies.facts import (
    AccountStatus,
    ConversationMode,
    PolicyFacts,
    ResourceFacts,
    TargetKind,
)

ALLOW = Decision()
NOT_FOUND = Decision(code=DenialCode.NOT_FOUND)


def authentication_denial(actor: ActorContext, facts: PolicyFacts) -> Decision | None:
    if actor.role is ActorRole.ANONYMOUS:
        return Decision(code=DenialCode.AUTH_REQUIRED)
    auth = facts.authentication
    if (
        auth is None
        or actor.user_id != auth.user_id
        or actor.auth_session_id != auth.session_id
        or auth.session_user_id != auth.user_id
        or actor.role is not auth.role
        or auth.user_deleted_at is not None
        or auth.status is AccountStatus.DISABLED
        or auth.revoked_at is not None
        or auth.session_auth_version != auth.user_auth_version
        or auth.idle_expires_at <= facts.now
        or auth.absolute_expires_at <= facts.now
    ):
        return Decision(code=DenialCode.SESSION_EXPIRED)
    return None


def verified_denial(actor: ActorContext, facts: PolicyFacts) -> Decision | None:
    denial = authentication_denial(actor, facts)
    if denial is not None:
        return denial
    auth = facts.authentication
    assert auth is not None
    if auth.status is not AccountStatus.ACTIVE or auth.verified_at is None:
        return Decision(code=DenialCode.EMAIL_UNVERIFIED)
    return None


def owner_denial(
    actor: ActorContext, facts: PolicyFacts, *, conceal: bool = False
) -> Decision | None:
    if actor.role is not ActorRole.OWNER and conceal:
        return NOT_FOUND
    denial = verified_denial(actor, facts)
    if denial is not None:
        return denial
    if actor.role is not ActorRole.OWNER:
        return Decision(code=DenialCode.FORBIDDEN)
    auth = facts.authentication
    assert auth is not None
    # 同时检查入口快照和当前记录，当前延长的授权须先重新构建 ActorContext。
    if (
        actor.step_up_expires_at is None
        or actor.step_up_expires_at <= facts.now
        or auth.step_up_expires_at is None
        or auth.step_up_expires_at <= facts.now
    ):
        return Decision(code=DenialCode.STEP_UP_REQUIRED)
    return None


def resource_live(resource: ResourceFacts | None, facts: PolicyFacts) -> bool:
    return (
        resource is not None
        and not resource.is_deleted
        and (resource.expires_at is None or resource.expires_at > facts.now)
    )


def publication_live(resource: ResourceFacts | None, facts: PolicyFacts) -> bool:
    if not resource_live(resource, facts) or resource is None or resource.is_archived:
        return False
    publication = resource.publication
    return publication is not None and publication.is_current and publication.revoked_at is None


def target_owned(actor: ActorContext, facts: PolicyFacts, kind: TargetKind) -> bool:
    target = facts.target
    return (
        target is not None
        and target.kind is kind
        and actor.user_id is not None
        and target.owner_id == actor.user_id
        and not target.is_deleted
        and (target.expires_at is None or target.expires_at > facts.now)
    )


def own_chat(actor: ActorContext, facts: PolicyFacts, kind: TargetKind) -> Decision:
    if not target_owned(actor, facts, kind):
        return NOT_FOUND
    denial = authentication_denial(actor, facts)
    if denial is not None:
        return denial
    assert facts.target is not None
    if facts.target.mode is ConversationMode.OWNER:
        return owner_denial(actor, facts, conceal=True) or ALLOW
    return ALLOW


def private_resource(actor: ActorContext, facts: PolicyFacts) -> Decision:
    resource = facts.resource
    if (
        resource is None
        or actor.user_id is None
        or resource.owner_id != actor.user_id
        or not resource_live(resource, facts)
    ):
        return NOT_FOUND
    return owner_denial(actor, facts, conceal=True) or ALLOW

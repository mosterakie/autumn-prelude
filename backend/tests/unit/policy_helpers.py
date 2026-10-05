"""纯值测试数据，无 Repository 或数据库 fixture。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from autumn_backend.policies.actor import ActorContext, ActorRole, Capability
from autumn_backend.policies.facts import (
    AccountStatus,
    AuthenticationFacts,
    ConversationMode,
    Operation,
    PolicyFacts,
    PublicationFacts,
    ResourceFacts,
    RevisionFacts,
    SourceFacts,
    SourceScope,
    TargetFacts,
    TargetKind,
)

NOW = datetime(2026, 10, 5, tzinfo=UTC)


def identity(
    role: ActorRole = ActorRole.MEMBER, *, step_up: bool = False
) -> tuple[ActorContext, AuthenticationFacts]:
    user_id, session_id = uuid4(), uuid4()
    step_up_expiry = NOW + timedelta(minutes=15) if step_up else None
    actor = ActorContext(
        user_id=user_id,
        role=role,
        auth_session_id=session_id,
        step_up_expires_at=step_up_expiry,
        capabilities=frozenset({Capability.READ_PUBLIC}),
        scope_epoch=3,
    )
    auth = AuthenticationFacts(
        user_id=user_id,
        session_id=session_id,
        session_user_id=user_id,
        role=role,
        status=AccountStatus.ACTIVE,
        user_auth_version=1,
        session_auth_version=1,
        idle_expires_at=NOW + timedelta(hours=1),
        absolute_expires_at=NOW + timedelta(days=1),
        verified_at=NOW - timedelta(days=2),
        step_up_expires_at=step_up_expiry,
    )
    return actor, auth


def anonymous() -> ActorContext:
    return ActorContext(
        user_id=None,
        role=ActorRole.ANONYMOUS,
        auth_session_id=None,
        step_up_expires_at=None,
        capabilities=frozenset({Capability.READ_PUBLIC}),
        scope_epoch=3,
    )


def resource(actor: ActorContext, *, published: bool = True) -> ResourceFacts:
    assert actor.user_id is not None
    resource_id, revision_id = uuid4(), uuid4()
    return ResourceFacts(
        resource_id=resource_id,
        owner_id=actor.user_id,
        current_revision_id=revision_id,
        acl_version=2,
        publication=(
            PublicationFacts(
                publication_id=uuid4(),
                resource_id=resource_id,
                revision_id=revision_id,
                is_current=True,
                ai_enabled=True,
                raw_download_enabled=False,
            )
            if published
            else None
        ),
    )


def target(
    actor: ActorContext,
    kind: TargetKind = TargetKind.CONVERSATION,
    mode: ConversationMode = ConversationMode.PUBLIC,
) -> TargetFacts:
    return TargetFacts(object_id=uuid4(), owner_id=actor.user_id, kind=kind, mode=mode)


def facts(
    operation: Operation,
    authentication: AuthenticationFacts | None = None,
    *,
    obj: TargetFacts | None = None,
    res: ResourceFacts | None = None,
) -> PolicyFacts:
    return PolicyFacts(
        operation=operation,
        now=NOW,
        current_scope_epoch=3,
        authentication=authentication,
        target=obj,
        resource=res,
    )


def source(res: ResourceFacts, *, private: bool = False) -> SourceFacts:
    revision_id = res.current_revision_id
    publication_id = None
    if not private:
        assert res.publication is not None
        revision_id = res.publication.revision_id
        publication_id = res.publication.publication_id
    return SourceFacts(
        source_id=uuid4(),
        scope=SourceScope.OWNER if private else SourceScope.PUBLIC,
        resource_id=res.resource_id,
        revision_id=revision_id,
        acl_version=res.acl_version,
        publication_id=publication_id,
        resource=res,
        revision=RevisionFacts(revision_id=revision_id, resource_id=res.resource_id),
    )

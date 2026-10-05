"""E1 基础验收：三个关键流程，真实 PostgreSQL，测试数据外层事务回滚。"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from autumn_backend.db.enums import (
    ActionAuthorizationKind,
    ActionStatus,
    JobStatus,
    ResourceKind,
    UserRole,
    UserStatus,
)
from autumn_backend.db.models import Action, AuditEvent, AuthSession, Job, Publication, User
from autumn_backend.db.session import UnitOfWork, UnitOfWorkFactory
from autumn_backend.errors import IdempotencyConflictError, OptimisticLockError
from autumn_backend.policies import ActorContext, ActorRole, DenialCode
from autumn_backend.repositories.audit import AuditEventRepository
from autumn_backend.repositories.resources import RevisionDraft
from autumn_backend.services.access import AuthorizationError
from autumn_backend.services.publication import PublicationService, PublishCommand, RevokeCommand

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class Case:
    uows: UnitOfWorkFactory
    actor: ActorContext
    command: PublishCommand
    slug: str
    epoch: int


@pytest.fixture
async def case(engine: AsyncEngine) -> AsyncIterator[Case]:
    async with engine.connect() as connection:
        async with connection.begin():
            uows = UnitOfWorkFactory(
                async_sessionmaker(
                    connection,
                    expire_on_commit=False,
                    autoflush=False,
                    join_transaction_mode="create_savepoint",
                )
            )
            async with uows() as uow:
                now = await uow.repositories.users.database_time()
                user = User(
                    email_normalized=f"e1-{uuid4().hex}@example.com",
                    password_hash="placeholder",
                    role=UserRole.OWNER,
                    status=UserStatus.ACTIVE,
                    verified_at=now,
                )
                uow.session.add(user)
                await uow.session.flush()
                session = AuthSession(
                    user_id=user.id,
                    token_hash=uuid4().hex,
                    auth_version=user.auth_version,
                    idle_expires_at=now + timedelta(hours=1),
                    absolute_expires_at=now + timedelta(days=1),
                    step_up_expires_at=now + timedelta(minutes=15),
                )
                uow.session.add(session)
                await uow.session.flush()
                resource = await uow.repositories.resources.create(
                    owner_id=user.id,
                    kind=ResourceKind.ARTICLE,
                    slug=f"e1-{uuid4().hex}",
                    draft=RevisionDraft(
                        title="标题", body_text="正文", private_note="不应公开的私人备注"
                    ),
                )
                assert resource.current_revision_id is not None
                actor = ActorContext(
                    user_id=user.id,
                    role=ActorRole.OWNER,
                    auth_session_id=session.id,
                    step_up_expires_at=session.step_up_expires_at,
                    capabilities=frozenset(),
                    scope_epoch=await uow.repositories.settings.get_acl_epoch(),
                )
                command = PublishCommand(
                    resource_id=resource.id,
                    revision_id=resource.current_revision_id,
                    expected_version=0,
                    expected_acl_version=0,
                    idempotency_key=uuid4().hex,
                    public_fields=frozenset({"title", "body_text"}),
                    ai_enabled=True,
                )
                value = Case(uows, actor, command, resource.slug, actor.scope_epoch)
            yield value
            await connection.rollback()


async def counts(uow: UnitOfWork, case: Case) -> tuple[int, int, int, int]:
    return (
        await uow.session.scalar(
            select(func.count()).select_from(Action).where(Action.actor_id == case.actor.user_id)
        ),
        await uow.session.scalar(
            select(func.count()).select_from(Job).where(Job.actor_id == case.actor.user_id)
        ),
        await uow.session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.actor_id == case.actor.user_id)
        ),
        await uow.session.scalar(
            select(func.count())
            .select_from(Publication)
            .where(Publication.resource_id == case.command.resource_id)
        ),
    )


async def test_publish_replay_and_revoke_commit_together(case: Case) -> None:
    service = PublicationService(case.uows)
    result = await service.publish_explicit(case.actor, case.command, request_id="e1-basic")
    assert result.is_current and result.public_url == f"/notes/{case.slug}"
    assert result.actual_public_fields == ("body", "title")
    assert (result.resource_version, result.acl_version, result.scope_epoch) == (
        0,
        1,
        case.epoch + 1,
    )
    assert result.index_job_status is JobStatus.QUEUED
    replay = await service.publish_explicit(
        case.actor, replace(case.command, public_fields=frozenset({"title", "body"}))
    )
    assert replay == result
    with pytest.raises(IdempotencyConflictError):
        await service.publish_explicit(
            case.actor, replace(case.command, public_fields=frozenset({"title"}))
        )
    async with case.uows() as uow:
        assert await counts(uow, case) == (1, 1, 1, 1)
        assert result.publication_id is not None and result.index_job_id is not None
        publication = await uow.repositories.publications.get_or_raise(result.publication_id)
        assert publication.public_body == "正文" and publication.public_note is None
        action = await uow.repositories.actions.get_or_raise(result.action_id)
        assert action.status is ActionStatus.SUCCEEDED
        assert action.authorization_kind is ActionAuthorizationKind.EXPLICIT_REQUEST
        assert not action.requires_confirmation
        job = await uow.repositories.jobs.get_or_raise(result.index_job_id)
        assert job.payload == {
            "scope": "public",
            "resource_id": str(case.command.resource_id),
            "publication_id": str(result.publication_id),
            "acl_version": 1,
            "scope_epoch": case.epoch + 1,
            "ai_enabled": True,
        }
    revoke = RevokeCommand(
        resource_id=case.command.resource_id,
        expected_version=0,
        expected_acl_version=1,
        idempotency_key=uuid4().hex,
    )
    revoked = await service.revoke_explicit(case.actor, revoke)
    assert revoked.changed and not revoked.is_current and revoked.public_url is None
    assert (revoked.acl_version, revoked.scope_epoch) == (2, case.epoch + 2)
    assert await service.revoke_explicit(case.actor, revoke) == revoked
    # 再次读原发布结果不会把已撤回的 URL 描述为仍在公开。
    old = await service.publish_explicit(case.actor, case.command)
    assert old.action_id == result.action_id and not old.is_current and old.public_url is None
    async with case.uows() as uow:
        assert await counts(uow, case) == (2, 2, 2, 1)
        assert await uow.repositories.publications.public_by_slug(case.slug) is None
        assert await uow.repositories.settings.get_acl_epoch() == case.epoch + 2


async def test_permission_and_versions_block_writes(case: Case) -> None:
    service = PublicationService(case.uows)
    with pytest.raises(AuthorizationError) as denied:
        await service.publish_explicit(replace(case.actor, step_up_expires_at=None), case.command)
    assert denied.value.decision.code is DenialCode.STEP_UP_REQUIRED
    with pytest.raises(OptimisticLockError):
        await service.publish_explicit(case.actor, replace(case.command, expected_acl_version=1))
    async with case.uows() as uow:
        assert await counts(uow, case) == (0, 0, 0, 0)
        assert await uow.repositories.settings.get_acl_epoch() == case.epoch


async def test_late_failure_rolls_back_publication_action_job_and_epoch(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failed_audit(self: AuditEventRepository, **kwargs: object) -> None:
        raise RuntimeError("simulated audit failure")

    # 仅注入故障触发回滚；此前资源/投影/Action/Job 均由真实数据库执行。
    monkeypatch.setattr(AuditEventRepository, "record", failed_audit)
    with pytest.raises(RuntimeError, match="simulated audit failure"):
        await PublicationService(case.uows).publish_explicit(case.actor, case.command)
    async with case.uows() as uow:
        assert await counts(uow, case) == (0, 0, 0, 0)
        resource = await uow.repositories.resources.get_or_raise(case.command.resource_id)
        assert (resource.version, resource.acl_version) == (0, 0)
        assert await uow.repositories.settings.get_acl_epoch() == case.epoch

"""E2 四项基础验收；真实 PostgreSQL，外层事务回滚，不扩展并发矩阵。"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from autumn_backend.db.enums import ConversationMode, ResourceKind, RunStatus, UserRole, UserStatus
from autumn_backend.db.models import (
    AuthSession,
    Job,
    Message,
    QuotaBucket,
    QuotaReservation,
    RateLimitBucket,
    Run,
    Setting,
    User,
)
from autumn_backend.db.session import UnitOfWork, UnitOfWorkFactory
from autumn_backend.errors import (
    ConcurrencyLimitError,
    IdempotencyConflictError,
    NotFoundError,
    QuotaExceededError,
    RateLimitedError,
)
from autumn_backend.policies import ActorContext, ActorRole, DenialCode
from autumn_backend.policies.facts import SearchMode
from autumn_backend.repositories.constraints import ActiveRunConflictError
from autumn_backend.repositories.jobs import JobRepository, JobSpec
from autumn_backend.repositories.publications import PublicationProjection
from autumn_backend.repositories.resources import RevisionDraft
from autumn_backend.repositories.runs import RunRepository
from autumn_backend.services.access import AuthorizationError
from autumn_backend.services.ai_limits import daily_window
from autumn_backend.services.runs import AcceptRunCommand, RunService

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class Case:
    uows: UnitOfWorkFactory
    actor: ActorContext
    owner: ActorContext
    conversations: tuple[UUID, ...]
    owner_conversation: UUID
    public_resource: UUID
    private_resource: UUID

    def command(self, index: int = 0, **changes: object) -> AcceptRunCommand:
        return replace(
            AcceptRunCommand(
                conversation_id=self.conversations[index],
                client_message_id=uuid4(),
                idempotency_key=uuid4().hex,
                message="问题\r\n第二行",
            ),
            **changes,
        )


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
                actors = []
                for role in (UserRole.MEMBER, UserRole.OWNER):
                    user = User(
                        email_normalized=f"e2-{uuid4().hex}@example.com",
                        password_hash="placeholder",
                        role=role,
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
                        step_up_expires_at=now + timedelta(minutes=15)
                        if role is UserRole.OWNER
                        else None,
                    )
                    uow.session.add(session)
                    await uow.session.flush()
                    actors.append(
                        ActorContext(
                            user_id=user.id,
                            role=ActorRole(role.value),
                            auth_session_id=session.id,
                            step_up_expires_at=session.step_up_expires_at,
                            capabilities=frozenset(),
                            scope_epoch=0,
                        )
                    )
                member, owner = actors
                assert member.user_id is not None and owner.user_id is not None
                conversations = tuple(
                    [
                        (
                            await uow.repositories.conversations.create(
                                user_id=member.user_id,
                                mode=ConversationMode.PUBLIC,
                            )
                        ).id
                        for _ in range(3)
                    ]
                )
                owner_conversation = await uow.repositories.conversations.create(
                    user_id=owner.user_id,
                    mode=ConversationMode.OWNER,
                )
                public = await uow.repositories.resources.create(
                    owner_id=owner.user_id,
                    kind=ResourceKind.ARTICLE,
                    slug=f"e2-public-{uuid4().hex}",
                    draft=RevisionDraft(title="公开资料", body_text="可检索正文"),
                )
                assert public.current_revision_id is not None
                await uow.repositories.publications.publish_under_resource_lock(
                    resource_id=public.id,
                    revision_id=public.current_revision_id,
                    expected_version=0,
                    expected_acl_version=0,
                    published_by=owner.user_id,
                    projection=PublicationProjection(
                        fields=frozenset({"title", "body"}), ai_enabled=True
                    ),
                )
                private = await uow.repositories.resources.create(
                    owner_id=owner.user_id,
                    kind=ResourceKind.ARTICLE,
                    slug=f"e2-private-{uuid4().hex}",
                    draft=RevisionDraft(title="私人资料", body_text="私人正文"),
                )
                value = Case(
                    uows, member, owner, conversations, owner_conversation.id, public.id, private.id
                )
            yield value
            await connection.rollback()


async def set_limits(
    uow: UnitOfWork, *, daily: int = 10, minute: int = 3, concurrency: int = 1
) -> None:
    values = {
        "daily_limit": daily,
        "cooldown_hours": 24,
        "per_minute": minute,
        "concurrency": concurrency,
    }
    setting = await uow.repositories.settings.get("ai_limits")
    if setting is None:
        uow.session.add(Setting(key="ai_limits", value=values))
        await uow.session.flush()
    else:
        await uow.session.execute(
            update(Setting)
            .where(Setting.key == "ai_limits")
            .values(
                value=values,
                version=Setting.version + 1,
            )
        )


async def counts(uow: UnitOfWork, case: Case) -> tuple[int, ...]:
    statements = (
        select(func.count()).select_from(Run).where(Run.user_id == case.actor.user_id),
        select(func.count())
        .select_from(Message)
        .where(Message.conversation_id.in_(case.conversations)),
        select(func.count())
        .select_from(QuotaReservation)
        .where(QuotaReservation.user_id == case.actor.user_id),
        select(func.count()).select_from(Job).where(Job.actor_id == case.actor.user_id),
    )
    return tuple(
        [int((await uow.session.execute(statement)).scalar_one()) for statement in statements]
    )


async def test_accept_and_replay_do_not_allocate_again(case: Case) -> None:
    service = RunService(case.uows)
    command = case.command(resource_ids=(case.public_resource, case.public_resource))
    result = await service.accept_run(case.actor, command)
    assert result.status is RunStatus.QUEUED
    assert (result.quota.used, result.quota.reserved, result.quota.remaining) == (0, 1, 9)
    assert (result.quota.window_start, result.quota.window_end) == daily_window(
        result.quota.server_time, "Asia/Shanghai"
    )
    before = datetime(2026, 10, 5, 15, 59, 59, tzinfo=UTC)
    assert (
        daily_window(before, "Asia/Shanghai")[1]
        == daily_window(before + timedelta(seconds=1), "Asia/Shanghai")[0]
    )
    # 同消息的新 key 不可复用旧输入；正文/身份不同的同 key 返回稳定冲突。
    with pytest.raises(IdempotencyConflictError):
        await service.accept_run(case.actor, replace(command, idempotency_key=uuid4().hex))
    with pytest.raises(IdempotencyConflictError):
        await service.accept_run(case.actor, replace(command, message="另一问题"))
    async with case.uows() as uow:
        now = await uow.repositories.users.database_time()
        await uow.session.execute(
            update(User)
            .where(User.id == case.actor.user_id)
            .values(ai_cooldown_until=now + timedelta(days=1))
        )
        await set_limits(uow, daily=0, minute=0, concurrency=0)
    replay = await service.accept_run(
        case.actor, replace(command, message=command.body, resource_ids=(case.public_resource,))
    )
    assert (replay.run_id, replay.input_message_id, replay.quota.window_start) == (
        result.run_id,
        result.input_message_id,
        result.quota.window_start,
    )
    # 重试展示原受理策略/原桶，不重新读取新配置以决定是否受理。
    assert replay.quota.daily_limit == 10
    async with case.uows() as uow:
        assert await counts(uow, case) == (1, 1, 1, 1)
        message = await uow.session.get(Message, result.input_message_id)
        assert message is not None and message.body_text == command.body and message.seq == 1
        run = await uow.repositories.runs.get_or_raise(result.run_id)
        assert run.config_snapshot["resource_ids"] == [str(case.public_resource)]
        assert not run.config_snapshot["web_enabled"] and "message" not in run.config_snapshot
        job = (
            await uow.session.execute(select(Job).where(Job.run_id == result.run_id))
        ).scalar_one()
        assert job.kind == "run.dispatch" and job.payload == {"run_id": str(result.run_id)}
        rate = (await uow.session.execute(select(RateLimitBucket))).scalar_one()
        assert rate.hits == 1 and str(case.actor.user_id) not in rate.scope_hash
        events = await uow.repositories.run_events.after(result.run_id, 0)
        assert len(events) == 1 and events[0].payload == {"status": "queued"}


async def test_current_permissions_cooldown_and_owner_search(case: Case) -> None:
    service = RunService(case.uows)
    with pytest.raises(NotFoundError):
        await service.accept_run(case.actor, case.command(conversation_id=case.owner_conversation))
    with pytest.raises(AuthorizationError) as web_denied:
        await service.accept_run(case.actor, case.command(search_mode=SearchMode.WEB))
    assert web_denied.value.decision.code is DenialCode.FORBIDDEN
    with pytest.raises(NotFoundError):
        await service.accept_run(case.actor, case.command(resource_ids=(case.private_resource,)))
    async with case.uows() as uow:
        now = await uow.repositories.users.database_time()
        await uow.session.execute(
            update(User)
            .where(User.id == case.actor.user_id)
            .values(ai_cooldown_until=now + timedelta(days=1))
        )
    with pytest.raises(AuthorizationError) as cooled:
        await service.accept_run(case.actor, case.command())
    assert cooled.value.decision.code is DenialCode.AI_COOLDOWN
    async with case.uows() as uow:
        assert await counts(uow, case) == (0, 0, 0, 0)
        await uow.session.execute(
            update(User).where(User.id == case.actor.user_id).values(ai_cooldown_until=None)
        )
    command = case.command(resource_ids=(case.public_resource,), search_mode=SearchMode.AUTO)
    await service.accept_run(case.actor, command)
    async with case.uows() as uow:
        await uow.repositories.publications.revoke(case.public_resource, expected_acl_version=1)
    with pytest.raises(NotFoundError):
        await service.accept_run(case.actor, command)
    owner_command = case.command(
        conversation_id=case.owner_conversation,
        resource_ids=(case.private_resource,),
        search_mode=SearchMode.WEB,
    )
    with pytest.raises(AuthorizationError) as step_denied:
        await service.accept_run(replace(case.owner, step_up_expires_at=None), owner_command)
    assert step_denied.value.decision.code is DenialCode.STEP_UP_REQUIRED
    owner_result = await service.accept_run(case.owner, owner_command)
    assert owner_result.quota.daily_limit == 100
    async with case.uows() as uow:
        assert await counts(uow, case) == (1, 1, 1, 1)
        run = await uow.repositories.runs.get_or_raise(owner_result.run_id)
        assert run.config_snapshot["web_enabled"] and run.config_snapshot["mode"] == "owner"


async def test_concurrency_waiting_rate_and_quota_boundaries(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RunService(case.uows)
    async with case.uows() as uow:
        await set_limits(uow, daily=1, minute=1)
        now = await uow.repositories.users.database_time()

    async def fixed_clock(self: RunRepository) -> datetime:
        return now

    # 使用一次真实 DB 时间固定本用例，避免恰好跨分钟导致速率边界测试偶发失败。
    monkeypatch.setattr(RunRepository, "database_time", fixed_clock)
    first = await service.accept_run(case.actor, case.command())
    with pytest.raises(ConcurrencyLimitError):
        await service.accept_run(case.actor, case.command(1))
    async with case.uows() as uow:
        # 仅构造持久状态边界；业务等待/恢复转换由 G/H 后续实现。
        await uow.session.execute(
            update(Run).where(Run.id == first.run_id).values(status=RunStatus.WAITING_AUTH)
        )
    with pytest.raises(ActiveRunConflictError):
        await service.accept_run(case.actor, case.command())
    with pytest.raises(RateLimitedError):
        await service.accept_run(case.actor, case.command(1))
    async with case.uows() as uow:
        await set_limits(uow, daily=1, minute=10)
    with pytest.raises(QuotaExceededError):
        await service.accept_run(case.actor, case.command(1))
    async with case.uows() as uow:
        assert await counts(uow, case) == (1, 1, 1, 1)
        rate_hits = (await uow.session.execute(select(func.sum(RateLimitBucket.hits)))).scalar_one()
        assert rate_hits == 1
        await set_limits(uow, daily=2, minute=10)
    second = await service.accept_run(case.actor, case.command(1))
    assert second.quota.reserved == 2 and second.quota.remaining == 0


async def test_late_enqueue_failure_rolls_back_all_allocations(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failed_enqueue(self: JobRepository, spec: JobSpec) -> None:
        raise RuntimeError("simulated queue failure")

    monkeypatch.setattr(JobRepository, "enqueue", failed_enqueue)
    with pytest.raises(RuntimeError, match="simulated queue failure"):
        await RunService(case.uows).accept_run(case.actor, case.command())
    async with case.uows() as uow:
        assert await counts(uow, case) == (0, 0, 0, 0)
        assert (
            await uow.session.execute(
                select(func.count())
                .select_from(QuotaBucket)
                .where(QuotaBucket.user_id == case.actor.user_id)
            )
        ).scalar_one() == 0
        assert (
            await uow.session.execute(select(func.count()).select_from(RateLimitBucket))
        ).scalar_one() == 0
        conversation = await uow.repositories.conversations.get_or_raise(case.conversations[0])
        assert conversation.next_message_seq == 1

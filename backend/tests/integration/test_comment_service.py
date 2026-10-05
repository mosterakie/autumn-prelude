"""E3 四项基础验收；真实 PostgreSQL，测试数据外层事务回滚。"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from autumn_backend.db.enums import CommentStatus, ResourceKind, UserRole, UserStatus
from autumn_backend.db.models import AuditEvent, AuthSession, Comment, User
from autumn_backend.db.session import UnitOfWork, UnitOfWorkFactory
from autumn_backend.errors import ConflictError, IdempotencyConflictError, NotFoundError
from autumn_backend.policies import ActorContext, ActorRole, DenialCode
from autumn_backend.repositories.audit import AuditEventRepository
from autumn_backend.repositories.publications import PublicationProjection
from autumn_backend.repositories.resources import RevisionDraft
from autumn_backend.services.access import AuthorizationError
from autumn_backend.services.comments import CommentService, CreateCommentCommand

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class Case:
    uows: UnitOfWorkFactory
    actor: ActorContext
    other: ActorContext
    public_resource: UUID
    private_resource: UUID


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
                        email_normalized=f"e3-{uuid4().hex}@example.com",
                        password_hash="placeholder",
                        display_name="拾秋",
                        role=role,
                        status=UserStatus.ACTIVE,
                        verified_at=now,
                        ai_cooldown_until=now + timedelta(hours=24),
                    )
                    uow.session.add(user)
                    await uow.session.flush()
                    session = AuthSession(
                        user_id=user.id,
                        token_hash=uuid4().hex,
                        auth_version=user.auth_version,
                        idle_expires_at=now + timedelta(hours=1),
                        absolute_expires_at=now + timedelta(days=1),
                    )
                    uow.session.add(session)
                    await uow.session.flush()
                    actors.append(
                        ActorContext(
                            user_id=user.id,
                            role=ActorRole(role.value),
                            auth_session_id=session.id,
                            step_up_expires_at=None,
                            capabilities=frozenset(),
                            scope_epoch=0,
                        )
                    )
                actor, other = actors
                assert other.user_id is not None
                public = await uow.repositories.resources.create(
                    owner_id=other.user_id,
                    kind=ResourceKind.ARTICLE,
                    slug=f"e3-public-{uuid4().hex}",
                    draft=RevisionDraft(title="公开文章", body_text="公开正文"),
                )
                assert public.current_revision_id is not None
                await uow.repositories.publications.publish_under_resource_lock(
                    resource_id=public.id,
                    revision_id=public.current_revision_id,
                    expected_version=0,
                    expected_acl_version=0,
                    published_by=other.user_id,
                    projection=PublicationProjection(
                        fields=frozenset({"title", "body"}), ai_enabled=False
                    ),
                )
                private = await uow.repositories.resources.create(
                    owner_id=other.user_id,
                    kind=ResourceKind.ARTICLE,
                    slug=f"e3-private-{uuid4().hex}",
                    draft=RevisionDraft(title="私人文章", body_text="私人正文"),
                )
                value = Case(uows, actor, other, public.id, private.id)
            yield value
            await connection.rollback()


async def counts(uow: UnitOfWork, case: Case) -> tuple[int, int]:
    return (
        (
            await uow.session.execute(
                select(func.count())
                .select_from(Comment)
                .where(Comment.author_id == case.actor.user_id)
            )
        ).scalar_one(),
        (
            await uow.session.execute(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.actor_id == case.actor.user_id)
            )
        ).scalar_one(),
    )


async def test_creation_replay_and_current_author_result(case: Case) -> None:
    service = CommentService(case.uows)
    command = CreateCommentCommand(client_id=uuid4(), body=" 秋序\r\n你好 ")
    result = await service.create_comment(case.actor, command, request_id="e3-basic")
    assert result.body == " 秋序\n你好 " and result.author_display_name == "拾秋"
    assert (
        result.status is CommentStatus.PENDING
        and result.resource_id is None
        and result.version == 0
    )
    assert await service.create_comment(case.actor, replace(command, body=result.body)) == result
    with pytest.raises(IdempotencyConflictError):
        await service.create_comment(case.actor, replace(command, body="不同内容"))
    async with case.uows() as uow:
        assert await counts(uow, case) == (1, 1)
        audit = (
            await uow.session.execute(
                select(AuditEvent).where(AuditEvent.actor_id == case.actor.user_id)
            )
        ).scalar_one()
        assert audit.metadata_json == {"comment_id": str(result.id), "after_status": "pending"}
        assert audit.before_version is None and audit.after_version is None
        # 构造后续编辑/审核后的持久结果；本节点不实现编辑和审核入口。
        await uow.session.execute(
            update(Comment)
            .where(Comment.id == result.id)
            .values(
                body="后续修改的正文",
                status=CommentStatus.APPROVED,
                version=1,
            )
        )
    replay = await service.create_comment(case.actor, command)
    assert replay.id == result.id and replay.body == "后续修改的正文"
    assert replay.status is CommentStatus.APPROVED and replay.version == 1
    async with case.uows() as uow:
        assert await counts(uow, case) == (1, 1)


async def test_current_identity_and_public_resource_permissions(case: Case) -> None:
    service = CommentService(case.uows)
    command = CreateCommentCommand(
        client_id=uuid4(), body="文章留言", resource_id=case.public_resource
    )
    anonymous = ActorContext(
        user_id=None,
        role=ActorRole.ANONYMOUS,
        auth_session_id=None,
        step_up_expires_at=None,
        capabilities=frozenset(),
        scope_epoch=0,
    )
    with pytest.raises(AuthorizationError) as denied:
        await service.create_comment(anonymous, command)
    assert denied.value.decision.code is DenialCode.AUTH_REQUIRED
    async with case.uows() as uow:
        await uow.session.execute(
            update(User)
            .where(User.id == case.actor.user_id)
            .values(
                verified_at=None,
                status=UserStatus.PENDING_VERIFICATION,
            )
        )
    with pytest.raises(AuthorizationError) as unverified:
        await service.create_comment(case.actor, command)
    assert unverified.value.decision.code is DenialCode.EMAIL_UNVERIFIED
    async with case.uows() as uow:
        assert await counts(uow, case) == (0, 0)
        await uow.session.execute(
            update(User)
            .where(User.id == case.actor.user_id)
            .values(
                verified_at=await uow.repositories.users.database_time(),
                status=UserStatus.ACTIVE,
            )
        )
    with pytest.raises(NotFoundError):
        await service.create_comment(
            case.actor, replace(command, resource_id=case.private_resource)
        )
    # AI 冷却仍有效、文章未开启 AI，均不限制已验证用户留言。
    result = await service.create_comment(case.actor, command)
    assert result.resource_id == case.public_resource and result.status is CommentStatus.PENDING
    async with case.uows() as uow:
        await uow.repositories.publications.revoke(case.public_resource, expected_acl_version=1)
    with pytest.raises(NotFoundError):
        await service.create_comment(case.actor, command)
    async with case.uows() as uow:
        assert await counts(uow, case) == (1, 1)


async def test_reply_visibility_level_and_deleted_replay(case: Case) -> None:
    service = CommentService(case.uows)
    root = await service.create_comment(
        case.actor, CreateCommentCommand(client_id=uuid4(), body="本人待审核留言")
    )
    other_root = await service.create_comment(
        case.other, CreateCommentCommand(client_id=uuid4(), body="他人待审核留言")
    )
    with pytest.raises(NotFoundError):
        await service.create_comment(
            case.actor,
            CreateCommentCommand(client_id=uuid4(), body="不可枚举", parent_id=other_root.id),
        )
    with pytest.raises(NotFoundError):
        await service.create_comment(
            case.actor,
            CreateCommentCommand(
                client_id=uuid4(),
                body="范围不匹配",
                parent_id=root.id,
                resource_id=case.public_resource,
            ),
        )
    command = CreateCommentCommand(client_id=uuid4(), body="一级回复", parent_id=root.id)
    reply = await service.create_comment(case.actor, command)
    assert reply.parent_id == root.id and reply.status is CommentStatus.PENDING
    with pytest.raises(ConflictError):
        await service.create_comment(
            case.actor,
            CreateCommentCommand(client_id=uuid4(), body="不允许二级", parent_id=reply.id),
        )
    async with case.uows() as uow:
        await uow.session.execute(
            update(Comment).where(Comment.id == root.id).values(deleted_at=func.clock_timestamp())
        )
    with pytest.raises(NotFoundError):
        await service.create_comment(case.actor, replace(command, client_id=uuid4()))
    # 已创建回复的重试是读取本人结果，不重新执行父留言前置条件，也不创建新回复。
    assert await service.create_comment(case.actor, command) == reply
    async with case.uows() as uow:
        await uow.session.execute(
            update(Comment).where(Comment.id == reply.id).values(deleted_at=func.clock_timestamp())
        )
    with pytest.raises(NotFoundError):
        await service.create_comment(case.actor, command)
    async with case.uows() as uow:
        assert await counts(uow, case) == (2, 2)


async def test_late_audit_failure_rolls_back_comment(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failed_audit(self: AuditEventRepository, **kwargs: object) -> None:
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr(AuditEventRepository, "record", failed_audit)
    with pytest.raises(RuntimeError, match="simulated audit failure"):
        await CommentService(case.uows).create_comment(
            case.actor, CreateCommentCommand(client_id=uuid4(), body="回滚验证")
        )
    async with case.uows() as uow:
        assert await counts(uow, case) == (0, 0)

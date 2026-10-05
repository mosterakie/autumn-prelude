"""A 阶段补充验收的回归测试（在 28 表新契约下重写）。

来源：《秋序_实施顺序文档评审与 A 阶段验收》§5 的五项补充验收发现。
这些用例按**期望行为**编写：修复前应当失败，修复后必须通过。

- A-P1-1 发布序号是资源内唯一，不是全局唯一
- A-P1-2 可空幂等键仍必须去重
- A-P1-4 publication 的 revision 必须归属于同一 resource
- A-P2-5 会话轮换链的墓碑状态必须可清理

新契约带来的两处**契约替换**（不是删除用例，而是换掉载体）：

1. 旧的 ``publications.public_url`` 全局唯一约束已不存在——公开 URL 由
   ``resources.slug``（全局唯一）派生，因此"两个公开 URL 不会互相覆盖"这条
   不变量改由 ``uq_resources_slug`` 承担；
2. 旧的 ``auth_sessions.replaced_by_session_id`` / ``rotated_at`` 墓碑列已不存在——
   身份失效改由 ``revoked_at`` + ``users.auth_version`` 表达。相应的用例改为
   断言"旧列与旧 CHECK 都不存在，失效语义仍可表达"。

A-P1-3（生产配置拒绝开发默认密钥）是纯配置问题，见
``tests/unit/test_config_secrets.py``。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from autumn_backend.db.enums import (
    ActionAuthorizationKind,
    ActionStatus,
    ActionType,
    ConversationMode,
    ResourceKind,
    RunStatus,
    UserRole,
)
from autumn_backend.db.models import (
    Action,
    AuthSession,
    Comment,
    Publication,
    Resource,
    Run,
)

pytestmark = pytest.mark.integration

UTC = dt.UTC

#: 数据库里实际安装的"run 必须与会话同属一个用户"外键（列序 conversation_id, user_id）。
RUN_CONVERSATION_OWNER_FK = "fk_runs_conversation_id_user_id_conversations"


async def _expect_error(
    session: AsyncSession, message: str | None = None, *, statement: Any = None
) -> str:
    with pytest.raises((IntegrityError, DBAPIError, StatementError)) as excinfo:
        if statement is not None:
            await session.execute(statement)
        await session.flush()
    raw = str(excinfo.value)
    if message is not None:
        assert message in raw, raw
    return raw


class TestPublicationNumberIsResourceScoped:
    """A-P1-1：``publication_no`` 是"该资源第几次发布"，不是全局序号。"""

    async def test_same_publication_no_allowed_for_different_resources(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
        make_publication: Callable[..., Awaitable[Any]],
    ) -> None:
        """两篇文章各自发布第一版，序号都是 1，必须都能落库。"""
        user = make_user(email="pubno1@example.com")
        await session.flush()

        first = await make_resource(user.id)
        second = await make_resource(user.id)
        first_revision = await make_resource_version(first)
        second_revision = await make_resource_version(second)

        await make_publication(first, first_revision, publication_no=1)
        await make_publication(second, second_revision, publication_no=1)

        total = await session.scalar(text("SELECT count(*) FROM publications"))
        assert total == 2

    async def test_duplicate_publication_no_rejected_within_one_resource(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
        make_publication: Callable[..., Awaitable[Any]],
    ) -> None:
        """同一资源内的序号不得重复——即使旧版本已被撤销。"""
        user = make_user(email="pubno2@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)
        first = await make_publication(resource, revision, publication_no=1)
        await session.execute(
            text("UPDATE publications SET revoked_at = now() WHERE id = :id"),
            {"id": first.id},
        )

        second_revision = await make_resource_version(resource, revision_no=2)
        session.add(
            Publication(
                resource_id=resource.id,
                revision_id=second_revision.id,
                publication_no=1,
                published_by=user.id,
                public_fields=[],
            )
        )
        await _expect_error(session, "uq_publications_resource_publication")

    async def test_public_url_unique_is_carried_by_the_global_slug(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
    ) -> None:
        """公开 URL 的唯一性由全局唯一的 slug 承担（旧契约是 publications.public_url）。

        新契约把公开 URL 从投影里去掉、改由 ``resources.slug`` 派生，因此这里断言：
        1. ``publications`` 上不存在 ``uq_publications_public_url`` 这类全局唯一约束；
        2. 两个资源不能取同一个 slug——这正是"两个公开 URL 不会互相覆盖"的物理保证。
        """
        user = make_user(email="pubno3@example.com")
        await session.flush()

        unique_names = {
            row[0]
            for row in await session.execute(
                text(
                    "SELECT conname FROM pg_constraint con "
                    "JOIN pg_class c ON c.oid = con.conrelid "
                    "WHERE c.relname = 'publications' AND con.contype = 'u'"
                )
            )
        }
        assert "uq_publications_public_url" not in unique_names
        assert "uq_publications_resource_publication" in unique_names

        await make_resource(user.id, slug="shared-slug")
        session.add(Resource(owner_id=user.id, kind=ResourceKind.ARTICLE, slug="shared-slug"))
        await _expect_error(session, "uq_resources_slug")


class TestPublicationRevisionBelongsToSameResource:
    """A-P1-4：两个独立外键只能证明"两行存在"，不能证明属于同一资源。"""

    async def test_revision_of_another_resource_is_rejected(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="xref1@example.com")
        await session.flush()
        first = await make_resource(user.id)
        second = await make_resource(user.id)
        foreign_revision = await make_resource_version(second)

        # resource_id 指向 F，但 revision_id 指向 G 的版本。
        session.add(
            Publication(
                resource_id=first.id,
                revision_id=foreign_revision.id,
                publication_no=1,
                published_by=user.id,
                public_fields=[],
            )
        )
        await _expect_error(session, "fk_publications_resource_id_revision_id")

    async def test_matching_revision_is_accepted(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
        make_publication: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="xref2@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)
        publication = await make_publication(resource, revision, publication_no=1)
        assert publication.revision_id == revision.id


class TestActionIdempotencyWithNullTarget:
    """A-P1-2：唯一约束里的 NULL 默认互不相等，因此可空列不能承担去重。"""

    async def _action(self, session: AsyncSession, actor_id: uuid.UUID, **overrides: Any) -> Action:
        action = Action(
            actor_id=actor_id,
            type=overrides.pop("type", ActionType.UPDATE_SETTINGS),
            parameters=overrides.pop("parameters", {"assistant_theme": "dark"}),
            parameters_hash=overrides.pop("parameters_hash", "s" * 64),
            idempotency_key=overrides.pop("idempotency_key", f"act-{uuid.uuid4().hex[:16]}"),
            expires_at=overrides.pop("expires_at", dt.datetime.now(UTC) + dt.timedelta(hours=1)),
            **overrides,
        )
        session.add(action)
        await session.flush()
        return action

    async def test_null_target_id_still_dedupes(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """设置类动作没有具体目标对象：target_resource_id 为 NULL 也必须只留一条。"""
        user = make_user(email="actnull1@example.com")
        await session.flush()
        await self._action(
            session, user.id, idempotency_key="settings-apply-1", target_resource_id=None
        )

        session.add(
            Action(
                actor_id=user.id,
                type=ActionType.UPDATE_SETTINGS,
                target_resource_id=None,
                parameters={"assistant_theme": "light"},
                parameters_hash="t" * 64,
                idempotency_key="settings-apply-1",
                expires_at=dt.datetime.now(UTC) + dt.timedelta(hours=1),
            )
        )
        await _expect_error(session, "uq_actions_actor_id_idempotency_key")

    async def test_same_idempotency_key_with_different_parameters_still_dedupes(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """幂等键是稳定请求身份；参数不同由 parameters_hash 判等，不是新插入的理由。"""
        user = make_user(email="actnull2@example.com")
        await session.flush()
        await self._action(
            session,
            user.id,
            type=ActionType.PUBLISH,
            idempotency_key="publish-1",
            parameters={"visibility": "public"},
            parameters_hash="a" * 64,
        )
        session.add(
            Action(
                actor_id=user.id,
                type=ActionType.PUBLISH,
                parameters={"visibility": "private"},
                parameters_hash="b" * 64,
                idempotency_key="publish-1",
                expires_at=dt.datetime.now(UTC) + dt.timedelta(hours=1),
            )
        )
        await _expect_error(session, "uq_actions_actor_id_idempotency_key")

    async def test_distinct_idempotency_keys_coexist(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="actnull3@example.com")
        await session.flush()
        for index in range(2):
            await self._action(
                session,
                user.id,
                idempotency_key=f"publish-{index}",
                parameters={"index": index},
                parameters_hash=str(index) * 64,
            )

    async def test_idempotency_identity_columns_are_not_nullable(
        self, session: AsyncSession
    ) -> None:
        """A-P1-2 的结构前提：可空列会让唯一约束失效，因此身份列必须 NOT NULL。"""
        columns = await session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'actions'::text "
                "AND column_name = ANY(ARRAY['actor_id', 'idempotency_key']) "
                "AND is_nullable = 'YES'"
            )
        )
        assert list(columns) == []

    async def test_run_idempotency_is_scoped_to_the_user(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        """runs 的幂等身份同样是不可空复合列：同用户同键必须去重。"""
        user = make_user(email="actnull4@example.com")
        await session.flush()
        first = await make_conversation(user.id)
        second = await make_conversation(user.id)
        await make_run(first, idempotency_key="run-dedupe")

        session.add(
            Run(
                user_id=user.id,
                conversation_id=second.id,
                idempotency_key="run-dedupe",
                request_hash="r" * 64,
                checkpoint_thread_id="thread-dedupe",
            )
        )
        await _expect_error(session, "uq_runs_user_id_idempotency_key")

    async def test_action_persists_both_expected_versions(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
    ) -> None:
        """发布/撤回类动作必须同时保存内容版本与 ACL 版本，执行时分别比较。"""
        user = make_user(email="actnull5@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        action = await self._action(
            session,
            user.id,
            type=ActionType.PUBLISH,
            target_resource_id=resource.id,
            expected_version=5,
            expected_acl_version=2,
            idempotency_key="publish-both",
        )
        await session.refresh(action)
        assert action.expected_version == 5
        assert action.expected_acl_version == 2

    async def test_bad_action_status_rejected(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="actnull6@example.com")
        await session.flush()
        await _expect_error(
            session,
            "ck_actions_status_valid",
            statement=text(
                "INSERT INTO actions (actor_id, type, parameters, parameters_hash, "
                "idempotency_key, expires_at, status, authorization_kind, requires_confirmation, "
                "version) VALUES (:aid, 'publish', '{}'::jsonb, 'h', 'k', "
                "now() + interval '1 hour', 'waiting', 'confirmed_preview', true, 0)"
            ).bindparams(aid=user.id),
        )

    async def test_awaiting_confirmation_is_a_valid_status(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """API 契约使用 awaiting_confirmation；DB 状态集合必须能表示它。"""
        user = make_user(email="actnull7@example.com")
        await session.flush()
        action = await self._action(
            session,
            user.id,
            type=ActionType.PUBLISH,
            idempotency_key="await-1",
            status=ActionStatus.AWAITING_CONFIRMATION,
        )
        assert action.status is ActionStatus.AWAITING_CONFIRMATION

    async def test_default_authorization_kind_is_confirmed_preview(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """模型新生成或范围不明的内容必须走 confirmed_preview。"""
        user = make_user(email="actnull8@example.com")
        await session.flush()
        action = await self._action(session, user.id, idempotency_key="auth-default")
        assert action.authorization_kind is ActionAuthorizationKind.CONFIRMED_PREVIEW


class TestSessionRotationTombstone:
    """A-P2-5 在新契约下改由 ``revoked_at`` + ``auth_version`` 表达。

    旧契约用 ``replaced_by_session_id`` + ``rotated_at`` 记录轮换墓碑；新契约
    （``docs/architecture/database.md`` §3）只保留 ``revoked_at``，并用
    ``auth_version`` 作为身份失效闸门。因此"后继被清理不得让旧会话复活"这条
    不变量改由"撤销是单向布尔时刻、失效由 auth_version 决定"来证明。
    """

    async def test_rotation_marker_columns_and_check_are_gone(self, session: AsyncSession) -> None:
        """旧墓碑列与旧 CHECK 必须彻底消失：留着它们就等于两套失效语义并存。"""
        columns = await session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'auth_sessions'::text "
                "AND column_name = ANY(ARRAY['replaced_by_session_id', 'rotated_at'])"
            )
        )
        assert list(columns) == []
        checks = await session.execute(
            text(
                "SELECT conname FROM pg_constraint con "
                "JOIN pg_class c ON c.oid = con.conrelid "
                "WHERE c.relname = 'auth_sessions' AND con.contype = 'c'"
            )
        )
        assert "ck_auth_sessions_rotation_marker_consistent" not in {row[0] for row in checks}

    async def test_revoked_session_stays_revoked(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """撤销是落在行上的单向时刻：不依赖任何后继会话是否存在。"""
        user = make_user(email="rot1@example.com")
        await session.flush()
        now = dt.datetime.now(UTC)
        auth_session = AuthSession(
            user_id=user.id,
            token_hash="n" * 64,
            auth_version=user.auth_version,
            idle_expires_at=now + dt.timedelta(hours=1),
            absolute_expires_at=now + dt.timedelta(days=1),
            revoked_at=now,
        )
        session.add(auth_session)
        await session.flush()

        row = (
            await session.execute(
                text("SELECT revoked_at, auth_version FROM auth_sessions WHERE id = :id"),
                {"id": auth_session.id},
            )
        ).one()
        assert row[0] is not None, "撤销时刻必须保留"
        assert row[1] == user.auth_version

    async def test_revoked_session_needs_no_successor_row(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """旧契约要求"先有后继才能撤销"，新契约允许直接撤销——不存在悬挂引用。"""
        user = make_user(email="rot2@example.com")
        await session.flush()
        now = dt.datetime.now(UTC)
        session.add(
            AuthSession(
                user_id=user.id,
                token_hash="o" * 64,
                auth_version=user.auth_version,
                idle_expires_at=now + dt.timedelta(hours=1),
                absolute_expires_at=now + dt.timedelta(days=1),
                revoked_at=now,
            )
        )
        await session.flush()
        count = await session.scalar(
            text("SELECT count(*) FROM auth_sessions WHERE user_id = :uid"), {"uid": user.id}
        )
        assert count == 1

    async def test_auth_version_is_the_global_invalidation_gate(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """密码变化/身份撤销时递增 ``users.auth_version``；会话侧必须能表示旧版本。"""
        user = make_user(email="rot3@example.com", role=UserRole.MEMBER, auth_version=1)
        await session.flush()
        now = dt.datetime.now(UTC)
        session.add(
            AuthSession(
                user_id=user.id,
                token_hash="p" * 64,
                auth_version=1,
                idle_expires_at=now + dt.timedelta(hours=1),
                absolute_expires_at=now + dt.timedelta(days=1),
            )
        )
        await session.flush()

        await session.execute(
            text("UPDATE users SET auth_version = auth_version + 1 WHERE id = :id"),
            {"id": user.id},
        )
        stale = await session.scalar(
            text(
                "SELECT count(*) FROM auth_sessions s JOIN users u ON u.id = s.user_id "
                "WHERE u.auth_version <> s.auth_version"
            )
        )
        # 会话不会自动失效，但"不一致"这个状态是可查询的：认证检查必须据此拒绝。
        assert stale == 1


class TestCommentResourceIsOptional:
    """§6 缺口：公开留言板允许无文章关联，``comments.resource_id`` 必须可空。"""

    async def test_guestbook_comment_without_resource(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="gb1@example.com")
        await session.flush()
        session.add(
            Comment(
                resource_id=None,
                author_id=user.id,
                client_id=uuid.uuid4(),
                body="留言板留言",
            )
        )
        await session.flush()

    async def test_guestbook_reply_does_not_cross_check_resource(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
    ) -> None:
        """MATCH SIMPLE：resource_id 为空时复合外键不校验，由 service 承担责任。"""
        user = make_user(email="gb2@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        parent = Comment(
            resource_id=resource.id, author_id=user.id, client_id=uuid.uuid4(), body="父留言"
        )
        session.add(parent)
        await session.flush()

        reply = Comment(
            resource_id=None,
            parent_id=parent.id,
            author_id=user.id,
            client_id=uuid.uuid4(),
            body="留言板回复",
        )
        session.add(reply)
        await session.flush()
        assert reply.resource_id is None


class TestRunStatusCoversApiContract:
    """§6 缺口：DB 状态集合必须覆盖 API 契约的 run 状态。"""

    @pytest.mark.parametrize(
        "status",
        [
            RunStatus.QUEUED.value,
            RunStatus.RUNNING.value,
            RunStatus.WAITING_INPUT.value,
            RunStatus.WAITING_APPROVAL.value,
            RunStatus.WAITING_AUTH.value,
            RunStatus.SUCCEEDED.value,
            RunStatus.FAILED.value,
            RunStatus.CANCELLING.value,
            RunStatus.CANCELLED.value,
        ],
    )
    async def test_api_contract_status_is_representable(
        self, session: AsyncSession, status: str
    ) -> None:
        allowed = await session.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_runs_status_valid' AND contype = 'c'"
            )
        )
        assert allowed is not None
        assert f"'{status}'" in allowed, f"runs.status 不能表示 API 契约的 {status}"


class TestOwnershipIsStructural:
    """归属是结构约束而不是服务约定：跨租户写入必须在数据库层被拒。"""

    async def test_run_cannot_belong_to_another_users_conversation(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        owner = make_user(email="own1@example.com")
        intruder = make_user(email="own2@example.com")
        await session.flush()
        now = dt.datetime.now(UTC)
        conversation = await _conversation(session, owner.id)
        assert conversation.user_id == owner.id

        session.add(
            Run(
                user_id=intruder.id,
                conversation_id=conversation.id,
                idempotency_key=f"key-{uuid.uuid4().hex[:16]}",
                request_hash="r" * 64,
                checkpoint_thread_id=f"thread-{uuid.uuid4().hex[:8]}",
            )
        )
        await _expect_error(session, RUN_CONVERSATION_OWNER_FK)
        assert now is not None


async def _conversation(session: AsyncSession, user_id: uuid.UUID) -> Any:
    """小工具：本文件只在归属用例里需要会话，避免引入更多夹具参数。"""
    from autumn_backend.db.models import Conversation

    conversation = Conversation(user_id=user_id, mode=ConversationMode.OWNER)
    session.add(conversation)
    await session.flush()
    return conversation

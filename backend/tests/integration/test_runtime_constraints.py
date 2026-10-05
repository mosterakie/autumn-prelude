"""对话、运行、事件、动作、配额、作业、调用与审计表：真实 PostgreSQL 约束验收。

对应 ``docs/architecture/database.md`` §7、§8、§9。重点是被实施顺序点名的密集约束：

- ``runs`` 的幂等身份、非终态唯一、终态时刻与可延迟消息指针；
- ``runs`` / ``messages`` / ``quota_reservations`` / ``memories`` 的**复合归属外键**；
- ``messages`` 的部分唯一去重、``jobs`` 的 lease 一致性与 progress 范围；
- ``provider_calls`` 的逻辑调用身份与``actions`` 的状态机。
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
    NON_TERMINAL_RUN_STATUSES,
    ActionAuthorizationKind,
    ActionStatus,
    ActionType,
    AuditResult,
    ConversationMode,
    JobPhase,
    JobStatus,
    MessageRole,
    MessageStatus,
    ProviderCallPurpose,
    ProviderCallStatus,
    QuotaReservationStatus,
    RunEventType,
    RunSourceType,
    RunStatus,
    SummaryStatus,
)
from autumn_backend.db.models import (
    Action,
    AuditEvent,
    AuthSession,
    ConversationSummary,
    Job,
    Memory,
    Message,
    ProviderCall,
    QuotaBucket,
    QuotaReservation,
    Run,
    RunEvent,
    RunSource,
)

pytestmark = pytest.mark.integration

UTC = dt.UTC

MUTABLE_TABLES = (
    "conversations",
    "messages",
    "runs",
    "actions",
    "quota_buckets",
    "quota_reservations",
    "jobs",
    "provider_calls",
)

APPEND_ONLY_TABLES = ("run_events", "audit_events")

#: "run 必须与会话同属一个用户"的外键。列顺序是 (conversation_id, user_id) ——
#: 与 ``conversations(id, user_id)`` 的列顺序一一对应，名字由命名约定生成。
RUN_CONVERSATION_OWNER_FK = "fk_runs_conversation_id_user_id_conversations"


async def _expect_error(
    session: AsyncSession, message: str | None = None, *, statement: Any = None
) -> str:
    # 注意：SQLAlchemy 把 PostgreSQL 的 CheckViolation 也映射成 IntegrityError 的子类，
    # 但 asyncpg 原生异常会以 DBAPIError 形式冒出，两者都要接住。
    with pytest.raises((IntegrityError, DBAPIError, StatementError)) as excinfo:
        if statement is not None:
            await session.execute(statement)
        await session.flush()
    raw = str(excinfo.value)
    if message is not None:
        assert message in raw, raw
    return raw


def _bucket_window(day: int = 1) -> dt.datetime:
    """Asia/Shanghai 当天 00:00 对应的 UTC 时刻（测试只关心窗口是有序区间）。"""
    return dt.datetime(2026, 3, day, 16, 0, tzinfo=UTC)


async def _bucket(session: AsyncSession, user_id: uuid.UUID, **overrides: Any) -> QuotaBucket:
    window_start = overrides.pop("window_start", _bucket_window())
    bucket = QuotaBucket(
        user_id=user_id,
        window_start=window_start,
        window_end=overrides.pop("window_end", window_start + dt.timedelta(days=1)),
        **overrides,
    )
    session.add(bucket)
    await session.flush()
    return bucket


async def _job(session: AsyncSession, **overrides: Any) -> Job:
    job = Job(
        kind=overrides.pop("kind", "run.dispatch"),
        idempotency_key=overrides.pop("idempotency_key", f"job-{uuid.uuid4().hex[:16]}"),
        **overrides,
    )
    session.add(job)
    await session.flush()
    return job


class TestSchemaObjectsExist:
    async def test_new_tables_present(self, session: AsyncSession) -> None:
        names = [
            "conversations",
            "messages",
            "runs",
            "run_events",
            "actions",
            "quota_buckets",
            "quota_reservations",
            "jobs",
            "provider_calls",
            "audit_events",
            "run_sources",
            "conversation_summaries",
            "memories",
        ]
        rows = await session.execute(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename = ANY(:names)"
            ),
            {"names": names},
        )
        assert {row[0] for row in rows} == set(names)

    async def test_run_events_primary_key_is_composite(self, session: AsyncSession) -> None:
        columns = await session.execute(
            text(
                "SELECT a.attname FROM pg_index i "
                "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE i.indisprimary AND c.relname = 'pk_run_events' "
                "ORDER BY array_position(i.indkey, a.attnum)"
            )
        )
        assert [row[0] for row in columns] == ["run_id", "seq"]

    async def test_non_terminal_partial_unique_index_installed(self, session: AsyncSession) -> None:
        predicate = await session.scalar(
            text(
                "SELECT pg_get_expr(i.indpred, i.indrelid) FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = 'uq_runs_conversation_id_non_terminal'"
            )
        )
        assert predicate is not None
        for status in NON_TERMINAL_RUN_STATUSES:
            assert status in predicate
        assert "succeeded" not in predicate

    async def test_message_client_id_dedupe_index_installed(self, session: AsyncSession) -> None:
        predicate = await session.scalar(
            text(
                "SELECT pg_get_expr(i.indpred, i.indrelid) FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = 'uq_messages_conversation_id_client_message_id'"
            )
        )
        assert predicate is not None
        assert "client_message_id IS NOT NULL" in predicate

    async def test_updated_at_triggers_installed(self, session: AsyncSession) -> None:
        rows = await session.execute(
            text(
                "SELECT c.relname, t.tgenabled FROM pg_trigger t "
                "JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE c.relnamespace = 'public'::regnamespace AND NOT t.tgisinternal "
                "AND t.tgname LIKE 'trg%set_updated_at'"
            )
        )
        found = {row[0]: bytes(row[1]).decode() for row in rows}
        for name in MUTABLE_TABLES:
            assert found.get(name) == "O", f"{name} 缺少已启用的 updated_at 触发器"
        for name in APPEND_ONLY_TABLES:
            assert name not in found, f"{name} 是只追加表，不应挂 updated_at 触发器"


class TestRunConstraints:
    async def test_idempotency_key_unique_per_user(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="run1@example.com")
        await session.flush()
        first = await make_conversation(user.id)
        second = await make_conversation(user.id)
        await make_run(first, idempotency_key="same-key")

        session.add(
            Run(
                user_id=user.id,
                conversation_id=second.id,
                idempotency_key="same-key",
                request_hash="r" * 64,
                checkpoint_thread_id="thread-x",
            )
        )
        await _expect_error(session, "uq_runs_user_id_idempotency_key")

    async def test_same_key_allowed_for_different_users(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        first = make_user(email="run2@example.com")
        second = make_user(email="run3@example.com")
        await session.flush()
        for user in (first, second):
            conversation = await make_conversation(user.id)
            await make_run(conversation, idempotency_key="shared-key")

    async def test_only_one_non_terminal_run_per_conversation(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="run4@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        await make_run(conversation, status=RunStatus.RUNNING)

        session.add(
            Run(
                user_id=user.id,
                conversation_id=conversation.id,
                idempotency_key=f"key-{uuid.uuid4().hex[:16]}",
                request_hash="r" * 64,
                checkpoint_thread_id="thread-y",
                status=RunStatus.QUEUED,
            )
        )
        await _expect_error(session, "uq_runs_conversation_id_non_terminal")

    async def test_terminal_run_frees_the_conversation(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        """旧 run 进入终态后，同一会话可以再开新 run。"""
        user = make_user(email="run5@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        first = await make_run(conversation, status=RunStatus.RUNNING)

        await session.execute(
            text("UPDATE runs SET status = 'succeeded', finished_at = now() WHERE id = :id"),
            {"id": first.id},
        )
        await make_run(conversation, status=RunStatus.QUEUED)

    async def test_terminal_status_requires_finished_at(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
    ) -> None:
        """终态必须带 finished_at：用原生 SQL 绕过 Python 侧校验。"""
        user = make_user(email="run6@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)

        await _expect_error(
            session,
            "ck_runs_finished_at_matches_terminal_status",
            statement=text(
                "INSERT INTO runs (user_id, conversation_id, idempotency_key, request_hash, "
                "status, checkpoint_thread_id, next_event_seq, scope_epoch, version) "
                "VALUES (:uid, :cid, 'x', :rh, 'succeeded', 'thread-z', 1, 0, 0)"
            ).bindparams(uid=user.id, cid=conversation.id, rh="r" * 64),
        )

    async def test_non_terminal_status_must_not_have_finished_at(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="run7@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)

        await _expect_error(
            session,
            "ck_runs_finished_at_matches_terminal_status",
            statement=text(
                "INSERT INTO runs (user_id, conversation_id, idempotency_key, request_hash, "
                "status, checkpoint_thread_id, next_event_seq, scope_epoch, finished_at, version) "
                "VALUES (:uid, :cid, 'y', :rh, 'running', 'thread-w', 1, 0, now(), 0)"
            ).bindparams(uid=user.id, cid=conversation.id, rh="r" * 64),
        )

    async def test_next_event_seq_is_allocated_atomically(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        """``UPDATE ... RETURNING next_event_seq - 1`` 是唯一的 seq 分配方式。"""
        user = make_user(email="run8@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        assert run.next_event_seq == 1

        allocated: list[int] = []
        for _ in range(3):
            result = await session.execute(
                text(
                    "UPDATE runs SET next_event_seq = next_event_seq + 1 "
                    "WHERE id = :id RETURNING next_event_seq - 1"
                ),
                {"id": run.id},
            )
            allocated.append(int(result.scalar_one()))

        # next_event_seq 初值为 1，因此首次分配得到 1；序号从 1 开始且严格递增、不重号。
        assert allocated == [1, 2, 3]

    async def test_next_event_seq_cannot_go_below_one(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="run9@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        await _expect_error(
            session,
            "ck_runs_next_event_seq_positive",
            statement=text("UPDATE runs SET next_event_seq = 0 WHERE id = :id").bindparams(
                id=run.id
            ),
        )

    async def test_run_must_belong_to_the_conversation_owner(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
    ) -> None:
        """``(conversation_id, user_id)`` 复合外键：run 不能跑进别人的会话。"""
        owner = make_user(email="run10@example.com")
        intruder = make_user(email="run11@example.com")
        await session.flush()
        conversation = await make_conversation(owner.id)

        session.add(
            Run(
                user_id=intruder.id,
                conversation_id=conversation.id,
                idempotency_key=f"key-{uuid.uuid4().hex[:16]}",
                request_hash="r" * 64,
                checkpoint_thread_id="thread-intruder",
            )
        )
        await _expect_error(session, RUN_CONVERSATION_OWNER_FK)

    async def test_auth_session_must_belong_to_the_same_user(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
    ) -> None:
        """恢复时只能换成同一用户的当前会话：复合外键把这条规则结构化。"""
        owner = make_user(email="run12@example.com")
        other = make_user(email="run13@example.com")
        await session.flush()
        auth_session = AuthSession(
            user_id=other.id,
            token_hash="t" * 64,
            auth_version=1,
            idle_expires_at=dt.datetime.now(UTC) + dt.timedelta(hours=1),
            absolute_expires_at=dt.datetime.now(UTC) + dt.timedelta(days=1),
        )
        session.add(auth_session)
        await session.flush()
        conversation = await make_conversation(owner.id)

        session.add(
            Run(
                user_id=owner.id,
                conversation_id=conversation.id,
                auth_session_id=auth_session.id,
                idempotency_key=f"key-{uuid.uuid4().hex[:16]}",
                request_hash="r" * 64,
                checkpoint_thread_id="thread-session",
            )
        )
        await _expect_error(session, "fk_runs_auth_session_id_user_id")

    async def test_input_message_may_point_within_the_conversation(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        """正例：可延迟循环外键指向同会话消息时，强制检查立即通过。"""
        user = make_user(email="run14@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        message = Message(
            conversation_id=conversation.id,
            run_id=run.id,
            seq=1,
            role=MessageRole.USER,
            body_text="你好",
        )
        session.add(message)
        await session.flush()

        await session.execute(
            text("UPDATE runs SET input_message_id = :mid WHERE id = :rid"),
            {"mid": message.id, "rid": run.id},
        )
        await session.execute(text("SET CONSTRAINTS fk_runs_input_message_id_messages IMMEDIATE"))
        stored = await session.scalar(
            text("SELECT input_message_id FROM runs WHERE id = :rid"), {"rid": run.id}
        )
        assert stored == message.id

    async def test_input_message_cannot_point_at_another_conversation(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        """可延迟循环外键：消息指针必须留在同一会话内（文档 §11）。"""
        user = make_user(email="run15@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        other_conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        foreign_message = Message(
            conversation_id=other_conversation.id,
            seq=1,
            role=MessageRole.USER,
            body_text="别的会话",
        )
        session.add(foreign_message)
        await session.flush()

        await session.execute(
            text("UPDATE runs SET input_message_id = :mid WHERE id = :rid"),
            {"mid": foreign_message.id, "rid": run.id},
        )
        await _expect_error(
            session,
            "fk_runs_input_message_id_messages",
            statement=text("SET CONSTRAINTS fk_runs_input_message_id_messages IMMEDIATE"),
        )


class TestMessageConstraints:
    async def test_seq_unique_per_conversation(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="msg1@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        session.add(
            Message(
                conversation_id=conversation.id,
                seq=1,
                role=MessageRole.USER,
                body_text="你好",
            )
        )
        await session.flush()
        session.add(
            Message(
                conversation_id=conversation.id,
                seq=1,
                role=MessageRole.USER,
                body_text="重复序号",
            )
        )
        await _expect_error(session, "uq_messages_conversation_seq")

    async def test_message_run_must_belong_to_the_same_conversation(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        """``(run_id, conversation_id)`` 复合外键：消息不能挂到别的会话的 run 上。"""
        user = make_user(email="msg2@example.com")
        await session.flush()
        first = await make_conversation(user.id)
        second = await make_conversation(user.id)
        run = await make_run(first)

        session.add(
            Message(
                conversation_id=second.id,
                run_id=run.id,
                seq=1,
                role=MessageRole.ASSISTANT,
                body_text="错会话",
            )
        )
        await _expect_error(session, "fk_messages_run_id_conversation_id")

    async def test_same_conversation_run_is_accepted(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="msg3@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        message = Message(
            conversation_id=conversation.id,
            run_id=run.id,
            seq=1,
            role=MessageRole.USER,
            body_text="同一会话",
        )
        session.add(message)
        await session.flush()
        assert message.run_id == run.id

    async def test_client_message_id_dedupes_within_a_conversation(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
    ) -> None:
        """部分唯一索引：只有非空 ``client_message_id`` 参与去重。"""
        user = make_user(email="msg4@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        client_message_id = uuid.uuid4()
        for body in ("第一次", "重放"):
            session.add(
                Message(
                    conversation_id=conversation.id,
                    seq=1 if body == "第一次" else 2,
                    role=MessageRole.USER,
                    body_text=body,
                    client_message_id=client_message_id,
                )
            )
        await _expect_error(session, "uq_messages_conversation_id_client_message_id")

    async def test_null_client_message_ids_are_not_deduped(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
    ) -> None:
        """服务端生成的消息没有客户端标识：NULL 之间不参与唯一性。"""
        user = make_user(email="msg5@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        for seq in (1, 2):
            session.add(
                Message(
                    conversation_id=conversation.id,
                    seq=seq,
                    role=MessageRole.ASSISTANT,
                    body_text="服务端消息",
                )
            )
        await session.flush()

    async def test_same_client_message_id_allowed_in_another_conversation(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="msg6@example.com")
        await session.flush()
        client_message_id = uuid.uuid4()
        for _ in range(2):
            conversation = await make_conversation(user.id)
            session.add(
                Message(
                    conversation_id=conversation.id,
                    seq=1,
                    role=MessageRole.USER,
                    body_text="跨会话同一客户端 ID",
                    client_message_id=client_message_id,
                )
            )
        await session.flush()

    async def test_role_must_be_whitelisted(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
    ) -> None:
        """工具协议消息保存在运行状态中，不混入普通对话展示。"""
        user = make_user(email="msg7@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        await _expect_error(
            session,
            "ck_messages_role_valid",
            statement=text(
                "INSERT INTO messages (conversation_id, seq, role, body_text, content_version, "
                "status, version) VALUES (:cid, 1, 'tool', 'x', 1, 'complete', 0)"
            ).bindparams(cid=conversation.id),
        )

    async def test_status_and_counters_are_guarded(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="msg8@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        await _expect_error(
            session,
            "ck_messages_status_valid",
            statement=text(
                "INSERT INTO messages (conversation_id, seq, role, body_text, content_version, "
                "status, version) VALUES (:cid, 1, 'assistant', 'x', 1, 'streaming', 0)"
            ).bindparams(cid=conversation.id),
        )

    async def test_seq_must_be_positive(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="msg9@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        session.add(
            Message(
                conversation_id=conversation.id,
                seq=0,
                role=MessageRole.USER,
                body_text="零号",
            )
        )
        await _expect_error(session, "ck_messages_seq_positive")

    async def test_default_status_is_complete(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="msg10@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        message = Message(
            conversation_id=conversation.id,
            seq=1,
            role=MessageRole.ASSISTANT,
            body_text="完成的消息",
        )
        session.add(message)
        await session.flush()
        assert message.status is MessageStatus.COMPLETE
        assert message.content_version == 1


class TestRunEventConstraints:
    async def test_run_and_seq_is_the_identity(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="ev1@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)

        session.add(RunEvent(run_id=run.id, seq=0, type=RunEventType.RUN_STATUS, payload={"a": 1}))
        await session.flush()
        session.add(RunEvent(run_id=run.id, seq=0, type=RunEventType.DONE, payload={"b": 2}))
        await _expect_error(session, "pk_run_events")

    async def test_same_seq_allowed_for_different_runs(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="ev2@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        first = await make_run(conversation)
        await session.execute(
            text("UPDATE runs SET status = 'succeeded', finished_at = now() WHERE id = :id"),
            {"id": first.id},
        )
        second = await make_run(conversation)

        for run in (first, second):
            session.add(RunEvent(run_id=run.id, seq=0, type=RunEventType.RUN_STATUS))
        await session.flush()

    async def test_event_type_must_be_whitelisted(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="ev3@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)

        await _expect_error(
            session,
            "ck_run_events_type_valid",
            statement=text(
                "INSERT INTO run_events (run_id, seq, type) VALUES (:rid, 0, 'run.exploded')"
            ).bindparams(rid=run.id),
        )

    async def test_seq_must_be_non_negative(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="ev4@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        await _expect_error(
            session,
            "ck_run_events_seq_non_negative",
            statement=text(
                "INSERT INTO run_events (run_id, seq, type) VALUES (:rid, -1, 'done')"
            ).bindparams(rid=run.id),
        )

    async def test_payload_must_be_an_object(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="ev5@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        await _expect_error(
            session,
            "ck_run_events_payload_is_object",
            statement=text(
                "INSERT INTO run_events (run_id, seq, type, payload) "
                "VALUES (:rid, 0, 'done', '[1, 2]'::jsonb)"
            ).bindparams(rid=run.id),
        )

    async def test_deleting_run_cascades_to_events(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="ev6@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        session.add(RunEvent(run_id=run.id, seq=0, type=RunEventType.DONE))
        await session.flush()

        await session.execute(text("DELETE FROM runs WHERE id = :id"), {"id": run.id})
        remaining = await session.scalar(
            text("SELECT count(*) FROM run_events WHERE run_id = :rid"), {"rid": run.id}
        )
        assert remaining == 0


class TestActionConstraints:
    async def _action(self, session: AsyncSession, actor_id: uuid.UUID, **overrides: Any) -> Action:
        action = Action(
            actor_id=actor_id,
            type=overrides.pop("type", ActionType.PUBLISH),
            parameters=overrides.pop("parameters", {"visibility": "public"}),
            parameters_hash=overrides.pop("parameters_hash", "p" * 64),
            idempotency_key=overrides.pop("idempotency_key", f"act-{uuid.uuid4().hex[:16]}"),
            expires_at=overrides.pop("expires_at", dt.datetime.now(UTC) + dt.timedelta(hours=1)),
            **overrides,
        )
        session.add(action)
        await session.flush()
        return action

    async def test_idempotency_identity_blocks_duplicates_even_without_target(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """设置类动作没有目标对象：``target_resource_id`` 为 NULL 也必须只留一条。"""
        actor = make_user(email="ac1@example.com")
        await session.flush()
        await self._action(
            session,
            actor.id,
            type=ActionType.UPDATE_SETTINGS,
            idempotency_key="settings-apply-1",
            target_resource_id=None,
        )
        session.add(
            Action(
                actor_id=actor.id,
                type=ActionType.UPDATE_SETTINGS,
                target_resource_id=None,
                parameters={"assistant_theme": "dark"},
                parameters_hash="q" * 64,
                idempotency_key="settings-apply-1",
                expires_at=dt.datetime.now(UTC) + dt.timedelta(hours=1),
            )
        )
        await _expect_error(session, "uq_actions_actor_id_idempotency_key")

    async def test_same_key_with_different_parameters_is_still_rejected(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """幂等键是稳定请求身份；同键不同体由 service 映射 409，不是新增行。"""
        actor = make_user(email="ac2@example.com")
        await session.flush()
        await self._action(session, actor.id, idempotency_key="shared-key")
        session.add(
            Action(
                actor_id=actor.id,
                type=ActionType.PUBLISH,
                parameters={"visibility": "private"},
                parameters_hash="z" * 64,
                idempotency_key="shared-key",
                expires_at=dt.datetime.now(UTC) + dt.timedelta(hours=1),
            )
        )
        await _expect_error(session, "uq_actions_actor_id_idempotency_key")

    async def test_same_key_allowed_for_different_actors(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        first = make_user(email="ac3@example.com")
        second = make_user(email="ac4@example.com")
        await session.flush()
        await self._action(session, first.id, idempotency_key="per-actor-key")
        await self._action(session, second.id, idempotency_key="per-actor-key")

    async def test_type_and_status_whitelists(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        actor = make_user(email="ac5@example.com")
        await session.flush()
        await _expect_error(
            session,
            "ck_actions_status_valid",
            statement=text(
                "INSERT INTO actions (actor_id, type, parameters, parameters_hash, "
                "idempotency_key, expires_at, status, authorization_kind, requires_confirmation, "
                "version) VALUES (:aid, 'publish', '{}'::jsonb, 'h', 'k1', "
                "now() + interval '1 hour', 'waiting', 'confirmed_preview', true, 0)"
            ).bindparams(aid=actor.id),
        )

    async def test_unknown_action_type_is_rejected(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        actor = make_user(email="ac6@example.com")
        await session.flush()
        await _expect_error(
            session,
            "ck_actions_type_valid",
            statement=text(
                "INSERT INTO actions (actor_id, type, parameters, parameters_hash, "
                "idempotency_key, expires_at, status, authorization_kind, requires_confirmation, "
                "version) VALUES (:aid, 'teleport', '{}'::jsonb, 'h', 'k2', "
                "now() + interval '1 hour', 'proposed', 'confirmed_preview', true, 0)"
            ).bindparams(aid=actor.id),
        )

    async def test_unknown_authorization_kind_is_rejected(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """模型新生成或范围不明的内容必须走 confirmed_preview。"""
        actor = make_user(email="ac7@example.com")
        await session.flush()
        await _expect_error(
            session,
            "ck_actions_authorization_kind_valid",
            statement=text(
                "INSERT INTO actions (actor_id, type, parameters, parameters_hash, "
                "idempotency_key, expires_at, status, authorization_kind, requires_confirmation, "
                "version) VALUES (:aid, 'publish', '{}'::jsonb, 'h', 'k3', "
                "now() + interval '1 hour', 'proposed', 'model_guess', true, 0)"
            ).bindparams(aid=actor.id),
        )

    async def test_confirmed_statuses_require_confirmed_at(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """running 却没有 confirmed_at：跳过确认直接执行，必须被拒绝。"""
        actor = make_user(email="ac8@example.com")
        await session.flush()
        await _expect_error(
            session,
            "ck_actions_confirmed_statuses_require_confirmed_at",
            statement=text(
                "INSERT INTO actions (actor_id, type, parameters, parameters_hash, "
                "idempotency_key, expires_at, status, authorization_kind, requires_confirmation, "
                "version) VALUES (:aid, 'publish', '{}'::jsonb, 'h', 'k4', "
                "now() + interval '1 hour', 'running', 'confirmed_preview', true, 0)"
            ).bindparams(aid=actor.id),
        )

    async def test_unconfirmed_statuses_must_not_carry_confirmed_at(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """等待确认 ≠ 已确认：把 confirmed_at 提前写上必须被拒绝。"""
        actor = make_user(email="ac9@example.com")
        await session.flush()
        await _expect_error(
            session,
            "ck_actions_unconfirmed_has_no_confirmed_at",
            statement=text(
                "INSERT INTO actions (actor_id, type, parameters, parameters_hash, "
                "idempotency_key, expires_at, status, authorization_kind, requires_confirmation, "
                "confirmed_at, version) VALUES (:aid, 'publish', '{}'::jsonb, 'h', 'k5', "
                "now() + interval '1 hour', 'awaiting_confirmation', 'confirmed_preview', true, "
                "now(), 0)"
            ).bindparams(aid=actor.id),
        )

    async def test_executed_at_matches_succeeded(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        actor = make_user(email="ac10@example.com")
        await session.flush()
        await _expect_error(
            session,
            "ck_actions_executed_at_matches_status",
            statement=text(
                "INSERT INTO actions (actor_id, type, parameters, parameters_hash, "
                "idempotency_key, expires_at, status, authorization_kind, requires_confirmation, "
                "confirmed_at, executed_at, version) VALUES (:aid, 'publish', '{}'::jsonb, 'h', "
                "'k6', now() + interval '1 hour', 'succeeded', 'confirmed_preview', true, now(), "
                "NULL, 0)"
            ).bindparams(aid=actor.id),
        )

    async def test_lifecycle_round_trip(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """proposed → awaiting_confirmation → ready → running → succeeded。"""
        actor = make_user(email="ac11@example.com")
        await session.flush()
        action = await self._action(session, actor.id)
        assert action.status is ActionStatus.PROPOSED
        assert action.authorization_kind is ActionAuthorizationKind.CONFIRMED_PREVIEW

        for statement in (
            "UPDATE actions SET status = 'awaiting_confirmation' WHERE id = :id",
            "UPDATE actions SET status = 'ready', confirmed_at = now() WHERE id = :id",
            "UPDATE actions SET status = 'running' WHERE id = :id",
            "UPDATE actions SET status = 'succeeded', executed_at = now() WHERE id = :id",
        ):
            await session.execute(text(statement), {"id": action.id})
        await session.refresh(action)
        assert action.status is ActionStatus.SUCCEEDED
        assert action.executed_at is not None

    async def test_expected_versions_are_non_negative(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        actor = make_user(email="ac12@example.com")
        await session.flush()
        await _expect_error(
            session,
            "ck_actions_expected_version_non_negative",
            statement=text(
                "INSERT INTO actions (actor_id, type, parameters, parameters_hash, "
                "idempotency_key, expires_at, status, authorization_kind, requires_confirmation, "
                "expected_version, version) VALUES (:aid, 'publish', '{}'::jsonb, 'h', 'k7', "
                "now() + interval '1 hour', 'proposed', 'confirmed_preview', true, -1, 0)"
            ).bindparams(aid=actor.id),
        )

    async def test_expiry_must_be_after_creation(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        actor = make_user(email="ac13@example.com")
        await session.flush()
        await _expect_error(
            session,
            "ck_actions_expires_after_created",
            statement=text(
                "INSERT INTO actions (actor_id, type, parameters, parameters_hash, "
                "idempotency_key, expires_at, status, authorization_kind, requires_confirmation, "
                "version, created_at) VALUES (:aid, 'publish', '{}'::jsonb, 'h', 'k8', "
                "now() - interval '1 hour', 'proposed', 'confirmed_preview', true, 0, now())"
            ).bindparams(aid=actor.id),
        )

    async def test_parameters_must_be_an_object(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        actor = make_user(email="ac14@example.com")
        await session.flush()
        await _expect_error(
            session,
            "ck_actions_parameters_is_object",
            statement=text(
                "INSERT INTO actions (actor_id, type, parameters, parameters_hash, "
                "idempotency_key, expires_at, status, authorization_kind, requires_confirmation, "
                "version) VALUES (:aid, 'publish', '[1]'::jsonb, 'h', 'k9', "
                "now() + interval '1 hour', 'proposed', 'confirmed_preview', true, 0)"
            ).bindparams(aid=actor.id),
        )

    async def test_confirmed_action_may_run(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        actor = make_user(email="ac15@example.com")
        await session.flush()
        action = await self._action(
            session,
            actor.id,
            status=ActionStatus.RUNNING,
            confirmed_at=dt.datetime.now(UTC),
        )
        assert action.confirmed_at is not None


class TestQuotaConstraints:
    async def test_bucket_window_unique_per_user(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="q1@example.com")
        await session.flush()
        window = _bucket_window(1)
        await _bucket(session, user.id, window_start=window)

        session.add(
            QuotaBucket(
                user_id=user.id,
                window_start=window,
                window_end=window + dt.timedelta(days=1),
            )
        )
        await _expect_error(session, "uq_quota_buckets_user_window")

    async def test_counters_cannot_be_negative(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="q2@example.com")
        await session.flush()
        await _bucket(session, user.id, window_start=_bucket_window(2))
        await _expect_error(
            session,
            "ck_quota_buckets_used_non_negative",
            statement=text("UPDATE quota_buckets SET used = -1 WHERE user_id = :uid").bindparams(
                uid=user.id
            ),
        )

    async def test_reserved_cannot_be_negative(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="q3@example.com")
        await session.flush()
        await _bucket(session, user.id, window_start=_bucket_window(3))
        await _expect_error(
            session,
            "ck_quota_buckets_reserved_non_negative",
            statement=text(
                "UPDATE quota_buckets SET reserved = -1 WHERE user_id = :uid"
            ).bindparams(uid=user.id),
        )

    async def test_window_must_be_ordered(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """CHECK window_end > window_start：限额窗口必须是有序区间。"""
        user = make_user(email="q4@example.com")
        await session.flush()
        window = _bucket_window(4)
        session.add(QuotaBucket(user_id=user.id, window_start=window, window_end=window))
        await _expect_error(session, "ck_quota_buckets_window_ordered")

    async def test_policy_version_cannot_be_negative(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="q5@example.com")
        await session.flush()
        session.add(
            QuotaBucket(
                user_id=user.id,
                window_start=_bucket_window(5),
                window_end=_bucket_window(5) + dt.timedelta(days=1),
                policy_version=-1,
            )
        )
        await _expect_error(session, "ck_quota_buckets_policy_version_non_negative")

    async def test_reservation_run_id_is_unique(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="q6@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        bucket = await _bucket(session, user.id, window_start=_bucket_window(6))

        session.add(QuotaReservation(run_id=run.id, bucket_id=bucket.id, user_id=user.id))
        await session.flush()
        session.add(QuotaReservation(run_id=run.id, bucket_id=bucket.id, user_id=user.id))
        await _expect_error(session, "uq_quota_reservations_run_id")

    async def test_reservation_amount_must_be_one(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="q7@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        bucket = await _bucket(session, user.id, window_start=_bucket_window(7))

        await _expect_error(
            session,
            "ck_quota_reservations_amount_is_one",
            statement=text(
                "INSERT INTO quota_reservations (run_id, bucket_id, user_id, amount, status, "
                "version) VALUES (:rid, :bid, :uid, 2, 'reserved', 0)"
            ).bindparams(rid=run.id, bid=bucket.id, uid=user.id),
        )

    async def test_reservation_status_machine_values(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="q8@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        bucket = await _bucket(session, user.id, window_start=_bucket_window(8))

        await _expect_error(
            session,
            "ck_quota_reservations_status_valid",
            statement=text(
                "INSERT INTO quota_reservations (run_id, bucket_id, user_id, amount, status, "
                "version) VALUES (:rid, :bid, :uid, 1, 'pending', 0)"
            ).bindparams(rid=run.id, bid=bucket.id, uid=user.id),
        )
        assert QuotaReservationStatus.CHARGED.value == "charged"

    async def test_reserved_cannot_already_be_charged(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="q9@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        bucket = await _bucket(session, user.id, window_start=_bucket_window(9))

        session.add(
            QuotaReservation(
                run_id=run.id,
                bucket_id=bucket.id,
                user_id=user.id,
                charged_at=dt.datetime.now(UTC),
            )
        )
        await _expect_error(session, "ck_quota_reservations_reserved_not_charged")

    async def test_released_requires_settled_at(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="q10@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        bucket = await _bucket(session, user.id, window_start=_bucket_window(10))

        session.add(
            QuotaReservation(
                run_id=run.id,
                bucket_id=bucket.id,
                user_id=user.id,
                status=QuotaReservationStatus.RELEASED,
            )
        )
        await _expect_error(session, "ck_quota_reservations_settled_at_matches_terminal_status")

    async def test_bucket_must_belong_to_the_same_user(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        """``(bucket_id, user_id)`` 复合外键：不能扣别人的额度桶。"""
        owner = make_user(email="q11@example.com")
        other = make_user(email="q12@example.com")
        await session.flush()
        conversation = await make_conversation(owner.id)
        run = await make_run(conversation)
        bucket = await _bucket(session, other.id, window_start=_bucket_window(11))

        session.add(QuotaReservation(run_id=run.id, bucket_id=bucket.id, user_id=owner.id))
        await _expect_error(session, "fk_quota_reservations_bucket_id_user_id")

    async def test_run_must_belong_to_the_same_user(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        """``(run_id, user_id)`` 复合外键：预留的用户必须就是 run 的用户。"""
        owner = make_user(email="q13@example.com")
        other = make_user(email="q14@example.com")
        await session.flush()
        other_conversation = await make_conversation(other.id)
        foreign_run = await make_run(other_conversation)
        bucket = await _bucket(session, owner.id, window_start=_bucket_window(12))

        session.add(QuotaReservation(run_id=foreign_run.id, bucket_id=bucket.id, user_id=owner.id))
        await _expect_error(session, "fk_quota_reservations_run_id_user_id")

    async def test_reservation_round_trip_for_the_same_user(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="q15@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        bucket = await _bucket(session, user.id, window_start=_bucket_window(13))
        reservation = QuotaReservation(run_id=run.id, bucket_id=bucket.id, user_id=user.id)
        session.add(reservation)
        await session.flush()
        assert reservation.amount == 1
        assert reservation.status is QuotaReservationStatus.RESERVED


class TestJobConstraints:
    async def test_lease_fields_must_match_status(self, session: AsyncSession) -> None:
        await _expect_error(
            session,
            "ck_jobs_lease_fields_match_status",
            statement=text(
                "INSERT INTO jobs (kind, status, idempotency_key, attempts, max_attempts, version) "
                "VALUES ('run.dispatch', 'running', 'job-lease-1', 1, 5, 0)"
            ),
        )

    async def test_queued_job_cannot_carry_lease(self, session: AsyncSession) -> None:
        await _expect_error(
            session,
            "ck_jobs_lease_fields_match_status",
            statement=text(
                "INSERT INTO jobs (kind, status, idempotency_key, attempts, max_attempts, "
                "lease_token, lease_expires_at, version) "
                "VALUES ('run.dispatch', 'queued', 'job-lease-2', 0, 5, gen_random_uuid(), "
                "now() + interval '1 hour', 0)"
            ),
        )

    async def test_running_job_requires_both_lease_fields(self, session: AsyncSession) -> None:
        """只给 token 不给到期时刻同样属于半截租约。"""
        await _expect_error(
            session,
            "ck_jobs_lease_fields_match_status",
            statement=text(
                "INSERT INTO jobs (kind, status, idempotency_key, attempts, max_attempts, "
                "lease_token, version) "
                "VALUES ('run.dispatch', 'running', 'job-lease-3', 1, 5, gen_random_uuid(), 0)"
            ),
        )

    async def test_running_job_round_trip(self, session: AsyncSession) -> None:
        job = await _job(
            session,
            status=JobStatus.RUNNING,
            phase=JobPhase.INDEXING,
            lease_token=uuid.uuid4(),
            lease_expires_at=dt.datetime.now(UTC) + dt.timedelta(minutes=5),
            attempts=1,
        )
        assert job.lease_token is not None
        assert job.phase is JobPhase.INDEXING

    async def test_progress_must_be_in_range(self, session: AsyncSession) -> None:
        await _expect_error(
            session,
            "ck_jobs_progress_in_range",
            statement=text(
                "INSERT INTO jobs (kind, status, idempotency_key, attempts, max_attempts, "
                "progress, version) VALUES ('run.dispatch', 'queued', 'job-progress-1', 0, 5, "
                "101, 0)"
            ),
        )

    async def test_negative_progress_is_rejected(self, session: AsyncSession) -> None:
        await _expect_error(
            session,
            "ck_jobs_progress_in_range",
            statement=text(
                "INSERT INTO jobs (kind, status, idempotency_key, attempts, max_attempts, "
                "progress, version) VALUES ('run.dispatch', 'queued', 'job-progress-2', 0, 5, "
                "-1, 0)"
            ),
        )

    async def test_progress_may_be_absent_or_complete(self, session: AsyncSession) -> None:
        """进度不确定时写 NULL，不编造百分比；100 是合法的完成值。"""
        unknown = await _job(session, idempotency_key="job-progress-3")
        assert unknown.progress is None
        done = await _job(session, idempotency_key="job-progress-4", progress=100)
        assert done.progress == 100

    async def test_kind_shape_is_domain_verb(self, session: AsyncSession) -> None:
        await _expect_error(
            session,
            "ck_jobs_kind_shape",
            statement=text(
                "INSERT INTO jobs (kind, status, idempotency_key, attempts, max_attempts, version) "
                "VALUES ('Run.Dispatch', 'queued', 'job-kind-1', 0, 5, 0)"
            ),
        )

    async def test_idempotency_key_is_unique(self, session: AsyncSession) -> None:
        await _job(session, idempotency_key="job-dup-key")
        session.add(Job(kind="run.dispatch", idempotency_key="job-dup-key"))
        await _expect_error(session, "uq_jobs_idempotency_key")

    async def test_attempt_counters_are_guarded(self, session: AsyncSession) -> None:
        await _expect_error(
            session,
            "ck_jobs_attempts_non_negative",
            statement=text(
                "INSERT INTO jobs (kind, status, idempotency_key, attempts, max_attempts, version) "
                "VALUES ('run.dispatch', 'queued', 'job-attempts-1', -1, 5, 0)"
            ),
        )

    async def test_max_attempts_must_be_positive(self, session: AsyncSession) -> None:
        await _expect_error(
            session,
            "ck_jobs_max_attempts_positive",
            statement=text(
                "INSERT INTO jobs (kind, status, idempotency_key, attempts, max_attempts, version) "
                "VALUES ('run.dispatch', 'queued', 'job-attempts-2', 0, 0, 0)"
            ),
        )

    async def test_available_at_has_server_default(self, session: AsyncSession) -> None:
        job = await _job(session, idempotency_key="job-available")
        await session.refresh(job)
        assert job.available_at is not None

    async def test_claim_query_shape_works(self, session: AsyncSession) -> None:
        """SKIP LOCKED 取任务：真实 PostgreSQL 上可执行。"""
        await _job(session, idempotency_key="job-claim")
        picked = await session.scalar(
            text(
                "SELECT id FROM jobs WHERE status = 'queued' AND available_at <= now() "
                "ORDER BY available_at, id "
                "FOR UPDATE SKIP LOCKED LIMIT 1"
            )
        )
        assert isinstance(picked, uuid.UUID)


class TestProviderCallConstraints:
    async def test_logical_call_attempt_identity_is_unique(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="pc1@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)

        for _ in range(2):
            session.add(
                ProviderCall(
                    run_id=run.id,
                    provider="deepseek",
                    purpose=ProviderCallPurpose.CHAT,
                    logical_call_key="run:1:chat",
                    attempt_no=1,
                )
            )
        await _expect_error(session, "uq_provider_calls_logical_key_attempt")

    async def test_new_attempt_of_the_same_logical_call_is_allowed(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        """物理尝试编号区分重送：同一逻辑调用的 attempt 2 是合法的新行。"""
        user = make_user(email="pc2@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        for attempt in (1, 2):
            session.add(
                ProviderCall(
                    run_id=run.id,
                    provider="deepseek",
                    purpose=ProviderCallPurpose.CHAT,
                    logical_call_key="run:2:chat",
                    attempt_no=attempt,
                )
            )
        await session.flush()

    async def test_call_must_be_traceable(self, session: AsyncSession) -> None:
        await _expect_error(
            session,
            "ck_provider_calls_traceable_target",
            statement=text(
                "INSERT INTO provider_calls (provider, purpose, logical_call_key, attempt_no, "
                "status, version) VALUES ('p', 'chat', 'k', 1, 'prepared', 0)"
            ),
        )

    async def test_prepared_cannot_have_start_time(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="pc3@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        await _expect_error(
            session,
            "ck_provider_calls_prepared_not_started",
            statement=text(
                "INSERT INTO provider_calls (run_id, provider, purpose, logical_call_key, "
                "attempt_no, status, started_at, version) "
                "VALUES (:rid, 'p', 'chat', 'k', 1, 'prepared', now(), 0)"
            ).bindparams(rid=run.id),
        )

    async def test_terminal_status_requires_finished_at(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="pc4@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        await _expect_error(
            session,
            "ck_provider_calls_finished_at_matches_terminal_status",
            statement=text(
                "INSERT INTO provider_calls (run_id, provider, purpose, logical_call_key, "
                "attempt_no, status, version) VALUES (:rid, 'p', 'chat', 'k', 1, 'succeeded', 0)"
            ).bindparams(rid=run.id),
        )

    async def test_cost_requires_a_currency(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        """不同币种不能直接相加：有费用就必须有币种。"""
        user = make_user(email="pc5@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        await _expect_error(
            session,
            "ck_provider_calls_currency_required_with_cost",
            statement=text(
                "INSERT INTO provider_calls (run_id, provider, purpose, logical_call_key, "
                "attempt_no, status, estimated_cost, version) "
                "VALUES (:rid, 'p', 'chat', 'k', 1, 'prepared', 1.5, 0)"
            ).bindparams(rid=run.id),
        )

    async def test_cost_with_currency_is_accepted(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="pc6@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        call = ProviderCall(
            run_id=run.id,
            provider="deepseek",
            purpose=ProviderCallPurpose.CHAT,
            logical_call_key="run:3:chat",
            attempt_no=1,
            status=ProviderCallStatus.SUCCEEDED,
            started_at=dt.datetime.now(UTC),
            finished_at=dt.datetime.now(UTC),
            estimated_cost=0.5,
            actual_cost=0.4,
            currency="USD",
        )
        session.add(call)
        await session.flush()
        assert call.currency == "USD"

    async def test_attempt_no_must_be_positive(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="pc7@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        await _expect_error(
            session,
            "ck_provider_calls_attempt_no_positive",
            statement=text(
                "INSERT INTO provider_calls (run_id, provider, purpose, logical_call_key, "
                "attempt_no, status, version) VALUES (:rid, 'p', 'chat', 'k', 0, 'prepared', 0)"
            ).bindparams(rid=run.id),
        )

    async def test_unknown_is_representable(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        """unknown 是独立状态：不能当作"失败且零成本"，因此允许无成本但有 finished_at。"""
        user = make_user(email="pc8@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        call = ProviderCall(
            run_id=run.id,
            provider="deepseek",
            purpose=ProviderCallPurpose.SEARCH,
            logical_call_key="run:4:search",
            attempt_no=1,
            status=ProviderCallStatus.UNKNOWN,
            started_at=dt.datetime.now(UTC),
            finished_at=dt.datetime.now(UTC),
        )
        session.add(call)
        await session.flush()
        assert call.actual_cost is None

    async def test_deleting_job_keeps_cost_record(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        """成本记录不能因为 job 被清理而消失：外键是 SET NULL。"""
        user = make_user(email="pc9@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        job = await _job(session, run_id=run.id, actor_id=user.id)
        session.add(
            ProviderCall(
                job_id=job.id,
                run_id=run.id,
                provider="deepseek",
                purpose=ProviderCallPurpose.EMAIL,
                logical_call_key="job:1:mail",
                attempt_no=1,
            )
        )
        await session.flush()

        await session.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job.id})
        orphaned = await session.scalar(
            text("SELECT job_id FROM provider_calls WHERE run_id = :rid"), {"rid": run.id}
        )
        assert orphaned is None


class TestAuditEventConstraints:
    async def test_append_only_shape(self, session: AsyncSession) -> None:
        columns = await session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'audit_events'::text "
                "AND column_name = ANY(ARRAY['updated_at', 'version', 'deleted_at'])"
            )
        )
        assert list(columns) == []

    async def test_event_type_shape_is_enforced(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        await _expect_error(
            session,
            "ck_audit_events_event_type_shape",
            statement=text(
                "INSERT INTO audit_events (event_type, result) VALUES ('ResourceEdit', 'succeeded')"
            ),
        )
        assert make_user is not None

    async def test_valid_event_type_round_trip(self, session: AsyncSession) -> None:
        session.add(AuditEvent(event_type="resource.edit", result=AuditResult.SUCCEEDED))
        await session.flush()

    async def test_result_must_be_whitelisted(self, session: AsyncSession) -> None:
        await _expect_error(
            session,
            "ck_audit_events_result_valid",
            statement=text(
                "INSERT INTO audit_events (event_type, result) VALUES ('resource.edit', 'ok')"
            ),
        )

    async def test_version_fields_are_non_negative(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="a1@example.com")
        await session.flush()
        session.add(
            AuditEvent(
                event_type="resource.edit",
                result=AuditResult.SUCCEEDED,
                actor_id=user.id,
                before_version=1,
                after_version=2,
            )
        )
        await session.flush()

        await _expect_error(
            session,
            "ck_audit_events_after_version_non_negative",
            statement=text(
                "INSERT INTO audit_events (event_type, result, before_version, after_version) "
                "VALUES ('resource.edit', 'succeeded', 1, -1)"
            ),
        )

    async def test_metadata_must_be_an_object(self, session: AsyncSession) -> None:
        await _expect_error(
            session,
            "ck_audit_events_metadata_is_object",
            statement=text(
                "INSERT INTO audit_events (event_type, result, metadata_json) "
                "VALUES ('resource.edit', 'succeeded', '[1]'::jsonb)"
            ),
        )

    async def test_metadata_holds_acl_values_not_version_fields(
        self, session: AsyncSession
    ) -> None:
        """ACL 前后值写 metadata，不占用 before/after_version。"""
        event = AuditEvent(
            event_type="publication.publish",
            result=AuditResult.SUCCEEDED,
            before_version=None,
            after_version=None,
            metadata_json={"acl_version_before": 0, "acl_version_after": 1},
        )
        session.add(event)
        await session.flush()
        await session.refresh(event)
        assert event.before_version is None
        assert event.metadata_json == {"acl_version_before": 0, "acl_version_after": 1}

    async def test_deleting_actor_keeps_the_audit_row(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """删账号不能连审计一起删掉：actor_id 是 SET NULL。"""
        user = make_user(email="a2@example.com")
        await session.flush()
        session.add(
            AuditEvent(event_type="auth.login", result=AuditResult.SUCCEEDED, actor_id=user.id)
        )
        await session.flush()

        await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
        remaining = await session.scalar(
            text(
                "SELECT count(*) FROM audit_events "
                "WHERE event_type = 'auth.login' AND actor_id IS NULL"
            )
        )
        assert remaining == 1


class TestRunSourceConstraints:
    async def test_resource_source_round_trip(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
        make_publication: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="rs1@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)
        publication = await make_publication(resource, revision)

        session.add(
            RunSource(
                run_id=run.id,
                source_type=RunSourceType.RESOURCE,
                source_key="res-1",
                resource_id=resource.id,
                revision_id=revision.id,
            )
        )
        session.add(
            RunSource(
                run_id=run.id,
                source_type=RunSourceType.RESOURCE,
                source_key="res-2",
                resource_id=resource.id,
                revision_id=revision.id,
                publication_id=publication.id,
            )
        )
        await session.flush()

    async def test_web_source_round_trip(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="rs2@example.com")
        await session.flush()
        conversation = await make_conversation(user.id, mode=ConversationMode.OWNER)
        run = await make_run(conversation)
        session.add(
            RunSource(
                run_id=run.id,
                source_type=RunSourceType.WEB,
                source_key="web-1",
                web_url="https://example.com/page",
                web_title="网页标题",
            )
        )
        await session.flush()

    async def test_resource_source_requires_a_revision(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
        make_resource: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="rs3@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        resource = await make_resource(user.id)
        await _expect_error(
            session,
            "ck_run_sources_source_type_consistent",
            statement=text(
                "INSERT INTO run_sources (run_id, source_type, source_key, resource_id, "
                "observed_acl_version) VALUES (:rid, 'resource', 'k', :res, 0)"
            ).bindparams(rid=run.id, res=resource.id),
        )

    async def test_web_source_must_not_carry_resource_references(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
    ) -> None:
        """web 类型禁止填入无意义的资源外键（文档 §7）。"""
        user = make_user(email="rs4@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)
        await _expect_error(
            session,
            "ck_run_sources_source_type_consistent",
            statement=text(
                "INSERT INTO run_sources (run_id, source_type, source_key, resource_id, "
                "revision_id, web_url, observed_acl_version) "
                "VALUES (:rid, 'web', 'k', :res, :rev, 'https://example.com', 0)"
            ).bindparams(rid=run.id, res=resource.id, rev=revision.id),
        )

    async def test_web_source_requires_a_url(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="rs5@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        await _expect_error(
            session,
            "ck_run_sources_source_type_consistent",
            statement=text(
                "INSERT INTO run_sources (run_id, source_type, source_key, observed_acl_version) "
                "VALUES (:rid, 'web', 'k', 0)"
            ).bindparams(rid=run.id),
        )

    async def test_revision_must_belong_to_the_resource(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="rs6@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        first = await make_resource(user.id)
        second = await make_resource(user.id)
        foreign_revision = await make_resource_version(second)
        await _expect_error(
            session,
            "fk_run_sources_resource_id_revision_id",
            statement=text(
                "INSERT INTO run_sources (run_id, source_type, source_key, resource_id, "
                "revision_id, observed_acl_version) VALUES (:rid, 'resource', 'k', :res, :rev, 0)"
            ).bindparams(rid=run.id, res=first.id, rev=foreign_revision.id),
        )

    async def test_index_and_chunk_must_be_paired(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="rs7@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)
        await _expect_error(
            session,
            "ck_run_sources_index_chunk_paired",
            statement=text(
                "INSERT INTO run_sources (run_id, source_type, source_key, resource_id, "
                "revision_id, index_id, observed_acl_version) "
                "VALUES (:rid, 'resource', 'k', :res, :rev, gen_random_uuid(), 0)"
            ).bindparams(rid=run.id, res=resource.id, rev=revision.id),
        )

    async def test_source_key_is_unique_per_run(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="rs8@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        for _ in range(2):
            session.add(
                RunSource(
                    run_id=run.id,
                    source_type=RunSourceType.WEB,
                    source_key="same-key",
                    web_url="https://example.com",
                )
            )
        await _expect_error(session, "uq_run_sources_run_id_source_key")

    async def test_observed_acl_version_is_non_negative(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="rs9@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        await _expect_error(
            session,
            "ck_run_sources_observed_acl_version_non_negative",
            statement=text(
                "INSERT INTO run_sources (run_id, source_type, source_key, web_url, "
                "observed_acl_version) VALUES (:rid, 'web', 'k', 'https://example.com', -1)"
            ).bindparams(rid=run.id),
        )


class TestConversationSummaryConstraints:
    async def test_only_one_active_summary_per_conversation(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="cs1@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        session.add(
            ConversationSummary(
                conversation_id=conversation.id, upto_message_seq=5, body_text="摘要一"
            )
        )
        await session.flush()
        session.add(
            ConversationSummary(
                conversation_id=conversation.id, upto_message_seq=9, body_text="摘要二"
            )
        )
        await _expect_error(session, "uq_conversation_summaries_active")

    async def test_stale_summary_frees_the_slot(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="cs2@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        session.add(
            ConversationSummary(
                conversation_id=conversation.id,
                upto_message_seq=5,
                body_text="旧摘要",
                status=SummaryStatus.STALE,
            )
        )
        await session.flush()
        session.add(
            ConversationSummary(
                conversation_id=conversation.id, upto_message_seq=9, body_text="新摘要"
            )
        )
        await session.flush()

    async def test_summary_guards(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="cs3@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        session.add(
            ConversationSummary(
                conversation_id=conversation.id, upto_message_seq=0, body_text="摘要"
            )
        )
        await _expect_error(session, "ck_conversation_summaries_upto_message_seq_positive")


class TestMemoryConstraints:
    async def test_run_origin_must_exist_but_may_belong_to_another_user(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        """``origin_run_id`` 是**单列**外键：只保证"来源 run 存在"，不保证同用户。

        这条用例同时钉住两件事，避免以后有人误以为数据库还在做同用户校验：

        1. 引用一个**不存在**的 run 必须被外键拒绝；
        2. 引用**别人的** run 会被数据库放行——"来源 run 属于同一用户"是
           service 的职责。之所以不再用 ``(origin_run_id, user_id)`` 复合外键，
           是因为复合 ``ON DELETE SET NULL`` 会把 NOT NULL 的 ``user_id`` 一起置空，
           导致删除 run 永远失败（评审期间在真实库上验证过）。
        """
        owner = make_user(email="mem1@example.com")
        other = make_user(email="mem2@example.com")
        await session.flush()
        conversation = await make_conversation(other.id)
        foreign_run = await make_run(conversation)

        # 1) 不存在的 run 会被外键拒绝。
        # 用 SAVEPOINT 包住：失败会中止整个事务，不隔离的话第 2 步无法继续。
        await session.execute(text("SAVEPOINT memory_probe"))
        with pytest.raises((IntegrityError, DBAPIError, StatementError)) as excinfo:
            await session.execute(
                text(
                    "INSERT INTO memories (user_id, kind, content_text, origin_run_id, version) "
                    "VALUES (:uid, 'fact', '来源不存在', :rid, 0)"
                ).bindparams(uid=owner.id, rid=uuid.uuid4())
            )
        await session.execute(text("ROLLBACK TO SAVEPOINT memory_probe"))
        assert "fk_memories_origin_run_id_runs" in str(excinfo.value)

        # 2) 引用别人的 run 被放行：同用户归属靠 service 校验。
        session.add(
            Memory(
                user_id=owner.id,
                kind="preference",
                content_text="喜欢暗色主题",
                origin_run_id=foreign_run.id,
            )
        )
        await session.flush()

    async def test_manual_memory_without_origin_is_allowed(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """首版仅站长明确要求记住时创建；手工输入的记忆可以没有来源。"""
        user = make_user(email="mem3@example.com")
        await session.flush()
        session.add(Memory(user_id=user.id, kind="fact", content_text="生日是 10 月 5 日"))
        await session.flush()

    async def test_kind_must_be_whitelisted(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="mem4@example.com")
        await session.flush()
        await _expect_error(
            session,
            "ck_memories_kind_valid",
            statement=text(
                "INSERT INTO memories (user_id, kind, content_text, version) "
                "VALUES (:uid, 'profile', 'x', 0)"
            ).bindparams(uid=user.id),
        )

    async def test_empty_content_is_rejected(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="mem5@example.com")
        await session.flush()
        session.add(Memory(user_id=user.id, kind="fact", content_text=""))
        await _expect_error(session, "ck_memories_content_text_not_empty")

    async def test_memory_can_be_soft_deleted(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="mem6@example.com")
        await session.flush()
        memory = Memory(user_id=user.id, kind="preference", content_text="喜欢简洁界面")
        session.add(memory)
        await session.flush()
        await session.execute(
            text("UPDATE memories SET deleted_at = now() WHERE id = :id"), {"id": memory.id}
        )
        await session.refresh(memory)
        assert memory.deleted_at is not None


class TestJobPhaseAndWaitingAuth:
    async def test_waiting_auth_is_representable(self, session: AsyncSession) -> None:
        """需要额外验证但验证过期的任务转 waiting_auth，站长验证后可恢复。"""
        job = await _job(session, idempotency_key="job-waiting-auth", status=JobStatus.WAITING_AUTH)
        assert job.status is JobStatus.WAITING_AUTH
        assert job.lease_token is None

    async def test_job_carries_identity_without_cookie(self, session: AsyncSession) -> None:
        columns = await session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'jobs'::text AND column_name = 'cookie'"
            )
        )
        assert list(columns) == []

    async def test_terminal_job_releases_lease(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="job1@example.com")
        await session.flush()
        job = await _job(
            session,
            idempotency_key="job-terminal",
            actor_id=user.id,
            status=JobStatus.RUNNING,
            phase=JobPhase.FINALIZING,
            lease_token=uuid.uuid4(),
            lease_expires_at=dt.datetime.now(UTC) + dt.timedelta(minutes=5),
        )
        await session.execute(
            text(
                "UPDATE jobs SET status = 'succeeded', lease_token = NULL, "
                "lease_expires_at = NULL, result = '{}'::jsonb WHERE id = :id"
            ),
            {"id": job.id},
        )
        await session.refresh(job)
        assert job.status is JobStatus.SUCCEEDED
        assert job.lease_token is None


class TestCompositeForeignKeyDeleteActions:
    """复合外键的删除动作必须与"列组里有 NOT NULL 列"这件事兼容。

    评审期间在真实库上验证过一类缺陷：外键声明 ``ON DELETE SET NULL``，但列组里
    含 NOT NULL 列。PostgreSQL 的 SET NULL 会把**整个**列组置空，因此该动作永远以
    ``NotNullViolation`` 失败，声明的"解开引用"根本不可能发生。修法按语义二选一：

    - 只是来源/旁证关系（``memories.origin_run_id``）→ 改成**单列** SET NULL；
    - 构成归属的结构关系（``messages.run_id``、``runs.auth_session_id``）→
      改成 ``NO ACTION``，由 service 先摘引用，约束负责拒绝"静默丢归属"。

    每类都配"该成功"与"该被拒"两条用例，避免只验证一半。
    """

    async def test_deleting_run_detaches_the_memory(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        """``origin_run_id`` 是单列外键 + SET NULL：删 run 只解开引用，记忆保留。"""
        user = make_user(email="setnull1@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        memory = Memory(user_id=user.id, kind="fact", content_text="来源记忆", origin_run_id=run.id)
        session.add(memory)
        await session.flush()

        await session.execute(text("DELETE FROM runs WHERE id = :id"), {"id": run.id})
        await session.refresh(memory)
        assert memory.origin_run_id is None

    async def test_deleting_run_is_refused_while_messages_reference_it(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        """``(run_id, conversation_id)`` 是 NO ACTION：归属关系不允许被静默丢掉。"""
        user = make_user(email="setnull2@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        message = Message(
            conversation_id=conversation.id,
            run_id=run.id,
            seq=1,
            role=MessageRole.USER,
            body_text="属于 run 的消息",
        )
        session.add(message)
        await session.flush()

        await _expect_error(
            session,
            "fk_messages_run_id_conversation_id",
            statement=text("DELETE FROM runs WHERE id = :id").bindparams(id=run.id),
        )

    async def test_deleting_run_succeeds_after_messages_are_detached(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
        make_run: Callable[..., Awaitable[Any]],
    ) -> None:
        """service 先摘掉 ``run_id``，约束就不再拦——这是设计期望的删除路径。"""
        user = make_user(email="setnull2b@example.com")
        await session.flush()
        conversation = await make_conversation(user.id)
        run = await make_run(conversation)
        message = Message(
            conversation_id=conversation.id,
            run_id=run.id,
            seq=1,
            role=MessageRole.USER,
            body_text="先摘引用的消息",
        )
        session.add(message)
        await session.flush()

        await session.execute(
            text("UPDATE messages SET run_id = NULL WHERE id = :id"), {"id": message.id}
        )
        await session.execute(text("DELETE FROM runs WHERE id = :id"), {"id": run.id})
        await session.refresh(message)
        assert message.run_id is None
        assert (
            await session.scalar(text("SELECT count(1) FROM runs WHERE id = :id"), {"id": run.id})
            == 0
        )

    async def test_deleting_auth_session_is_refused_while_runs_reference_it(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
    ) -> None:
        """``(auth_session_id, user_id)`` 是 NO ACTION：会话清理不得静默丢归属。

        运维语义：会话的正常生命周期是"撤销"（``revoked_at``）与过期；
        清理作业只能删除**没有被 run 引用**的会话。
        """
        auth_session, _run = await self._session_and_run(session, make_user, make_conversation)

        await _expect_error(
            session,
            "fk_runs_auth_session_id_user_id",
            statement=text("DELETE FROM auth_sessions WHERE id = :id").bindparams(
                id=auth_session.id
            ),
        )

    async def test_deleting_auth_session_succeeds_after_runs_are_detached(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
    ) -> None:
        auth_session, run = await self._session_and_run(session, make_user, make_conversation)

        await session.execute(
            text("UPDATE runs SET auth_session_id = NULL WHERE id = :id"), {"id": run.id}
        )
        await session.execute(
            text("DELETE FROM auth_sessions WHERE id = :id"), {"id": auth_session.id}
        )
        await session.refresh(run)
        assert run.auth_session_id is None
        assert (
            await session.scalar(
                text("SELECT count(1) FROM auth_sessions WHERE id = :id"), {"id": auth_session.id}
            )
            == 0
        )

    async def _session_and_run(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_conversation: Callable[..., Awaitable[Any]],
    ) -> tuple[Any, Any]:
        """建一个用户 + 授权会话 + 引用它的 run。"""
        user = make_user(email=f"setnull3-{uuid.uuid4().hex[:8]}@example.com")
        await session.flush()
        now = dt.datetime.now(UTC)
        auth_session = AuthSession(
            user_id=user.id,
            token_hash="s" * 64,
            auth_version=user.auth_version,
            idle_expires_at=now + dt.timedelta(hours=1),
            absolute_expires_at=now + dt.timedelta(days=1),
        )
        session.add(auth_session)
        await session.flush()
        conversation = await make_conversation(user.id)
        run = Run(
            user_id=user.id,
            conversation_id=conversation.id,
            auth_session_id=auth_session.id,
            idempotency_key=f"key-{uuid.uuid4().hex[:16]}",
            request_hash="r" * 64,
            checkpoint_thread_id=f"thread-{uuid.uuid4().hex[:8]}",
        )
        session.add(run)
        await session.flush()
        return auth_session, run

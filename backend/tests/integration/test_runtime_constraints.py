"""A6 第三批表：真实 PostgreSQL 约束验收。

重点是实施顺序表点名的密集约束：
``runs`` 幂等与非终态唯一、``next_event_seq`` 原子分配、``quota`` 的
``UNIQUE``/``CHECK``、``provider_calls`` 的 attempt 唯一与状态机、
``jobs`` 的 lease 一致性与活跃去重、``actions`` 的幂等身份。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from autumn_backend.db.enums import (
    NON_TERMINAL_RUN_STATUSES,
    ActionKind,
    ActionStatus,
    ConversationMode,
    EventType,
    JobStatus,
    MessageRole,
    ProviderCallPurpose,
    ProviderCallStatus,
    QuotaReservationStatus,
    RunStatus,
)
from autumn_backend.db.models import (
    Action,
    AuditEvent,
    Conversation,
    Job,
    Message,
    ProviderCall,
    QuotaBucket,
    QuotaReservation,
    Run,
    RunEvent,
)

pytestmark = pytest.mark.integration

UTC = dt.UTC


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


async def _make_conversation(
    session: AsyncSession, owner_id: uuid.UUID, **overrides: Any
) -> Conversation:
    conversation = Conversation(
        user_id=owner_id,
        mode=overrides.pop("mode", ConversationMode.PUBLIC),
        thread_id=overrides.pop("thread_id", f"thread-{uuid.uuid4()}"),
        **overrides,
    )
    session.add(conversation)
    await session.flush()
    return conversation


async def _make_run(
    session: AsyncSession,
    owner_id: uuid.UUID,
    conversation: Conversation,
    **overrides: Any,
) -> Run:
    run = Run(
        user_id=owner_id,
        conversation_id=conversation.id,
        idempotency_key=overrides.pop("idempotency_key", f"key-{uuid.uuid4()}"),
        request_hash=overrides.pop("request_hash", "r" * 64),
        mode=overrides.pop("mode", ConversationMode.PUBLIC),
        **overrides,
    )
    session.add(run)
    await session.flush()
    return run


class TestSchemaObjectsExist:
    async def test_new_tables_present(self, session: AsyncSession) -> None:
        rows = await session.execute(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename = ANY(:names)"
            ),
            {
                "names": [
                    "conversations",
                    "messages",
                    "runs",
                    "run_events",
                    "quota_buckets",
                    "quota_reservations",
                    "jobs",
                    "provider_calls",
                    "audit_events",
                    "actions",
                ]
            },
        )
        assert len(list(rows)) == 10

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
        for name in (
            "conversations",
            "messages",
            "runs",
            "run_events",
            "quota_buckets",
            "quota_reservations",
            "jobs",
            "provider_calls",
            "audit_events",
            "actions",
        ):
            assert found.get(name) == "O", f"{name} 缺少已启用的 updated_at 触发器"


class TestRunConstraints:
    async def test_idempotency_key_unique_per_user(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="run1@example.com")
        await session.flush()
        first = await _make_conversation(session, user.id)
        second = await _make_conversation(session, user.id)

        await _make_run(session, user.id, first, idempotency_key="same-key")
        session.add(
            Run(
                user_id=user.id,
                conversation_id=second.id,
                idempotency_key="same-key",
                request_hash="r" * 64,
                mode=ConversationMode.PUBLIC,
            )
        )
        await _expect_error(session, "uq_runs_user_id_idempotency_key")

    async def test_same_key_allowed_for_different_users(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        first = make_user(email="run2@example.com")
        second = make_user(email="run3@example.com")
        await session.flush()
        for user in (first, second):
            conversation = await _make_conversation(session, user.id)
            await _make_run(session, user.id, conversation, idempotency_key="shared-key")

    async def test_only_one_non_terminal_run_per_conversation(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="run4@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id)
        await _make_run(session, user.id, conversation, status=RunStatus.RUNNING)

        session.add(
            Run(
                user_id=user.id,
                conversation_id=conversation.id,
                idempotency_key=f"key-{uuid.uuid4()}",
                request_hash="r" * 64,
                mode=ConversationMode.PUBLIC,
                status=RunStatus.PENDING,
            )
        )
        await _expect_error(session, "uq_runs_conversation_id_non_terminal")

    async def test_terminal_run_frees_the_conversation(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """旧 run 进入终态后，同一会话可以再开新 run。"""
        user = make_user(email="run5@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id)
        first = await _make_run(session, user.id, conversation, status=RunStatus.RUNNING)

        await session.execute(
            text(
                "UPDATE runs SET status = 'succeeded', finished_at = now(), "
                "terminal_reason = 'completed' WHERE id = :id"
            ),
            {"id": first.id},
        )
        await _make_run(session, user.id, conversation, status=RunStatus.PENDING)

    async def test_finished_at_must_match_terminal_status(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """终态必须带 finished_at：用原生 SQL 绕过 Python 侧校验。"""
        user = make_user(email="run6@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id)

        await _expect_error(
            session,
            "ck_runs_finished_at_matches_terminal_status",
            statement=text(
                "INSERT INTO runs (user_id, conversation_id, idempotency_key, request_hash, "
                "status, mode, model_profile, next_event_seq, context_generation, "
                "acl_epoch_at_start, steps_used, token_used, cost_micro_usd, version, "
                "created_at, updated_at) VALUES (:uid, :cid, 'x', :rh, 'succeeded', 'public', "
                "'primary', 1, 1, 0, 0, 0, 0, 0, now(), now())"
            ).bindparams(uid=user.id, cid=conversation.id, rh="r" * 64),
        )

    async def test_non_terminal_run_must_not_have_finished_at(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="run9@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id)

        await _expect_error(
            session,
            "ck_runs_finished_at_matches_terminal_status",
            statement=text(
                "INSERT INTO runs (user_id, conversation_id, idempotency_key, request_hash, "
                "status, mode, model_profile, next_event_seq, context_generation, "
                "acl_epoch_at_start, steps_used, token_used, cost_micro_usd, finished_at, "
                "version, created_at, updated_at) "
                "VALUES (:uid, :cid, 'y', :rh, 'running', 'public', 'primary', 1, 1, 0, 0, 0, 0, "
                "now(), 0, now(), now())"
            ).bindparams(uid=user.id, cid=conversation.id, rh="r" * 64),
        )

    async def test_next_event_seq_is_allocated_atomically(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """``UPDATE ... RETURNING next_event_seq - 1`` 是唯一的 seq 分配方式。"""
        user = make_user(email="run7@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id)
        run = await _make_run(session, user.id, conversation)
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
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="run8@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id)
        run = await _make_run(session, user.id, conversation)
        await _expect_error(
            session,
            "ck_runs_next_event_seq_positive",
            statement=text("UPDATE runs SET next_event_seq = 0 WHERE id = :id").bindparams(
                id=run.id
            ),
        )


class TestRunEventConstraints:
    async def test_run_and_seq_is_the_identity(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="ev1@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id)
        run = await _make_run(session, user.id, conversation)

        session.add(
            RunEvent(run_id=run.id, seq=0, event_type=EventType.RUN_STARTED, payload={"a": 1})
        )
        await session.flush()

        session.add(
            RunEvent(run_id=run.id, seq=0, event_type=EventType.MESSAGE_DELTA, payload={"b": 2})
        )
        await _expect_error(session, "pk_run_events")

    async def test_same_seq_allowed_for_different_runs(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="ev2@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id)
        first = await _make_run(session, user.id, conversation)
        await session.execute(
            text("UPDATE runs SET status = 'succeeded', finished_at = now() WHERE id = :id"),
            {"id": first.id},
        )
        second = await _make_run(session, user.id, conversation)

        for run in (first, second):
            session.add(
                RunEvent(run_id=run.id, seq=0, event_type=EventType.RUN_STARTED, payload=None)
            )
        await session.flush()

    async def test_event_type_must_be_whitelisted(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="ev3@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id)
        run = await _make_run(session, user.id, conversation)

        await _expect_error(
            session,
            "ck_run_events_event_type_valid",
            statement=text(
                "INSERT INTO run_events (run_id, seq, event_type, created_at, updated_at) "
                "VALUES (:rid, 0, 'run.exploded', now(), now())"
            ).bindparams(rid=run.id),
        )

    async def test_deleting_run_cascades_to_events(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="ev4@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id)
        run = await _make_run(session, user.id, conversation)
        session.add(RunEvent(run_id=run.id, seq=0, event_type=EventType.RUN_STARTED, payload=None))
        await session.flush()

        await session.execute(text("DELETE FROM runs WHERE id = :id").bindparams(id=run.id))
        remaining = await session.scalar(
            text("SELECT count(*) FROM run_events WHERE run_id = :rid"), {"rid": run.id}
        )
        assert remaining == 0


class TestMessageConstraints:
    async def test_seq_unique_per_conversation(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="msg1@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id)

        session.add(
            Message(
                conversation_id=conversation.id,
                role=MessageRole.USER,
                seq=1,
                content="你好",
                author_id=user.id,
            )
        )
        await session.flush()

        session.add(
            Message(
                conversation_id=conversation.id,
                role=MessageRole.USER,
                seq=1,
                content="重复序号",
                author_id=user.id,
            )
        )
        await _expect_error(session, "uq_messages_conversation_seq")

    async def test_tool_name_must_match_role(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="msg2@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id)

        # user 消息带 tool_name：不允许。
        session.add(
            Message(
                conversation_id=conversation.id,
                role=MessageRole.USER,
                seq=1,
                content="x",
                tool_name="search",
                author_id=user.id,
            )
        )
        await _expect_error(session, "ck_messages_tool_name_matches_role")

    async def test_tool_message_without_tool_name_is_rejected(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="msg3@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id)

        session.add(
            Message(
                conversation_id=conversation.id,
                role=MessageRole.TOOL,
                seq=1,
                content="x",
            )
        )
        await _expect_error(session, "ck_messages_tool_name_matches_role")

    async def test_tool_message_round_trips_payload(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="msg4@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id)
        message = Message(
            conversation_id=conversation.id,
            role=MessageRole.TOOL,
            seq=1,
            content="检索结果",
            tool_name="search_public",
            tool_payload={"hits": 3},
        )
        session.add(message)
        await session.flush()
        await session.refresh(message)
        assert message.tool_payload == {"hits": 3}


class TestQuotaConstraints:
    async def test_bucket_window_unique_per_user(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="q1@example.com")
        await session.flush()
        window = dt.datetime(2026, 3, 1, 16, 0, tzinfo=UTC)  # Asia/Shanghai 当天 00:00

        session.add(
            QuotaBucket(user_id=user.id, window_start=window, limit_value=10, used=0, reserved=0)
        )
        await session.flush()

        session.add(
            QuotaBucket(user_id=user.id, window_start=window, limit_value=10, used=0, reserved=0)
        )
        await _expect_error(session, "uq_quota_buckets_user_window")

    async def test_counters_cannot_be_negative(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="q2@example.com")
        await session.flush()
        window = dt.datetime(2026, 3, 2, 16, 0, tzinfo=UTC)

        session.add(
            QuotaBucket(user_id=user.id, window_start=window, limit_value=10, used=0, reserved=0)
        )
        await session.flush()

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
        window = dt.datetime(2026, 3, 3, 16, 0, tzinfo=UTC)
        session.add(
            QuotaBucket(user_id=user.id, window_start=window, limit_value=10, used=0, reserved=0)
        )
        await session.flush()

        await _expect_error(
            session,
            "ck_quota_buckets_reserved_non_negative",
            statement=text(
                "UPDATE quota_buckets SET reserved = -1 WHERE user_id = :uid"
            ).bindparams(uid=user.id),
        )

    async def test_usage_cannot_exceed_limit(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """并发受理不突破日额度的数据库兜底：used + reserved <= limit_value。"""
        user = make_user(email="q4@example.com")
        await session.flush()
        window = dt.datetime(2026, 3, 4, 16, 0, tzinfo=UTC)
        session.add(
            QuotaBucket(user_id=user.id, window_start=window, limit_value=10, used=7, reserved=3)
        )
        await session.flush()

        await _expect_error(
            session,
            "ck_quota_buckets_usage_within_limit",
            statement=text(
                "UPDATE quota_buckets SET reserved = reserved + 1 WHERE user_id = :uid"
            ).bindparams(uid=user.id),
        )

    async def test_reservation_run_id_is_unique(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="q5@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id)
        run = await _make_run(session, user.id, conversation)
        bucket = QuotaBucket(
            user_id=user.id,
            window_start=dt.datetime(2026, 3, 5, 16, 0, tzinfo=UTC),
            limit_value=10,
        )
        session.add(bucket)
        await session.flush()

        session.add(QuotaReservation(run_id=run.id, bucket_id=bucket.id, user_id=user.id))
        await session.flush()

        session.add(QuotaReservation(run_id=run.id, bucket_id=bucket.id, user_id=user.id))
        await _expect_error(session, "uq_quota_reservations_run_id")

    async def test_reservation_amount_must_be_one(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="q6@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id)
        run = await _make_run(session, user.id, conversation)
        bucket = QuotaBucket(
            user_id=user.id,
            window_start=dt.datetime(2026, 3, 6, 16, 0, tzinfo=UTC),
            limit_value=10,
        )
        session.add(bucket)
        await session.flush()

        await _expect_error(
            session,
            "ck_quota_reservations_amount_is_one",
            statement=text(
                "INSERT INTO quota_reservations (run_id, bucket_id, user_id, amount, status, "
                "version, created_at, updated_at) "
                "VALUES (:rid, :bid, :uid, 2, 'reserved', 0, now(), now())"
            ).bindparams(rid=run.id, bid=bucket.id, uid=user.id),
        )

    async def test_reservation_status_machine_values(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="q7@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id)
        run = await _make_run(session, user.id, conversation)
        bucket = QuotaBucket(
            user_id=user.id,
            window_start=dt.datetime(2026, 3, 7, 16, 0, tzinfo=UTC),
            limit_value=10,
        )
        session.add(bucket)
        await session.flush()

        await _expect_error(
            session,
            "ck_quota_reservations_status_valid",
            statement=text(
                "INSERT INTO quota_reservations (run_id, bucket_id, user_id, amount, status, "
                "version, created_at, updated_at) "
                "VALUES (:rid, :bid, :uid, 1, 'pending', 0, now(), now())"
            ).bindparams(rid=run.id, bid=bucket.id, uid=user.id),
        )
        assert QuotaReservationStatus.CHARGED.value == "charged"

    async def test_run_pins_its_bucket(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """run 固定归属受理时的 bucket：跨午夜恢复不换桶。"""
        user = make_user(email="q8@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id)
        bucket = QuotaBucket(
            user_id=user.id,
            window_start=dt.datetime(2026, 3, 8, 16, 0, tzinfo=UTC),
            limit_value=10,
        )
        session.add(bucket)
        await session.flush()

        run = await _make_run(session, user.id, conversation, quota_bucket_id=bucket.id)
        await session.refresh(run)
        assert run.quota_bucket_id == bucket.id


class TestJobConstraints:
    async def test_lease_fields_must_match_status(self, session: AsyncSession) -> None:
        await _expect_error(
            session,
            "ck_jobs_lease_fields_match_status",
            statement=text(
                "INSERT INTO jobs (job_type, status, attempts, max_attempts, priority, "
                "available_at, version, created_at, updated_at) "
                "VALUES ('run.dispatch', 'running', 1, 5, 0, now(), 0, now(), now())"
            ).bindparams(),
        )

    async def test_queued_job_cannot_carry_lease(self, session: AsyncSession) -> None:
        await _expect_error(
            session,
            "ck_jobs_lease_fields_match_status",
            statement=text(
                "INSERT INTO jobs (job_type, status, lease_token, lease_expires_at, attempts, "
                "max_attempts, priority, available_at, version, created_at, updated_at) "
                "VALUES ('run.dispatch', 'queued', gen_random_uuid(), now() + interval '1 hour', "
                "0, 5, 0, now(), 0, now(), now())"
            ),
        )

    async def test_running_job_round_trip(self, session: AsyncSession) -> None:
        job = Job(
            job_type="run.dispatch",
            status=JobStatus.RUNNING,
            lease_token=uuid.uuid4(),
            lease_expires_at=dt.datetime.now(UTC) + dt.timedelta(minutes=5),
            attempts=1,
        )
        session.add(job)
        await session.flush()
        assert job.lease_token is not None

    async def test_finished_at_must_match_terminal_status(self, session: AsyncSession) -> None:
        await _expect_error(
            session,
            "ck_jobs_finished_at_matches_terminal_status",
            statement=text(
                "INSERT INTO jobs (job_type, status, attempts, max_attempts, priority, "
                "available_at, version, created_at, updated_at) "
                "VALUES ('run.dispatch', 'succeeded', 1, 5, 0, now(), 0, now(), now())"
            ),
        )

    async def test_active_dedupe_blocks_second_active_job(self, session: AsyncSession) -> None:
        session.add(Job(job_type="storage.finalize", dedupe_key="file-1", status=JobStatus.QUEUED))
        await session.flush()

        session.add(Job(job_type="storage.finalize", dedupe_key="file-1", status=JobStatus.QUEUED))
        await _expect_error(session, "uq_jobs_type_dedupe_active")

    async def test_terminal_job_frees_the_dedupe_slot(self, session: AsyncSession) -> None:
        session.add(Job(job_type="storage.delete", dedupe_key="file-2", status=JobStatus.QUEUED))
        await session.flush()

        await session.execute(
            text(
                "UPDATE jobs SET status = 'succeeded', finished_at = now() "
                "WHERE dedupe_key = 'file-2'"
            )
        )
        session.add(Job(job_type="storage.delete", dedupe_key="file-2", status=JobStatus.QUEUED))
        await session.flush()

    async def test_multiple_jobs_without_dedupe_key_are_allowed(
        self, session: AsyncSession
    ) -> None:
        """dedupe_key 为 NULL 时不受活跃去重约束。"""
        for _ in range(3):
            session.add(Job(job_type="knowledge.index", status=JobStatus.QUEUED))
        await session.flush()

    async def test_claim_query_shape_works(self, session: AsyncSession) -> None:
        """SKIP LOCKED 取任务：真实 PostgreSQL 上可执行。"""
        picked = await session.scalar(
            text(
                "SELECT id FROM jobs WHERE status = 'queued' AND available_at <= now() "
                "ORDER BY priority DESC, available_at, id "
                "FOR UPDATE SKIP LOCKED LIMIT 1"
            )
        )
        assert picked is None or isinstance(picked, uuid.UUID)


class TestProviderCallConstraints:
    async def test_attempt_identity_is_unique(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="pc1@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id)
        run = await _make_run(session, user.id, conversation)
        job = Job(job_type="run.dispatch", run_id=run.id, user_id=user.id)
        session.add(job)
        await session.flush()

        session.add(
            ProviderCall(
                job_id=job.id,
                run_id=run.id,
                provider="deepseek",
                purpose=ProviderCallPurpose.CHAT,
                attempt_no=1,
                status=ProviderCallStatus.PREPARED,
            )
        )
        await session.flush()

        session.add(
            ProviderCall(
                job_id=job.id,
                run_id=run.id,
                provider="deepseek",
                purpose=ProviderCallPurpose.CHAT,
                attempt_no=1,
                status=ProviderCallStatus.PREPARED,
            )
        )
        await _expect_error(session, "uq_provider_calls_job_purpose_attempt")

    async def test_prepared_cannot_have_dispatch_time(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="pc2@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id)
        run = await _make_run(session, user.id, conversation)

        await _expect_error(
            session,
            "ck_provider_calls_prepared_not_dispatched",
            statement=text(
                "INSERT INTO provider_calls (run_id, provider, purpose, attempt_no, status, "
                "dispatched_at, version, created_at, updated_at) "
                "VALUES (:rid, 'p', 'chat', 1, 'prepared', now(), 0, now(), now())"
            ).bindparams(rid=run.id),
        )

    async def test_settled_at_required_for_terminal_states(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="pc3@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id)
        run = await _make_run(session, user.id, conversation)

        await _expect_error(
            session,
            "ck_provider_calls_settled_at_matches_status",
            statement=text(
                "INSERT INTO provider_calls (run_id, provider, purpose, attempt_no, status, "
                "dispatched_at, version, created_at, updated_at) "
                "VALUES (:rid, 'p', 'chat', 1, 'succeeded', now(), 0, now(), now())"
            ).bindparams(rid=run.id),
        )

    async def test_unknown_is_representable(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """unknown 是独立状态：不能当作"失败且零成本"，因此允许无成本但有 settled_at。"""
        user = make_user(email="pc4@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id)
        run = await _make_run(session, user.id, conversation)
        call = ProviderCall(
            run_id=run.id,
            provider="deepseek",
            purpose=ProviderCallPurpose.CHAT,
            attempt_no=1,
            status=ProviderCallStatus.UNKNOWN,
            dispatched_at=dt.datetime.now(UTC),
            settled_at=dt.datetime.now(UTC),
        )
        session.add(call)
        await session.flush()
        assert call.cost_micro_usd is None

    async def test_call_must_be_traceable(self, session: AsyncSession) -> None:
        await _expect_error(
            session,
            "ck_provider_calls_traceable_target",
            statement=text(
                "INSERT INTO provider_calls (provider, purpose, attempt_no, status, version, "
                "created_at, updated_at) VALUES ('p', 'chat', 1, 'prepared', 0, now(), now())"
            ).bindparams(),
        )

    async def test_deleting_job_keeps_cost_record(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """成本记录不能因为 job 被清理而消失：外键是 SET NULL。"""
        user = make_user(email="pc6@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id)
        run = await _make_run(session, user.id, conversation)
        job = Job(job_type="run.dispatch", run_id=run.id, user_id=user.id)
        session.add(job)
        await session.flush()
        session.add(
            ProviderCall(
                job_id=job.id,
                run_id=run.id,
                provider="deepseek",
                purpose=ProviderCallPurpose.CHAT,
                attempt_no=1,
            )
        )
        await session.flush()

        await session.execute(text("DELETE FROM jobs WHERE id = :id").bindparams(id=job.id))
        remaining = await session.scalar(
            text("SELECT count(*) FROM provider_calls WHERE run_id = :rid"), {"rid": run.id}
        )
        assert remaining == 1
        orphaned = await session.scalar(
            text("SELECT job_id FROM provider_calls WHERE run_id = :rid"), {"rid": run.id}
        )
        assert orphaned is None


class TestAuditEventConstraints:
    async def test_versions_are_non_negative(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="a1@example.com")
        await session.flush()
        session.add(
            AuditEvent(action="resource.edit", actor_id=user.id, before_version=1, after_version=2)
        )
        await session.flush()

        await _expect_error(
            session,
            "ck_audit_events_after_version_non_negative",
            statement=text(
                "INSERT INTO audit_events (action, before_version, after_version, created_at, "
                "updated_at) VALUES ('resource.edit', 1, -1, now(), now())"
            ),
        )

    async def test_deleting_actor_keeps_the_audit_row(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """删账号不能连审计一起删掉：actor_id 是 SET NULL。"""
        user = make_user(email="a2@example.com")
        await session.flush()
        session.add(AuditEvent(action="auth.login", actor_id=user.id))
        await session.flush()

        await session.execute(text("DELETE FROM users WHERE id = :id").bindparams(id=user.id))
        remaining = await session.scalar(
            text(
                "SELECT count(*) FROM audit_events WHERE action = 'auth.login' AND actor_id IS NULL"
            )
        )
        assert remaining == 1

    async def test_metadata_holds_acl_values_not_version_fields(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """ACL 前后值写 metadata，不占用 before/after_version。"""
        user = make_user(email="a3@example.com")
        await session.flush()
        event = AuditEvent(
            action="publication.publish",
            actor_id=user.id,
            before_version=None,
            after_version=None,
            metadata_json={"acl_version_before": 0, "acl_version_after": 1},
        )
        session.add(event)
        await session.flush()
        await session.refresh(event)
        assert event.before_version is None
        assert event.metadata_json == {"acl_version_before": 0, "acl_version_after": 1}

    async def test_action_must_not_be_empty(self, session: AsyncSession) -> None:
        await _expect_error(
            session,
            "ck_audit_events_action_not_empty",
            statement=text(
                "INSERT INTO audit_events (action, created_at, updated_at) "
                "VALUES ('', now(), now())"
            ),
        )


class TestActionConstraints:
    async def test_idempotency_identity_blocks_duplicates(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="ac1@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id, mode=ConversationMode.OWNER)
        run = await _make_run(session, user.id, conversation, mode=ConversationMode.OWNER)
        target = uuid.uuid4()
        expires = dt.datetime.now(UTC) + dt.timedelta(hours=1)

        def build() -> Action:
            return Action(
                run_id=run.id,
                actor_id=user.id,
                kind=ActionKind.PUBLISH,
                target_type="resource",
                target_id=target,
                target_version=1,
                args_summary={"visibility": "public"},
                args_hash="h" * 64,
                expires_at=expires,
            )

        session.add(build())
        await session.flush()
        session.add(build())
        await _expect_error(session, "uq_actions_idempotency_identity")

    async def test_different_args_hash_is_a_different_action(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="ac2@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id, mode=ConversationMode.OWNER)
        run = await _make_run(session, user.id, conversation, mode=ConversationMode.OWNER)
        target = uuid.uuid4()
        expires = dt.datetime.now(UTC) + dt.timedelta(hours=1)

        for index, args_hash in enumerate(("a" * 64, "b" * 64)):
            session.add(
                Action(
                    run_id=run.id,
                    actor_id=user.id,
                    kind=ActionKind.PUBLISH,
                    target_type="resource",
                    target_id=target,
                    target_version=1,
                    args_summary={"variant": index},
                    args_hash=args_hash,
                    expires_at=expires,
                )
            )
        await session.flush()

    async def test_status_whitelist(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="ac3@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id, mode=ConversationMode.OWNER)
        run = await _make_run(session, user.id, conversation, mode=ConversationMode.OWNER)

        await _expect_error(
            session,
            "ck_actions_status_valid",
            statement=text(
                "INSERT INTO actions (run_id, actor_id, kind, target_type, args_summary, "
                "args_hash, status, expires_at, version, created_at, updated_at) "
                "VALUES (:rid, :aid, 'publish', 'resource', '{}'::jsonb, :h, 'waiting', "
                "now() + interval '1 hour', 0, now(), now())"
            ).bindparams(rid=run.id, aid=user.id, h="h" * 64),
        )

    async def test_executed_requires_confirmation(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """执行态必须带 confirmed_at：不能跳过确认直接执行。"""
        user = make_user(email="ac4@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id, mode=ConversationMode.OWNER)
        run = await _make_run(session, user.id, conversation, mode=ConversationMode.OWNER)

        await _expect_error(
            session,
            "ck_actions_confirmed_at_matches_status",
            statement=text(
                "INSERT INTO actions (run_id, actor_id, kind, target_type, args_summary, "
                "args_hash, status, expires_at, executed_at, version, created_at, updated_at) "
                "VALUES (:rid, :aid, 'publish', 'resource', '{}'::jsonb, :h, 'executed', "
                "now() + interval '1 hour', now(), 0, now(), now())"
            ).bindparams(rid=run.id, aid=user.id, h="h" * 64),
        )

    async def test_confirmed_then_executed_round_trip(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """正常流程：pending -> confirmed -> executed，两个时刻共存。"""
        user = make_user(email="ac7@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id, mode=ConversationMode.OWNER)
        run = await _make_run(session, user.id, conversation, mode=ConversationMode.OWNER)

        action = Action(
            run_id=run.id,
            actor_id=user.id,
            kind=ActionKind.PUBLISH,
            target_type="resource",
            args_summary={},
            args_hash="e" * 64,
            expires_at=dt.datetime.now(UTC) + dt.timedelta(hours=1),
        )
        session.add(action)
        await session.flush()
        assert action.status is ActionStatus.PENDING

        confirmed = dt.datetime.now(UTC)
        await session.execute(
            text(
                "UPDATE actions SET status = 'confirmed', confirmed_at = :ts, "
                "version = version + 1 WHERE id = :id"
            ),
            {"ts": confirmed, "id": action.id},
        )
        await session.execute(
            text(
                "UPDATE actions SET status = 'executed', executed_at = :ts, "
                "version = version + 1 WHERE id = :id"
            ),
            {"ts": dt.datetime.now(UTC), "id": action.id},
        )
        await session.refresh(action)
        assert action.status is ActionStatus.EXECUTED
        assert action.confirmed_at is not None
        assert action.executed_at is not None

    async def test_pending_action_must_not_carry_any_transition_timestamp(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="ac8@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id, mode=ConversationMode.OWNER)
        run = await _make_run(session, user.id, conversation, mode=ConversationMode.OWNER)

        await _expect_error(
            session,
            "ck_actions_rejected_at_matches_status",
            statement=text(
                "INSERT INTO actions (run_id, actor_id, kind, target_type, args_summary, "
                "args_hash, status, expires_at, rejected_at, version, created_at, updated_at) "
                "VALUES (:rid, :aid, 'publish', 'resource', '{}'::jsonb, :h, 'pending', "
                "now() + interval '1 hour', now(), 0, now(), now())"
            ).bindparams(rid=run.id, aid=user.id, h="h" * 64),
        )

    async def test_expired_action_records_expired_at(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="ac9@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id, mode=ConversationMode.OWNER)
        run = await _make_run(session, user.id, conversation, mode=ConversationMode.OWNER)

        action = Action(
            run_id=run.id,
            actor_id=user.id,
            kind=ActionKind.DELETE,
            target_type="resource",
            args_summary={},
            args_hash="f" * 64,
            status=ActionStatus.EXPIRED,
            expires_at=dt.datetime.now(UTC) + dt.timedelta(hours=1),
            expired_at=dt.datetime.now(UTC),
        )
        session.add(action)
        await session.flush()

    async def test_expiry_must_be_after_creation(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="ac5@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id, mode=ConversationMode.OWNER)
        run = await _make_run(session, user.id, conversation, mode=ConversationMode.OWNER)

        await _expect_error(
            session,
            "ck_actions_expires_after_created",
            statement=text(
                "INSERT INTO actions (run_id, actor_id, kind, target_type, args_summary, "
                "args_hash, status, expires_at, version, created_at, updated_at) "
                "VALUES (:rid, :aid, 'publish', 'resource', '{}'::jsonb, :h, 'pending', "
                "now() - interval '1 hour', 0, now(), now())"
            ).bindparams(rid=run.id, aid=user.id, h="h" * 64),
        )

    async def test_lifecycle_round_trip(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="ac6@example.com")
        await session.flush()
        conversation = await _make_conversation(session, user.id, mode=ConversationMode.OWNER)
        run = await _make_run(session, user.id, conversation, mode=ConversationMode.OWNER)
        action = Action(
            run_id=run.id,
            actor_id=user.id,
            kind=ActionKind.DELETE,
            target_type="resource",
            args_summary={"confirm": True},
            args_hash="d" * 64,
            expires_at=dt.datetime.now(UTC) + dt.timedelta(hours=2),
        )
        session.add(action)
        await session.flush()
        assert action.status is ActionStatus.PENDING

        await session.execute(
            text(
                "UPDATE actions SET status = 'confirmed', confirmed_at = now(), "
                "version = version + 1 WHERE id = :id"
            ),
            {"id": action.id},
        )
        await session.refresh(action)
        assert action.status is ActionStatus.CONFIRMED
        assert action.confirmed_at is not None

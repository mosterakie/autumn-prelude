"""真实 SQLSTATE 映射和失败事务恢复边界。"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from autumn_backend.db.enums import JobStatus
from autumn_backend.db.models import Job
from autumn_backend.errors import ConflictError, InvalidInputError
from autumn_backend.repositories.base import ControlledMutableRepository
from autumn_backend.repositories.constraints import ActiveRunConflictError, database_errors

pytestmark = pytest.mark.integration


class JobStateProbe(ControlledMutableRepository[Job]):
    model = Job


async def test_state_cas_rejects_repeated_or_invalid_transition(session: AsyncSession) -> None:
    job = Job(kind="run.execute", idempotency_key="state-probe")
    session.add(job)
    await session.flush()
    repository = JobStateProbe(session)
    changed = await repository._transition(job.id, JobStatus.QUEUED, JobStatus.CANCELLED, {})
    assert changed.status == JobStatus.CANCELLED
    with pytest.raises(ConflictError):
        await repository._transition(job.id, JobStatus.QUEUED, JobStatus.CANCELLED, {})
    with pytest.raises(InvalidInputError):
        await repository._transition(job.id, JobStatus.CANCELLED, JobStatus.QUEUED, {"id": job.id})


async def test_unique_errors_allow_explicit_savepoint_recovery(
    session: AsyncSession, make_user: object
) -> None:
    make_user(email="private-secret@example.com")
    await session.flush()
    with pytest.raises(ConflictError) as caught:
        async with session.begin_nested():
            with database_errors():
                make_user(email="private-secret@example.com")
                await session.flush()
    assert "private-secret" not in str(caught.value)
    assert await session.scalar(text("SELECT 1")) == 1


async def test_failed_statement_requires_rollback(session: AsyncSession) -> None:
    with pytest.raises(InvalidInputError):
        with database_errors():
            await session.execute(text("UPDATE settings SET version = -1"))
    with pytest.raises(DBAPIError) as caught:
        await session.execute(text("SELECT 1"))
    assert caught.value.orig.sqlstate == "25P02"
    await session.rollback()


async def test_active_run_constraint_has_own_domain_error(
    session: AsyncSession, make_user: object, make_conversation: object, make_run: object
) -> None:
    user = make_user()
    await session.flush()
    conversation = await make_conversation(user.id)
    await make_run(conversation)
    with pytest.raises(ActiveRunConflictError):
        async with session.begin_nested():
            with database_errors():
                await make_run(conversation)


async def test_fk_maps_conflict(session: AsyncSession) -> None:
    with pytest.raises(ConflictError):
        async with session.begin_nested():
            with database_errors():
                await session.execute(
                    text(
                        "INSERT INTO conversations (user_id, mode, title, version, next_message_seq) "
                        "VALUES (gen_random_uuid(), 'public', '', 0, 1)"
                    )
                )

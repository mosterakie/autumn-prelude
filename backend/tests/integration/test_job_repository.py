from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from autumn_backend.db.enums import JobStatus
from autumn_backend.db.models import Job
from autumn_backend.errors import ConflictError, LeaseLostError
from autumn_backend.repositories.jobs import JobRepository, JobSpec

pytestmark = pytest.mark.integration


async def test_enqueue_replay_and_different_payload(session: AsyncSession) -> None:
    repository = JobRepository(session)
    spec = JobSpec(kind="run.execute", idempotency_key="enqueue-1", payload={"request": "one"})
    first = await repository.enqueue(spec)
    assert first.created
    assert not (await repository.enqueue(spec)).created
    with pytest.raises(ConflictError):
        await repository.enqueue(
            JobSpec(
                kind=spec.kind, idempotency_key=spec.idempotency_key, payload={"request": "two"}
            )
        )


async def test_claim_heartbeat_finish_and_stale_tokens(session: AsyncSession) -> None:
    repository = JobRepository(session)
    await repository.enqueue(JobSpec(kind="run.execute", idempotency_key="lease-1", payload={}))
    job = await repository.claim(lease_seconds=60)
    assert job is not None and job.lease_token is not None and job.attempts == 1
    token = job.lease_token
    assert await repository.claim() is None
    with pytest.raises(LeaseLostError):
        await repository.heartbeat(job.id, uuid4())
    await repository.heartbeat(job.id, token)
    await repository.finish(job.id, token, result={"run_id": "one"})
    assert job.status == JobStatus.SUCCEEDED and job.lease_token is None
    with pytest.raises(LeaseLostError):
        await repository.finish(job.id, token)


async def test_expired_lease_cannot_be_revived_or_finished(session: AsyncSession) -> None:
    repository = JobRepository(session)
    await repository.enqueue(JobSpec(kind="run.execute", idempotency_key="lease-2", payload={}))
    job = await repository.claim()
    assert job is not None and job.lease_token is not None
    token = job.lease_token
    await session.execute(
        update(Job)
        .where(Job.id == job.id)
        .values(lease_expires_at=func.clock_timestamp() - timedelta(seconds=1))
    )
    with pytest.raises(LeaseLostError):
        await repository.heartbeat(job.id, token)
    with pytest.raises(LeaseLostError):
        await repository.finish(job.id, token)
    assert (await repository.get_for_update_or_raise(job.id)).status == JobStatus.RUNNING


async def test_future_job_not_claimed(session: AsyncSession) -> None:
    job = Job(
        kind="run.execute",
        idempotency_key="future",
        available_at=func.clock_timestamp() + timedelta(days=1),
    )
    session.add(job)
    await session.flush()
    assert await JobRepository(session).claim() is None

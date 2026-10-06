from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, update

from autumn_backend.db.enums import JobStatus
from autumn_backend.db.models import Job
from autumn_backend.errors import LeaseLostError
from autumn_backend.repositories.jobs import JobSpec
from tests.integration.service_cases import ServiceCase

pytestmark = pytest.mark.integration


async def test_reclaim_retry_and_old_token_cannot_finish(e_case: ServiceCase) -> None:
    async with e_case.uows() as uow:
        repository = uow.repositories.jobs
        await repository.enqueue(
            JobSpec(kind="storage.test", idempotency_key=uuid4().hex, payload={}, max_attempts=2)
        )
        job = await repository.claim(kinds=("storage.test",))
        first_token = job.lease_token
        await uow.session.execute(
            update(Job)
            .where(Job.id == job.id)
            .values(lease_expires_at=func.clock_timestamp() - timedelta(seconds=1))
        )
        assert job.id in await repository.expired()
        await repository.reclaim(
            job.id, first_token, status=JobStatus.QUEUED, error_code="lease_expired"
        )
        with pytest.raises(LeaseLostError):
            await repository.finish(job.id, first_token)
        await uow.session.execute(
            update(Job).where(Job.id == job.id).values(available_at=func.clock_timestamp())
        )
        next_job = await repository.claim(kinds=("storage.test",))
        assert next_job.lease_token != first_token and next_job.attempts == 2
        await repository.retry(
            job.id, next_job.lease_token, delay_seconds=5, error_code="retryable_io"
        )
        assert job.status is JobStatus.FAILED and job.error_code == "max_attempts_exceeded"

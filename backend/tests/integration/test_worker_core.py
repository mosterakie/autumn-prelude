from uuid import uuid4

import pytest

from autumn_backend.db.enums import JobStatus
from autumn_backend.io_boundary import active_uows
from autumn_backend.jobs.queue import LeasedJob, Queue
from autumn_backend.repositories.jobs import JobSpec
from autumn_backend.services.tasks import TaskService
from autumn_backend.workers.core import Worker
from autumn_backend.workers.registry import Registry
from tests.integration.service_cases import ServiceCase

pytestmark = pytest.mark.integration


async def test_claim_handler_finishes_atomically_and_filters_kinds(e_case: ServiceCase) -> None:
    async with e_case.uows() as uow:
        job = (
            await uow.repositories.jobs.enqueue(
                JobSpec(kind="test.registered", idempotency_key=uuid4().hex, payload={})
            )
        ).record
        await uow.repositories.jobs.enqueue(
            JobSpec(kind="test.unsupported", idempotency_key=uuid4().hex, payload={})
        )

    async def handler(leased: LeasedJob) -> None:
        assert active_uows.get() == 0
        async with e_case.uows() as uow:
            await uow.repositories.jobs.finish(leased.id, leased.token, result={"ok": True})

    worker = Worker(
        Queue(e_case.uows), Registry({"test.registered": handler}), TaskService(e_case.uows).failure
    )
    assert await worker.run_once()
    assert not await worker.run_once()
    async with e_case.uows() as uow:
        assert (await uow.repositories.jobs.get_or_raise(job.id)).status is JobStatus.SUCCEEDED


async def test_exception_stores_only_stable_metadata(e_case: ServiceCase) -> None:
    async with e_case.uows() as uow:
        job = (
            await uow.repositories.jobs.enqueue(
                JobSpec(kind="test.error", idempotency_key=uuid4().hex, payload={})
            )
        ).record

    async def handler(leased: LeasedJob) -> None:
        raise RuntimeError("private provider response must not persist")

    await Worker(
        Queue(e_case.uows), Registry({"test.error": handler}), TaskService(e_case.uows).failure
    ).run_once()
    async with e_case.uows() as uow:
        failed = await uow.repositories.jobs.get_or_raise(job.id)
        assert failed.status is JobStatus.FAILED and failed.error_code == "HANDLER_FAILED"

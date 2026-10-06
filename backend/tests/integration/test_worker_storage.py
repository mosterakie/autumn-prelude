from datetime import timedelta

import pytest

from autumn_backend.db.enums import FileObjectStatus, JobStatus
from autumn_backend.jobs.queue import Queue
from autumn_backend.services.storage import StorageService
from autumn_backend.services.tasks import TaskService
from autumn_backend.workers.core import Worker
from autumn_backend.workers.handlers import storage_handlers
from autumn_backend.workers.registry import Registry
from tests.integration.service_cases import ServiceCase
from tests.integration.test_storage_service import store_for_test

pytestmark = pytest.mark.integration


async def test_storage_handlers_pause_reverify_finalize_delete(e_case: ServiceCase) -> None:
    storage, tasks = StorageService(e_case.uows, store_for_test()), TaskService(e_case.uows)
    worker = Worker(Queue(e_case.uows), Registry(storage_handlers(storage, tasks)), tasks.failure)
    file = await storage.upload(
        e_case.owner,
        idempotency_key="worker-upload",
        filename="test.pdf",
        data=b"%PDF-1.7\nexample",
        media_type="application/pdf",
    )
    async with e_case.uows() as uow:
        session = await uow.repositories.auth_sessions.get_for_update_or_raise(
            e_case.owner.auth_session_id
        )
        session.step_up_expires_at = await uow.repositories.users.database_time() - timedelta(
            seconds=1
        )
        await uow.session.flush()
    assert await worker.run_once()
    async with e_case.uows() as uow:
        from sqlalchemy import select

        from autumn_backend.db.models import Job

        job = await uow.session.scalar(select(Job).where(Job.kind == "storage.finalize"))
        assert job.status is JobStatus.WAITING_AUTH
        version, job_id = job.version, job.id
        session = await uow.repositories.auth_sessions.get_for_update_or_raise(
            e_case.owner.auth_session_id
        )
        session.step_up_expires_at = await uow.repositories.users.database_time() + timedelta(
            minutes=15
        )
        await uow.session.flush()
    await tasks.resume(e_case.owner, job_id, expected_version=version)
    assert await worker.run_once()
    assert await storage.read_private(e_case.owner, file.id) == b"%PDF-1.7\nexample"
    await storage.delete(e_case.owner, file.id)
    assert await worker.run_once()
    async with e_case.uows() as uow:
        deleted = await uow.repositories.files.get_or_raise(file.id)
        assert deleted.status is FileObjectStatus.PENDING_DELETE and deleted.deleted_at is not None

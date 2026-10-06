import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, update
from sqlalchemy.ext.asyncio import AsyncEngine

from autumn_backend.db.models import Job
from autumn_backend.db.session import UnitOfWorkFactory, create_session_factory
from autumn_backend.errors import LeaseLostError
from autumn_backend.jobs.queue import LeasedJob, Queue
from autumn_backend.repositories.jobs import JobSpec
from autumn_backend.services.tasks import TaskService
from autumn_backend.workers.core import Worker
from autumn_backend.workers.registry import Registry

pytestmark = pytest.mark.integration


async def test_heartbeat_loss_cancels_handler_and_rejects_late_commit(engine: AsyncEngine) -> None:
    uows = UnitOfWorkFactory(create_session_factory(engine))
    kind = "test.lease_" + uuid4().hex
    started, rejected = asyncio.Event(), asyncio.Event()
    async with uows() as uow:
        job_id = (
            await uow.repositories.jobs.enqueue(
                JobSpec(kind=kind, idempotency_key=kind, payload={})
            )
        ).record.id

    async def handler(job: LeasedJob) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # 模拟无法撤销的上游终于返回；旧资格仍不能提交正式结果。
            with pytest.raises(LeaseLostError):
                async with uows() as uow:
                    await uow.repositories.jobs.finish(job.id, job.token, result={"late": True})
            rejected.set()

    worker = Worker(
        Queue(uows, lease_seconds=2),
        Registry({kind: handler}),
        TaskService(uows).failure,
        heartbeat_seconds=0.1,
    )
    task = asyncio.create_task(worker.run_once())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        async with uows() as uow:
            await uow.session.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(
                    lease_expires_at=func.clock_timestamp() - timedelta(seconds=1),
                )
            )
        assert await asyncio.wait_for(task, timeout=3)
        assert rejected.is_set()
        async with uows() as uow:
            assert (await uow.repositories.jobs.get_or_raise(job_id)).result is None
    finally:
        task.cancel()
        async with uows() as uow:
            await uow.session.execute(delete(Job).where(Job.id == job_id))

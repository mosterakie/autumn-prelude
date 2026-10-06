"""应用事务内的薄入队入口，不自行开启或提交事务。"""

from dataclasses import dataclass, field
from uuid import UUID

from autumn_backend.db.enums import JobStatus
from autumn_backend.db.models import Job
from autumn_backend.db.session import UnitOfWork, UnitOfWorkFactory
from autumn_backend.repositories.jobs import JobSpec
from autumn_backend.repositories.result import Creation


async def enqueue(uow: UnitOfWork, spec: JobSpec) -> Creation[Job]:
    return await uow.repositories.jobs.enqueue(spec)


@dataclass(frozen=True, slots=True)
class LeasedJob:
    id: UUID
    kind: str
    token: UUID = field(repr=False)


class Queue:
    """每次方法持有独立短事务；领取结果只带调度身份，不带私人 payload。"""

    def __init__(self, uows: UnitOfWorkFactory, *, lease_seconds: int = 60) -> None:
        self.uows, self.lease_seconds = uows, lease_seconds

    async def claim(self, kinds: tuple[str, ...]) -> LeasedJob | None:
        async with self.uows() as uow:
            job = await uow.repositories.jobs.claim(kinds=kinds, lease_seconds=self.lease_seconds)
            if job is None:
                return None
            assert job.lease_token is not None
            return LeasedJob(job.id, job.kind, job.lease_token)

    async def heartbeat(self, job: LeasedJob) -> None:
        async with self.uows() as uow:
            await uow.repositories.jobs.heartbeat(
                job.id, job.token, lease_seconds=self.lease_seconds
            )

    async def settled(self, job: LeasedJob) -> bool:
        async with self.uows() as uow:
            current = await uow.repositories.jobs.get_or_raise(job.id)
            return current.status in (
                JobStatus.SUCCEEDED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
                JobStatus.WAITING_AUTH,
            )

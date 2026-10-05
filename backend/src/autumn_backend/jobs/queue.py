"""应用事务内的薄入队入口，不自行开启或提交事务。"""

from autumn_backend.db.models import Job
from autumn_backend.db.session import UnitOfWork
from autumn_backend.repositories.jobs import JobSpec
from autumn_backend.repositories.result import Creation


async def enqueue(uow: UnitOfWork, spec: JobSpec) -> Creation[Job]:
    return await uow.repositories.jobs.enqueue(spec)

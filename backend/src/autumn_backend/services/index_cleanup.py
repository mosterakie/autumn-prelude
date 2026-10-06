"""无正文读取能力的派生索引收敛，不能使任何内容重新可见。"""

from autumn_backend.db.session import UnitOfWorkFactory
from autumn_backend.errors import ConflictError
from autumn_backend.jobs.queue import LeasedJob


class IndexCleanupService:
    def __init__(self, uows: UnitOfWorkFactory) -> None:
        self.uows = uows

    async def execute(self, leased: LeasedJob) -> None:
        async with self.uows() as uow:
            probe = await uow.repositories.jobs.get_or_raise(leased.id)
            if probe.kind != "knowledge.cleanup" or probe.resource_id is None:
                raise ConflictError("索引收敛任务无效")
            await uow.repositories.resources.get_for_update_or_raise(probe.resource_id)
            await uow.repositories.jobs.require_lease(leased.id, leased.token)
            await uow.repositories.knowledge.retire_invalid(probe.resource_id)
            await uow.repositories.jobs.finish(
                leased.id, leased.token, result={"resource_id": str(probe.resource_id)}
            )

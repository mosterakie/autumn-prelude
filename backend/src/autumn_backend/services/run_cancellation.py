"""取消作业只停止已取消 Run 的外部请求，不重新授予读写权限。"""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress

from sqlalchemy import select

from autumn_backend.db.enums import ProviderCallStatus, RunStatus
from autumn_backend.db.models import ProviderCall
from autumn_backend.db.session import UnitOfWork, UnitOfWorkFactory
from autumn_backend.errors import ConflictError
from autumn_backend.io_boundary import require_outside_uow
from autumn_backend.jobs.queue import LeasedJob
from autumn_backend.repositories.provider_calls import external_idempotency_key

Cancel = Callable[[str, str, str], Awaitable[None]]


class RunCancellationService:
    def __init__(self, uows: UnitOfWorkFactory, cancel: Cancel | None = None) -> None:
        self.uows, self.cancel = uows, cancel

    async def _calls(self, uow: UnitOfWork, leased: LeasedJob) -> list[ProviderCall]:
        probe = await uow.repositories.jobs.get_or_raise(leased.id)
        if probe.kind != "run.cancel" or probe.run_id is None or probe.actor_id is None:
            raise ConflictError("取消任务绑定无效")
        await uow.repositories.users.get_for_update_or_raise(probe.actor_id)
        candidate = await uow.repositories.runs.get_or_raise(probe.run_id)
        await uow.repositories.conversations.for_user_for_update(
            candidate.conversation_id, probe.actor_id
        )
        run = await uow.repositories.runs.get_for_update_or_raise(candidate.id)
        await uow.repositories.jobs.require_lease(leased.id, leased.token)
        if (
            run.user_id != probe.actor_id
            or run.status is not RunStatus.CANCELLED
            or run.execution_generation != (probe.payload or {}).get("execution_generation")
        ):
            raise ConflictError("取消目标已变化")
        return list(
            (
                await uow.session.scalars(
                    select(ProviderCall)
                    .where(
                        ProviderCall.run_id == run.id,
                        ProviderCall.status == ProviderCallStatus.DISPATCHED,
                    )
                    .order_by(ProviderCall.id)
                    .with_for_update()
                )
            ).all()
        )

    async def execute(self, leased: LeasedJob) -> None:
        async with self.uows() as uow:
            calls = [
                (c.provider, c.model or "", external_idempotency_key(c.logical_call_key))
                for c in await self._calls(uow, leased)
            ]
        require_outside_uow()
        if self.cancel:
            for provider, model, key in calls:
                with suppress(Exception):
                    await asyncio.wait_for(self.cancel(provider, model, key), timeout=2)
        async with self.uows() as uow:
            for call in await self._calls(uow, leased):
                await uow.repositories.provider_calls.settle_unknown(
                    call.id, error_code="RUN_CANCELLED"
                )
            await uow.repositories.jobs.finish(
                leased.id, leased.token, result={"cancel_requested": True}
            )

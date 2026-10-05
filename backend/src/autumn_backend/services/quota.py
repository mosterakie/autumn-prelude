"""E4 额度查询与结算；内部结算方法组合进执行器的同一 UoW，不自行提交。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from autumn_backend.config import Settings, get_settings
from autumn_backend.db.enums import ProviderCallPurpose, ProviderCallStatus, QuotaReservationStatus
from autumn_backend.db.models import QuotaReservation, Run
from autumn_backend.db.session import UnitOfWork, UnitOfWorkFactory
from autumn_backend.errors import ConflictError, NotFoundError
from autumn_backend.policies import ActorContext
from autumn_backend.policies.access import authentication_denial
from autumn_backend.policies.facts import Operation, PolicyFacts
from autumn_backend.services.access import AuthorizationError, lock_authentication
from autumn_backend.services.ai_limits import daily_window, read_ai_limits
from autumn_backend.services.runs import QuotaView


class RefundReason(StrEnum):
    SERVER_FAILURE = "server_failure"
    PROVIDER_FAILURE = "provider_failure"


@dataclass(frozen=True, slots=True)
class Settlement:
    run_id: UUID
    bucket_id: UUID
    status: QuotaReservationStatus


class QuotaService:
    def __init__(self, uows: UnitOfWorkFactory, *, settings: Settings | None = None) -> None:
        self._uows = uows
        self._settings = settings if settings is not None else get_settings()

    async def current(self, actor: ActorContext) -> QuotaView:
        async with self._uows() as uow:
            auth = await lock_authentication(uow, actor)
            now = await uow.repositories.users.database_time()
            facts = PolicyFacts(
                operation=Operation.ASK,
                authentication=auth,
                now=now,
                current_scope_epoch=await uow.repositories.settings.get_acl_epoch(),
            )
            denial = authentication_denial(actor, facts)
            if denial is not None:
                raise AuthorizationError(denial)
            assert auth is not None
            limits = read_ai_limits(await uow.repositories.settings.get("ai_limits")).for_role(
                auth.role
            )
            start, end = daily_window(now, self._settings.quota_timezone)
            bucket = await uow.repositories.quota_buckets.for_window(auth.user_id, start)
            used, reserved = (bucket.used, bucket.reserved) if bucket is not None else (0, 0)
            return QuotaView(
                timezone=self._settings.quota_timezone,
                window_start=start,
                window_end=end,
                daily_limit=limits.daily_limit,
                used=used,
                reserved=reserved,
                remaining=max(0, limits.daily_limit - used - reserved),
                cooldown_until=auth.ai_cooldown_until,
                next_reset_at=end,
                server_time=now,
            )

    @staticmethod
    async def _lock_run(uow: UnitOfWork, run_id: UUID) -> Run:
        run = await uow.repositories.runs.get_or_raise(run_id)
        await uow.repositories.users.get_for_update_or_raise(run.user_id)
        await uow.repositories.conversations.get_for_update_or_raise(run.conversation_id)
        return await uow.repositories.runs.get_for_update_or_raise(run_id)

    @staticmethod
    def _result(record: QuotaReservation) -> Settlement:
        return Settlement(record.run_id, record.bucket_id, record.status)

    async def dispatch_model_in_uow(
        self, uow: UnitOfWork, run_id: UUID, call_id: UUID
    ) -> Settlement:
        """服务端执行入口：先完成当前权限/lease/generation 闸门，再派发标记与扣次一起提交。

        实际网络调用必须在调用方结束当前 UoW 后发生。不是 HTTP 或模型工具入口。
        """
        await self._lock_run(uow, run_id)
        call = await uow.repositories.provider_calls.get_for_update_or_raise(call_id)
        if call.run_id != run_id or call.purpose not in (
            ProviderCallPurpose.CHAT,
            ProviderCallPurpose.TOOL,
        ):
            raise NotFoundError("模型调用不存在")
        if call.status is ProviderCallStatus.PREPARED:
            await uow.repositories.provider_calls.mark_dispatched(call.id)
        elif call.started_at is None:
            raise ConflictError("调用未派发，不能扣次")
        return self._result(await uow.repositories.quota_reservations.charge(run_id))

    async def release_before_dispatch_in_uow(self, uow: UnitOfWork, run_id: UUID) -> Settlement:
        await self._lock_run(uow, run_id)
        if await uow.repositories.provider_calls.model_was_dispatched(run_id):
            raise ConflictError("模型已派发，不能按调用前失败释放")
        return self._result(await uow.repositories.quota_reservations.release(run_id))

    async def refund_failure_in_uow(
        self, uow: UnitOfWork, run_id: UUID, reason: RefundReason
    ) -> Settlement:
        """仅供可信错误分类/已鉴权管理流程调用；不接收模型提供的退款指令。"""
        if not isinstance(reason, RefundReason):
            raise ConflictError("需要可信的故障分类")
        await self._lock_run(uow, run_id)
        if not await uow.repositories.provider_calls.model_was_dispatched(run_id):
            raise ConflictError("未发生模型派发，应该释放预留")
        return self._result(await uow.repositories.quota_reservations.refund(run_id))

    async def cleanup_candidates(self, before: datetime, *, limit: int = 100) -> tuple[UUID, ...]:
        """只读候选接口；F/H 调度逐个重锁并复核后释放，不能按时间直接退款。"""
        async with self._uows() as uow:
            candidates = await uow.repositories.quota_reservations.cleanup_candidates(
                before, limit=limit
            )
            return tuple(
                [
                    run_id
                    for run_id in candidates
                    if not await uow.repositories.provider_calls.model_was_dispatched(run_id)
                ]
            )

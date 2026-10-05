"""额度桶和预留的原子状态机；限额由受控配置传入。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from autumn_backend.db.enums import QuotaReservationStatus as Status
from autumn_backend.db.models import QuotaBucket, QuotaReservation
from autumn_backend.errors import (
    ConflictError,
    InvalidInputError,
    NotFoundError,
    QuotaExceededError,
)
from autumn_backend.repositories.base import ControlledMutableRepository
from autumn_backend.repositories.constraints import database_errors
from autumn_backend.repositories.result import Creation


class QuotaBucketRepository(ControlledMutableRepository[QuotaBucket]):
    model = QuotaBucket

    async def get_or_create_for_update(
        self,
        user_id: UUID,
        window_start: datetime,
        window_end: datetime,
        *,
        timezone: str = "Asia/Shanghai",
        policy_version: int = 0,
    ) -> QuotaBucket:
        if (
            window_start.tzinfo is None
            or window_end.tzinfo is None
            or window_end <= window_start
            or policy_version < 0
        ):
            raise InvalidInputError("额度窗口无效")
        with database_errors():
            await self.session.execute(
                insert(QuotaBucket)
                .values(
                    user_id=user_id,
                    window_start=window_start,
                    window_end=window_end,
                    timezone=timezone,
                    policy_version=policy_version,
                )
                .on_conflict_do_nothing(constraint="uq_quota_buckets_user_window")
            )
        bucket = (
            await self.session.execute(
                select(QuotaBucket)
                .where(QuotaBucket.user_id == user_id, QuotaBucket.window_start == window_start)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        if bucket.window_end != window_end or bucket.timezone != timezone:
            raise ConflictError("已有额度桶的窗口定义不同")
        return bucket


class QuotaReservationRepository(ControlledMutableRepository[QuotaReservation]):
    model = QuotaReservation

    async def for_run(self, run_id: UUID, *, lock: bool = False) -> QuotaReservation | None:
        statement = select(QuotaReservation).where(QuotaReservation.run_id == run_id)
        if lock:
            statement = statement.with_for_update()
        return (
            await self.session.execute(statement.execution_options(populate_existing=True))
        ).scalar_one_or_none()

    async def reserve(
        self, *, run_id: UUID, bucket_id: UUID, user_id: UUID, current_limit: int, amount: int = 1
    ) -> Creation[QuotaReservation]:
        if amount != 1 or current_limit < 0:
            raise InvalidInputError("预留单位必须为 1，限额不得为负")
        # 所有计数变化均先锁 bucket 再锁 reservation，避免结算与重放锁序反转。
        bucket = await QuotaBucketRepository(self.session).get_for_update_or_raise(bucket_id)
        if bucket.user_id != user_id:
            raise ConflictError("额度桶归属不匹配")
        with database_errors():
            reservation = (
                await self.session.execute(
                    insert(QuotaReservation)
                    .values(run_id=run_id, bucket_id=bucket_id, user_id=user_id, amount=amount)
                    .on_conflict_do_nothing(constraint="uq_quota_reservations_run_id")
                    .returning(QuotaReservation)
                )
            ).scalar_one_or_none()
        if reservation is None:
            reservation = await self.for_run(run_id, lock=True)
            if (
                reservation is None
                or reservation.bucket_id != bucket_id
                or reservation.user_id != user_id
                or reservation.amount != amount
            ):
                raise ConflictError("运行已有不同的额度预留")
            return Creation(reservation, False)
        changed = (
            await self.session.execute(
                update(QuotaBucket)
                .where(
                    QuotaBucket.id == bucket_id,
                    QuotaBucket.used + QuotaBucket.reserved + amount <= current_limit,
                )
                .values(reserved=QuotaBucket.reserved + amount, version=QuotaBucket.version + 1)
                .returning(QuotaBucket)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if changed is None:
            # 必须向外传播，由 UoW 或显式 savepoint 回滚新插入的 reservation。
            raise QuotaExceededError("当前窗口额度已用完")
        return Creation(reservation, True)

    async def _locked(self, run_id: UUID) -> QuotaReservation:
        existing = await self.for_run(run_id)
        if existing is None:
            raise NotFoundError("额度预留不存在")
        await QuotaBucketRepository(self.session).get_for_update_or_raise(existing.bucket_id)
        locked = await self.for_run(run_id, lock=True)
        if locked is None:
            raise NotFoundError("额度预留不存在")
        return locked

    async def charge(self, run_id: UUID) -> QuotaReservation:
        reservation = await self._locked(run_id)
        if reservation.status in {Status.CHARGED, Status.REFUNDED}:
            return reservation
        if reservation.status != Status.RESERVED:
            raise ConflictError("已释放的预留不能扣次")
        await self._change_counts(reservation.bucket_id, used=1, reserved=-1)
        return await self._transition(
            reservation.id, Status.RESERVED, Status.CHARGED, {"charged_at": func.clock_timestamp()}
        )

    async def release(self, run_id: UUID) -> QuotaReservation:
        reservation = await self._locked(run_id)
        if reservation.status == Status.RELEASED:
            return reservation
        if reservation.status != Status.RESERVED:
            raise ConflictError("已扣次的预留不能释放")
        await self._change_counts(reservation.bucket_id, used=0, reserved=-1)
        return await self._transition(
            reservation.id, Status.RESERVED, Status.RELEASED, {"settled_at": func.clock_timestamp()}
        )

    async def refund(self, run_id: UUID) -> QuotaReservation:
        reservation = await self._locked(run_id)
        if reservation.status == Status.REFUNDED:
            return reservation
        if reservation.status != Status.CHARGED:
            raise ConflictError("只能退还已扣次的预留")
        await self._change_counts(reservation.bucket_id, used=-1, reserved=0)
        return await self._transition(
            reservation.id, Status.CHARGED, Status.REFUNDED, {"settled_at": func.clock_timestamp()}
        )

    transition_fields = frozenset({"charged_at", "settled_at"})

    async def _change_counts(self, bucket_id: UUID, *, used: int, reserved: int) -> None:
        with database_errors():
            changed = (
                await self.session.execute(
                    update(QuotaBucket)
                    .where(
                        QuotaBucket.id == bucket_id,
                        QuotaBucket.used + used >= 0,
                        QuotaBucket.reserved + reserved >= 0,
                    )
                    .values(
                        used=QuotaBucket.used + used,
                        reserved=QuotaBucket.reserved + reserved,
                        version=QuotaBucket.version + 1,
                    )
                    .returning(QuotaBucket)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
        if changed is None:
            raise ConflictError("额度计数不一致")

"""外部调用账本：逻辑身份稳定，物理尝试独立，未知结果不虚报零费用。"""

import hashlib
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from autumn_backend.db.enums import ProviderCallPurpose
from autumn_backend.db.enums import ProviderCallStatus as Status
from autumn_backend.db.models import ProviderCall
from autumn_backend.errors import ConflictError, InvalidInputError
from autumn_backend.repositories.base import ControlledMutableRepository
from autumn_backend.repositories.constraints import database_errors
from autumn_backend.repositories.result import Creation


def external_idempotency_key(logical_call_key: str) -> str:
    """重试时复用；不把 attempt_no 或私人参数送入外部幂等身份。"""
    if not logical_call_key:
        raise InvalidInputError("逻辑调用身份不能为空")
    return "autumn-" + hashlib.sha256(logical_call_key.encode("utf-8")).hexdigest()


class ProviderCallRepository(ControlledMutableRepository[ProviderCall]):
    model = ProviderCall
    transition_fields = frozenset(
        {
            "started_at",
            "finished_at",
            "external_request_id",
            "input_tokens",
            "output_tokens",
            "search_units",
            "actual_cost",
            "currency",
            "error_code",
        }
    )

    async def prepare(
        self,
        *,
        provider: str,
        purpose: ProviderCallPurpose,
        logical_call_key: str,
        attempt_no: int = 1,
        run_id: UUID | None = None,
        job_id: UUID | None = None,
        model: str | None = None,
        estimated_cost: Decimal | None = None,
        currency: str | None = None,
    ) -> Creation[ProviderCall]:
        if (
            not provider
            or not logical_call_key
            or attempt_no < 1
            or (run_id is None and job_id is None)
        ):
            raise InvalidInputError("外部调用缺少有效身份或归属")
        self._validate_cost(estimated_cost, currency)
        # 不存在首行时也可串行化同一逻辑调用的准备；事务结束即释放锁。
        await self.session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(logical_call_key, 0)))
        )
        previous = (
            await self.session.execute(
                select(ProviderCall)
                .where(ProviderCall.logical_call_key == logical_call_key)
                .order_by(ProviderCall.attempt_no)
                .limit(1)
            )
        ).scalar_one_or_none()
        identity = (provider, purpose, run_id, job_id, model)
        if (
            previous is not None
            and (
                previous.provider,
                previous.purpose,
                previous.run_id,
                previous.job_id,
                previous.model,
            )
            != identity
        ):
            raise ConflictError("逻辑调用身份已用于不同操作")
        with database_errors():
            call = (
                await self.session.execute(
                    insert(ProviderCall)
                    .values(
                        provider=provider,
                        purpose=purpose,
                        logical_call_key=logical_call_key,
                        attempt_no=attempt_no,
                        run_id=run_id,
                        job_id=job_id,
                        model=model,
                        estimated_cost=estimated_cost,
                        currency=currency,
                    )
                    .on_conflict_do_nothing(constraint="uq_provider_calls_logical_key_attempt")
                    .returning(ProviderCall)
                )
            ).scalar_one_or_none()
        if call is not None:
            return Creation(call, True)
        call = (
            await self.session.execute(
                select(ProviderCall).where(
                    ProviderCall.logical_call_key == logical_call_key,
                    ProviderCall.attempt_no == attempt_no,
                )
            )
        ).scalar_one()
        return Creation(call, False)

    @staticmethod
    def _validate_cost(cost: Decimal | None, currency: str | None) -> None:
        if cost is not None and (not cost.is_finite() or cost < 0 or currency is None):
            raise InvalidInputError("费用必须为非负有限值并指定币种")
        if currency is not None and (
            len(currency) != 3
            or not currency.isascii()
            or not currency.isalpha()
            or currency != currency.upper()
        ):
            raise InvalidInputError("币种必须为三个大写字母")

    async def mark_dispatched(self, call_id: UUID) -> ProviderCall:
        return await self._transition(
            call_id, Status.PREPARED, Status.DISPATCHED, {"started_at": func.clock_timestamp()}
        )

    async def settle_succeeded(
        self,
        call_id: UUID,
        *,
        external_request_id: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        search_units: int | None = None,
        actual_cost: Decimal | None = None,
        currency: str | None = None,
    ) -> ProviderCall:
        self._validate_cost(actual_cost, currency)
        changes: dict[str, object] = dict(
            finished_at=func.clock_timestamp(),
            external_request_id=external_request_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            search_units=search_units,
            actual_cost=actual_cost,
        )
        if currency is not None:
            changes["currency"] = currency
        return await self._transition(call_id, Status.DISPATCHED, Status.SUCCEEDED, changes)

    async def settle_failed(self, call_id: UUID, *, error_code: str) -> ProviderCall:
        return await self._transition(
            call_id,
            Status.DISPATCHED,
            Status.FAILED,
            {"finished_at": func.clock_timestamp(), "error_code": error_code},
        )

    async def settle_unknown(
        self, call_id: UUID, *, error_code: str = "provider_outcome_unknown"
    ) -> ProviderCall:
        # actual_cost 不写 0；unknown 由后续受控对账承接，禁止自动当作安全重试。
        return await self._transition(
            call_id,
            Status.DISPATCHED,
            Status.UNKNOWN,
            {"finished_at": func.clock_timestamp(), "error_code": error_code},
        )

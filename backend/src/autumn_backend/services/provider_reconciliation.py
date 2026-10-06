"""仅站长人工确认的供应商终态对账；不重放调用或写 assistant 正文。"""

import hashlib
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from autumn_backend.db.enums import AuditResult, ProviderCallStatus
from autumn_backend.db.models import AuditEvent, ProviderCall
from autumn_backend.db.session import UnitOfWork, UnitOfWorkFactory
from autumn_backend.errors import ConflictError, OptimisticLockError
from autumn_backend.policies import ActorContext
from autumn_backend.policies.facts import Operation, PolicyFacts
from autumn_backend.repositories.audit import AuditMetadata
from autumn_backend.services.access import lock_authentication, require_allowed


class Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    expected_version: int = Field(ge=0)
    expected_run_version: int | None = Field(default=None, ge=0)
    expected_generation: int | None = Field(default=None, ge=0)
    status: Literal["succeeded", "failed"]
    external_request_id: str = Field(min_length=1, max_length=256, pattern=r"^[^\s]+$")
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    search_units: int | None = Field(default=None, ge=0)
    actual_cost: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")

    @model_validator(mode="after")
    def cost_currency(self) -> "Receipt":
        if self.actual_cost is not None and self.currency is None:
            raise ValueError("费用必须附币种")
        return self

    def digest(self, call_id: UUID) -> str:
        # 包括所有结算字段，重放不能在同一证据摘要下改费用或终态。
        return hashlib.sha256((str(call_id) + self.model_dump_json()).encode()).hexdigest()


class ProviderReconciliationService:
    def __init__(self, uows: UnitOfWorkFactory) -> None:
        self.uows = uows

    @staticmethod
    async def _owner(uow: UnitOfWork, actor: ActorContext) -> None:
        auth = await lock_authentication(uow, actor)
        require_allowed(
            actor,
            PolicyFacts(
                operation=Operation.MANAGE_SETTINGS,
                authentication=auth,
                now=await uow.repositories.users.database_time(),
                current_scope_epoch=await uow.repositories.settings.get_acl_epoch(),
            ),
        )

    async def candidates(self, actor: ActorContext) -> tuple[dict[str, object], ...]:
        async with self.uows() as uow:
            await self._owner(uow, actor)
            calls = (
                await uow.session.scalars(
                    select(ProviderCall)
                    .where(
                        ProviderCall.status == ProviderCallStatus.UNKNOWN,
                    )
                    .order_by(ProviderCall.created_at, ProviderCall.id)
                    .limit(100)
                )
            ).all()
            results = []
            for call in calls:
                run = await uow.repositories.runs.get(call.run_id) if call.run_id else None
                results.append(
                    {
                        "id": str(call.id),
                        "version": call.version,
                        "provider": call.provider,
                        "purpose": call.purpose.value,
                        "expected_run_version": run.version if run else None,
                        "expected_generation": run.execution_generation if run else None,
                    }
                )
            await self._owner(uow, actor)
            return tuple(results)

    async def confirm(
        self, actor: ActorContext, call_id: UUID, receipt: Receipt
    ) -> dict[str, object]:
        async with self.uows() as uow:
            # 先拒绝非站长，不让目标是否存在成为身份探针；之后锁业务目标并复核。
            await self._owner(uow, actor)
            probe = await uow.repositories.provider_calls.get_or_raise(call_id)
            run = await uow.repositories.runs.get_or_raise(probe.run_id) if probe.run_id else None
            job = await uow.repositories.jobs.get_or_raise(probe.job_id) if probe.job_id else None
            target_user = run.user_id if run else job.actor_id if job else None
            for user_id in sorted({i for i in (target_user, actor.user_id) if i is not None}):
                await uow.repositories.users.get_for_update_or_raise(user_id)
            await self._owner(uow, actor)
            if run:
                await uow.repositories.conversations.get_for_update_or_raise(run.conversation_id)
                run = await uow.repositories.runs.get_for_update_or_raise(run.id)
            if job:
                await uow.repositories.jobs.get_for_update_or_raise(job.id)
            call = await uow.repositories.provider_calls.get_for_update_or_raise(call_id)
            digest = receipt.digest(call_id)
            existing = await uow.session.scalar(
                select(AuditEvent.id)
                .where(
                    AuditEvent.event_type == "provider.reconciled",
                    AuditEvent.metadata_json["provider_call_id"].astext == str(call_id),
                    AuditEvent.metadata_json["receipt_sha256"].astext == digest,
                )
                .limit(1)
            )
            if call.status is not ProviderCallStatus.UNKNOWN:
                if existing is not None and call.status.value == receipt.status:
                    return {
                        "id": str(call.id),
                        "status": call.status.value,
                        "version": call.version,
                    }
                raise ConflictError("对账结果已确定，不能替换证据或费用")
            if run and (run.version, run.execution_generation) != (
                receipt.expected_run_version,
                receipt.expected_generation,
            ):
                raise OptimisticLockError("运行仲裁身份已变化")
            if run is None and (
                receipt.expected_run_version is not None or receipt.expected_generation is not None
            ):
                raise ConflictError("作业调用不接受伪造运行身份")
            call = await uow.repositories.provider_calls.reconcile_unknown(
                call_id,
                expected_version=receipt.expected_version,
                status=ProviderCallStatus(receipt.status),
                external_request_id=receipt.external_request_id,
                input_tokens=receipt.input_tokens,
                output_tokens=receipt.output_tokens,
                search_units=receipt.search_units,
                actual_cost=receipt.actual_cost,
                currency=receipt.currency,
            )
            await uow.repositories.audit_events.record(
                event_type="provider.reconciled",
                result=AuditResult.SUCCEEDED,
                actor_id=actor.user_id,
                metadata=AuditMetadata(
                    provider_call_id=call.id,
                    receipt_sha256=digest,
                    run_id=call.run_id,
                    job_id=call.job_id,
                    after_status=call.status.value,
                ),
            )
            await self._owner(uow, actor)
            return {"id": str(call.id), "status": call.status.value, "version": call.version}

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from autumn_backend.db.enums import ProviderCallPurpose, ProviderCallStatus
from autumn_backend.errors import ConflictError, InvalidInputError
from autumn_backend.repositories.jobs import JobRepository, JobSpec
from autumn_backend.repositories.provider_calls import (
    ProviderCallRepository,
    external_idempotency_key,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "unknown"])
async def test_provider_state_cas(session: AsyncSession, terminal: str) -> None:
    job = (
        await JobRepository(session).enqueue(
            JobSpec(kind="run.execute", idempotency_key="provider-job", payload={})
        )
    ).record
    repository = ProviderCallRepository(session)
    arguments = dict(
        provider="deepseek",
        purpose=ProviderCallPurpose.CHAT,
        logical_call_key="logical-1",
        job_id=job.id,
    )
    first = await repository.prepare(**arguments, estimated_cost=Decimal("0.01"), currency="CNY")
    assert not (await repository.prepare(**arguments)).created
    with pytest.raises(ConflictError):
        await repository.settle_unknown(first.record.id)
    await repository.mark_dispatched(first.record.id)
    with pytest.raises(ConflictError):
        await repository.mark_dispatched(first.record.id)
    if terminal == "succeeded":
        settled = await repository.settle_succeeded(
            first.record.id, input_tokens=20, actual_cost=Decimal("0.02"), currency="CNY"
        )
        assert settled.actual_cost == Decimal("0.02")
    elif terminal == "failed":
        settled = await repository.settle_failed(first.record.id, error_code="provider_rejected")
    else:
        settled = await repository.settle_unknown(first.record.id)
        assert settled.actual_cost is None and settled.estimated_cost == Decimal("0.01")
    assert settled.status == ProviderCallStatus(terminal) and settled.finished_at is not None
    with pytest.raises(ConflictError):
        await repository.settle_unknown(first.record.id)
    second = await repository.prepare(**arguments, attempt_no=2)
    assert second.created and second.record.id != first.record.id
    assert external_idempotency_key(second.record.logical_call_key) == external_idempotency_key(
        first.record.logical_call_key
    )
    with pytest.raises(ConflictError):
        await repository.prepare(**(arguments | {"provider": "different"}), attempt_no=3)
    with pytest.raises(InvalidInputError):
        await repository.prepare(
            **arguments, attempt_no=3, estimated_cost=Decimal("NaN"), currency="CNY"
        )

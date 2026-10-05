import pytest

from autumn_backend.db.enums import ProviderCallPurpose, QuotaReservationStatus
from autumn_backend.errors import ConflictError
from autumn_backend.services.quota import QuotaService, RefundReason
from tests.integration.service_cases import ServiceCase

pytestmark = pytest.mark.integration


async def test_dispatch_charge_stop_and_refund_are_once(e_case: ServiceCase) -> None:
    service = QuotaService(e_case.uows)
    async with e_case.uows() as uow:
        run = await e_case.run(uow)
        call = (
            await uow.repositories.provider_calls.prepare(
                provider="deepseek",
                purpose=ProviderCallPurpose.CHAT,
                logical_call_key=f"model:{run.id}",
                run_id=run.id,
            )
        ).record
        first = await service.dispatch_model_in_uow(uow, run.id, call.id)
        assert first.status is QuotaReservationStatus.CHARGED
        assert await service.dispatch_model_in_uow(uow, run.id, call.id) == first
    with pytest.raises(ConflictError):
        async with e_case.uows() as uow:
            await service.release_before_dispatch_in_uow(uow, run.id)
    async with e_case.uows() as uow:
        await uow.repositories.provider_calls.settle_unknown(call.id)
    current = await service.current(e_case.member)
    assert (current.used, current.reserved) == (1, 0)
    async with e_case.uows() as uow:
        refund = await service.refund_failure_in_uow(uow, run.id, RefundReason.SERVER_FAILURE)
        assert refund.status is QuotaReservationStatus.REFUNDED
        assert (
            await service.refund_failure_in_uow(uow, run.id, RefundReason.SERVER_FAILURE) == refund
        )
    assert (await service.current(e_case.member)).used == 0


async def test_pre_dispatch_failure_releases_and_outer_failure_rolls_back(
    e_case: ServiceCase,
) -> None:
    service = QuotaService(e_case.uows)
    async with e_case.uows() as uow:
        run = await e_case.run(uow)
        call = (
            await uow.repositories.provider_calls.prepare(
                provider="deepseek",
                purpose=ProviderCallPurpose.CHAT,
                logical_call_key=f"model:{run.id}",
                run_id=run.id,
            )
        ).record
    with pytest.raises(RuntimeError):
        async with e_case.uows() as uow:
            await service.dispatch_model_in_uow(uow, run.id, call.id)
            raise RuntimeError("late failure")
    assert (await service.current(e_case.member)).reserved == 1
    async with e_case.uows() as uow:
        released = await service.release_before_dispatch_in_uow(uow, run.id)
        assert released.status is QuotaReservationStatus.RELEASED
        assert await service.release_before_dispatch_in_uow(uow, run.id) == released
    assert (await service.current(e_case.member)).reserved == 0

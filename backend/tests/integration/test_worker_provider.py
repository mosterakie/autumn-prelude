from uuid import uuid4

import pytest

from autumn_backend.db.enums import ProviderCallPurpose, ProviderCallStatus
from autumn_backend.errors import ConflictError
from autumn_backend.repositories.provider_calls import external_idempotency_key
from autumn_backend.services.runtime import RuntimeService
from tests.integration.service_cases import ServiceCase
from tests.integration.test_agent_runtime_service import accepted_job
from tests.integration.test_knowledge_service import service_for

pytestmark = pytest.mark.integration


async def test_stable_key_and_unknown_attempt_is_not_replayed(e_case: ServiceCase) -> None:
    job_id, token = await accepted_job(e_case)
    ticket = await RuntimeService(e_case.uows).open(job_id, token)
    logical = uuid4().hex
    async with e_case.uows() as uow:
        first = (
            await uow.repositories.provider_calls.prepare(
                provider="test",
                purpose=ProviderCallPurpose.EMBEDDING,
                logical_call_key=logical,
                run_id=ticket.run_id,
            )
        ).record
        await uow.repositories.provider_calls.mark_dispatched(first.id)
        await uow.repositories.provider_calls.settle_unknown(first.id)
        with pytest.raises(ConflictError):
            await uow.repositories.provider_calls.prepare(
                provider="test",
                purpose=ProviderCallPurpose.EMBEDDING,
                logical_call_key=logical,
                run_id=ticket.run_id,
                attempt_no=2,
            )
        safe = (
            await uow.repositories.provider_calls.prepare(
                provider="test",
                purpose=ProviderCallPurpose.EMBEDDING,
                logical_call_key=logical + "safe",
                run_id=ticket.run_id,
            )
        ).record
        await uow.repositories.provider_calls.mark_dispatched(safe.id)
        await uow.repositories.provider_calls.settle_failed(safe.id, error_code="known_failure")
        next_call = (
            await uow.repositories.provider_calls.prepare(
                provider="test",
                purpose=ProviderCallPurpose.EMBEDDING,
                logical_call_key=logical + "safe",
                run_id=ticket.run_id,
                attempt_no=2,
            )
        ).record
        assert external_idempotency_key(safe.logical_call_key) == external_idempotency_key(
            next_call.logical_call_key
        )
        assert first.status is ProviderCallStatus.UNKNOWN and first.actual_cost is None
    service = service_for(e_case)
    await service.capture_dependencies(ticket.actor, ticket.run_id)
    with pytest.raises(ConflictError):
        await service.retrieve(ticket.actor, ticket.run_id, "正文")
    await RuntimeService(e_case.uows).stop(job_id, token, code="PROVIDER_OUTCOME_UNKNOWN")
    another_id, another_token = await accepted_job(e_case)
    ticket = await RuntimeService(e_case.uows).open(another_id, another_token)
    await service.capture_dependencies(ticket.actor, ticket.run_id)
    await service.retrieve(ticket.actor, ticket.run_id, "正文")
    with pytest.raises(ConflictError):
        await service.retrieve(ticket.actor, ticket.run_id, "正文")

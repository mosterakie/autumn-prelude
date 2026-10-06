from dataclasses import replace
from uuid import uuid4

import pytest

from autumn_backend.config import Environment, Settings
from autumn_backend.db.enums import ConversationMode
from autumn_backend.services.access import AuthorizationError
from autumn_backend.services.runs import AcceptRunCommand, RunService
from autumn_backend.services.runtime import BudgetExceededError, RuntimeService
from tests.integration.service_cases import ServiceCase
from tests.integration.test_knowledge_service import service_for

pytestmark = pytest.mark.integration


async def accepted_job(e_case: ServiceCase, *, owner=False, message="请根据获准资料回答"):
    actor = e_case.owner if owner else e_case.member
    async with e_case.uows() as uow:
        conversation = await uow.repositories.conversations.create(
            user_id=actor.user_id, mode=ConversationMode.OWNER if owner else ConversationMode.PUBLIC
        )
    accepted = await RunService(e_case.uows).accept_run(
        actor,
        AcceptRunCommand(
            conversation_id=conversation.id,
            client_message_id=uuid4(),
            idempotency_key=uuid4().hex,
            message=message,
            resource_ids=(e_case.resource_id,),
        ),
    )
    async with e_case.uows() as uow:
        job = await uow.repositories.jobs.claim(kinds=("run.dispatch",))
        assert job is not None and job.lease_token is not None and job.run_id == accepted.run_id
    return job.id, job.lease_token


async def test_runtime_identity_is_database_bound_and_checkpoint_has_no_authority(
    e_case: ServiceCase,
) -> None:
    job_id, token = await accepted_job(e_case)
    runtime = RuntimeService(e_case.uows)
    ticket = await runtime.open(job_id, token)
    assert ticket.actor.user_id == e_case.member.user_id
    assert ticket.mode == "public" and ticket.thread_id.endswith(":mode:public")
    assert ticket.request_text == "请根据获准资料回答"
    await service_for(e_case).capture_dependencies(ticket.actor, ticket.run_id)
    await runtime.guard(replace(ticket, actor=e_case.owner))
    with pytest.raises(AuthorizationError):
        await runtime.guard(replace(ticket, generation=ticket.generation + 1))
    await runtime.checkpoint(ticket, "plan")
    restored = await runtime.open(job_id, token)
    assert restored.actor.role is e_case.member.role and restored.record.node == "plan"
    async with e_case.uows() as uow:
        run = await uow.repositories.runs.get_or_raise(ticket.run_id)
        assert "请根据获准资料回答" not in str(run.config_snapshot["agent_runtime"])


async def test_runtime_budget_survives_reentry_and_does_not_recharge_quota(
    e_case: ServiceCase,
) -> None:
    job_id, token = await accepted_job(e_case)
    settings = Settings(
        environment=Environment.TEST,
        agent_limits={
            "model_calls": 1,
            "tool_calls": 1,
            "input_units": 1000,
            "output_tokens": 256,
            "output_per_call": 128,
            "elapsed_ms": 1000,
        },
    )
    runtime = RuntimeService(e_case.uows, settings=settings)
    ticket = await runtime.open(job_id, token)
    await service_for(e_case).capture_dependencies(ticket.actor, ticket.run_id)
    await runtime.reserve(ticket, kind="model", input_units=100, elapsed_ms=5)
    await runtime.reserve(ticket, kind="tool")
    restored = await runtime.open(job_id, token)
    assert (restored.record.usage.model_calls, restored.record.usage.tool_calls) == (1, 1)
    with pytest.raises(BudgetExceededError):
        await runtime.reserve(restored, kind="model", input_units=100)
    async with e_case.uows() as uow:
        reservation = await uow.repositories.quota_reservations.for_run(ticket.run_id)
        assert reservation is not None and reservation.status.value == "reserved"

from dataclasses import replace
from uuid import uuid4

import pytest

from autumn_backend.agent.tools import ToolCall, Tools
from autumn_backend.db.enums import ActionStatus, JobStatus, RunStatus
from autumn_backend.errors import InvalidInputError, LeaseLostError
from autumn_backend.services.actions import ActionService
from autumn_backend.services.runtime import RuntimeService
from tests.integration.service_cases import ServiceCase
from tests.integration.test_action_service import command
from tests.integration.test_agent_runtime_service import accepted_job
from tests.integration.test_knowledge_service import build, service_for

pytestmark = pytest.mark.integration


async def test_search_adapter_uses_server_mode_and_rejects_identity_fields(
    e_case: ServiceCase,
) -> None:
    knowledge = service_for(e_case)
    await build(e_case, knowledge, e_case.publication_id)
    job_id, token = await accepted_job(e_case)
    runtime = RuntimeService(e_case.uows)
    ticket = await runtime.open(job_id, token)
    await knowledge.capture_dependencies(ticket.actor, ticket.run_id)
    tools = Tools(runtime, knowledge, ActionService(e_case.uows))
    assert {item["name"] for item in tools.schemas(ticket)} == {"search_knowledge"}
    with pytest.raises(InvalidInputError):
        await tools.execute(
            ticket,
            ToolCall(
                name="search_knowledge",
                arguments={"query": "正文", "actor_id": str(e_case.owner.user_id)},
            ),
            step=1,
        )
    result = await tools.execute(
        ticket, ToolCall(name="search_knowledge", arguments={"query": "正文"}), step=2
    )
    assert {item.text for item in result.sources} == {"公开标题", "公共正文"}
    assert "私密备注" not in str(result)
    async with e_case.uows() as uow:
        records = await uow.repositories.knowledge.sources(
            ticket.run_id, context_generation=ticket.generation
        )
        assert {source.id for source in records} == {item.id for item in result.sources}


async def test_proposal_is_human_confirmation_only_and_atomic_with_job_lease(
    e_case: ServiceCase,
) -> None:
    job_id, token = await accepted_job(e_case, owner=True)
    runtime = RuntimeService(e_case.uows)
    ticket = await runtime.open(job_id, token)
    knowledge = service_for(e_case)
    await knowledge.capture_dependencies(ticket.actor, ticket.run_id)
    tools = Tools(runtime, knowledge, ActionService(e_case.uows))
    call = ToolCall(
        name="propose_action", arguments={"command": command(e_case).model_dump(mode="json")}
    )
    with pytest.raises(LeaseLostError):
        await tools.execute(replace(ticket, token=uuid4()), call, step=1)
    result = await tools.execute(ticket, call, step=1)
    async with e_case.uows() as uow:
        action = await uow.repositories.actions.get_or_raise(result.action_id)
        run = await uow.repositories.runs.get_or_raise(ticket.run_id)
        job = await uow.repositories.jobs.get_or_raise(job_id)
        assert action.status is ActionStatus.AWAITING_CONFIRMATION and action.requires_confirmation
        assert action.authorization_message_id == ticket.request_id
        assert run.status is RunStatus.WAITING_APPROVAL and run.execution_generation == 2
        assert job.status is JobStatus.SUCCEEDED
        assert (
            await uow.repositories.publications.current_for_resource(e_case.resource_id)
        ).id == e_case.publication_id

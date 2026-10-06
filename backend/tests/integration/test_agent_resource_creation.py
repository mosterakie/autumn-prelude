"""收藏与手记的最小端到端业务验收：仅确认后写库，无外部 I/O。"""

from dataclasses import replace
from uuid import UUID

import pytest
from sqlalchemy import func, select

from autumn_backend.agent.tools import ToolCall, Tools
from autumn_backend.db.enums import ActionStatus, ConversationMode, RunStatus
from autumn_backend.db.models import Resource
from autumn_backend.errors import InvalidInputError, NotFoundError
from autumn_backend.jobs.queue import Queue
from autumn_backend.services.access import AuthorizationError
from autumn_backend.services.action_execution import ActionExecutionService
from autumn_backend.services.actions import ActionService
from autumn_backend.services.resources import ResourceService
from autumn_backend.services.runtime import RuntimeService
from autumn_backend.services.web_search import WebSearchService
from tests.integration.service_cases import ServiceCase
from tests.integration.test_agent_runtime import Model, runtime_for
from tests.integration.test_agent_runtime_service import accepted_job
from tests.integration.test_knowledge_service import service_for
from tests.integration.test_web_search_service import Search

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "call,kind,title",
    [
        (
            ToolCall(
                name="propose_bookmark",
                arguments={
                    "url": "https://zh.z-library.sk/svg",
                    "tags": ["阅读"],
                },
            ),
            "bookmark",
            "zh.z-library.sk",
        ),
        (
            ToolCall(
                name="propose_note",
                arguments={
                    "title": "秋日手记",
                    "body_text": "## 今天\n记下一个想法。",
                    "tags": ["随笔"],
                },
            ),
            "article",
            "秋日手记",
        ),
    ],
)
async def test_agent_creation_requires_confirmation_and_saves_private_once(
    e_case: ServiceCase,
    call: ToolCall,
    kind: str,
    title: str,
) -> None:
    job_id, token = await accepted_job(e_case, owner=True)
    runtime = RuntimeService(e_case.uows)
    ticket = await runtime.open(job_id, token)
    actions = ActionService(e_case.uows)
    async with e_case.uows() as uow:
        before = await uow.session.scalar(
            select(func.count())
            .select_from(Resource)
            .where(
                Resource.owner_id == e_case.owner.user_id,
            )
        )
    model = Model(
        {"kind": "tool", "call": call.model_dump(mode="json")},
        {"kind": "reply", "text": "已保存为私人内容。"},
    )
    agent = runtime_for(e_case, model)
    result = await agent.execute(job_id, token)
    assert result.status is RunStatus.WAITING_APPROVAL
    assert result.action_id is not None
    preview = await actions.read(e_case.owner, result.action_id)
    assert preview.status is ActionStatus.AWAITING_CONFIRMATION
    assert preview.changes["kind"] == kind and preview.changes["title"] == title
    async with e_case.uows() as uow:
        assert (
            await uow.session.scalar(
                select(func.count())
                .select_from(Resource)
                .where(
                    Resource.owner_id == e_case.owner.user_id,
                )
            )
            == before
        )
    await actions.confirm(
        e_case.owner,
        preview.id,
        expected_action_version=preview.version,
        parameters_hash=preview.parameters_hash,
    )
    leased = await Queue(e_case.uows).claim(("action.execute",))
    assert leased is not None
    async with e_case.uows() as uow:
        job = await uow.repositories.jobs.get_or_raise(leased.id)
        assert job.payload["action_id"] == str(preview.id) and job.actor_id == e_case.owner.user_id
    await ActionExecutionService(e_case.uows).execute(leased)
    completed = await actions.read(e_case.owner, preview.id)
    assert completed.status is ActionStatus.SUCCEEDED
    assert completed.result is not None
    resource_id = UUID(completed.result["resource_id"])
    saved = await ResourceService(e_case.uows).read(e_case.owner, resource_id)
    assert saved["kind"] == kind and saved["current_revision"]["title"] == title
    assert saved["publication"] is None
    for field in ("url", "body_text", "tags"):
        if field in call.arguments:
            assert saved["current_revision"][field] == call.arguments[field]
    # 重复确认不能产生第二份资源。
    again = await actions.confirm(
        e_case.owner,
        preview.id,
        expected_action_version=preview.version,
        parameters_hash=preview.parameters_hash,
    )
    assert again.status is ActionStatus.SUCCEEDED
    async with e_case.uows() as uow:
        assert (
            await uow.session.scalar(
                select(func.count())
                .select_from(Resource)
                .where(
                    Resource.owner_id == e_case.owner.user_id,
                )
            )
            == before + 1
        )
        run = await uow.repositories.runs.get_or_raise(ticket.run_id)
        assert run.status is RunStatus.QUEUED and run.execution_generation > ticket.generation
    resumed = await Queue(e_case.uows).claim(("run.resume",))
    assert resumed is not None
    finished = await agent.execute(resumed.id, resumed.token)
    assert finished.status is RunStatus.SUCCEEDED
    assert model.prompts[-1]["execution_results"][0]["result"]["resource_id"] == str(resource_id)
    assert model.prompts[-1]["runtime_context"] == {
        "role": "owner",
        "mode": "owner",
        "search_mode": "site",
    }


@pytest.mark.parametrize("owner", [False, True])
async def test_public_mode_cannot_propose_private_creation_even_with_owner_snapshot(
    e_case: ServiceCase,
    owner: bool,
) -> None:
    job_id, token = await accepted_job(e_case, owner=owner, mode=ConversationMode.PUBLIC)
    runtime = RuntimeService(e_case.uows)
    ticket = await runtime.open(job_id, token)
    knowledge = service_for(e_case)
    await knowledge.capture_dependencies(ticket.actor, ticket.run_id)
    tools = Tools(runtime, knowledge, ActionService(e_case.uows))
    assert {tool["name"] for tool in tools.schemas(replace(ticket, actor=e_case.owner))} == {
        "search_knowledge"
    }
    with pytest.raises((AuthorizationError, NotFoundError)):
        await tools.execute(
            replace(ticket, actor=e_case.owner),
            ToolCall(name="propose_bookmark", arguments={"url": "https://example.com"}),
            step=1,
        )


async def test_private_auto_tools_and_reject_model_supplied_identity(e_case: ServiceCase) -> None:
    job_id, token = await accepted_job(e_case, owner=True)
    runtime = RuntimeService(e_case.uows)
    ticket = await runtime.open(job_id, token)
    knowledge = service_for(e_case)
    await knowledge.capture_dependencies(ticket.actor, ticket.run_id)
    tools = Tools(
        runtime,
        knowledge,
        ActionService(e_case.uows),
        web_search=WebSearchService(e_case.uows, Search()),
    )
    assert {tool["name"] for tool in tools.schemas(replace(ticket, search_mode="auto"))} == {
        "search_knowledge",
        "search_web",
        "propose_action",
        "propose_bookmark",
        "propose_note",
    }
    assert "search_web" not in {tool["name"] for tool in tools.schemas(ticket)}
    with pytest.raises(InvalidInputError):
        await tools.execute(
            ticket,
            ToolCall(
                name="propose_note",
                arguments={
                    "title": "标题",
                    "body_text": "内容",
                    "owner_id": str(e_case.owner.user_id),
                },
            ),
            step=1,
        )

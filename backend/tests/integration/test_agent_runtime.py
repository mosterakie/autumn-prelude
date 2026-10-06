import asyncio
import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from autumn_backend.agent.context import ContextLoader
from autumn_backend.agent.runtime import AgentRuntime
from autumn_backend.agent.tools import Tools
from autumn_backend.db.enums import (
    JobStatus,
    MessageRole,
    ProviderCallPurpose,
    ProviderCallStatus,
    RunStatus,
)
from autumn_backend.db.models import Message, ProviderCall
from autumn_backend.io_boundary import active_uows
from autumn_backend.repositories.publications import PublicationProjection
from autumn_backend.services.actions import ActionService
from autumn_backend.services.execution import ExecutionService, ModelResult
from autumn_backend.services.input_waits import InputWaitService
from autumn_backend.services.quota import QuotaService
from autumn_backend.services.runtime import RuntimeService
from tests.integration.service_cases import ServiceCase
from tests.integration.test_action_service import command
from tests.integration.test_agent_runtime_service import accepted_job
from tests.integration.test_knowledge_service import build, service_for

pytestmark = pytest.mark.integration


class Model:
    provider, model = "test", "fixed"

    def __init__(self, *plans):
        self.plans = list(plans)
        self.prompts, self.keys = [], []
        self.canceled = False

    async def generate(self, prompt, *, max_output_tokens, external_idempotency_key):
        assert active_uows.get() == 0 and max_output_tokens == 4096
        self.prompts.append(json.loads(prompt))
        self.keys.append(external_idempotency_key)
        plan = self.plans.pop(0)
        if callable(plan):
            return await plan()
        return ModelResult(json.dumps(plan, ensure_ascii=False), input_tokens=20, output_tokens=30)

    async def cancel(self, *, external_idempotency_key):
        assert active_uows.get() == 0 and external_idempotency_key in self.keys
        self.canceled = True


def runtime_for(e_case, model, *, saver=None):
    service, knowledge = RuntimeService(e_case.uows), service_for(e_case)
    return AgentRuntime(
        service,
        ContextLoader(service, knowledge),
        Tools(service, knowledge, ActionService(e_case.uows)),
        ExecutionService(e_case.uows),
        InputWaitService(e_case.uows),
        model,
        checkpointer=saver,
    )


SEARCH = {"kind": "tool", "call": {"name": "search_knowledge", "arguments": {"query": "正文"}}}


async def test_graph_search_reply_persists_all_model_sources_and_charges_once(
    e_case: ServiceCase,
) -> None:
    await build(e_case, service_for(e_case), e_case.publication_id)
    job_id, token = await accepted_job(e_case)
    model = Model(SEARCH, {"kind": "reply", "text": "根据公开资料形成的回复"})
    saver = InMemorySaver()
    result = await runtime_for(e_case, model, saver=saver).execute(job_id, token)
    assert result.status is RunStatus.SUCCEEDED
    assert model.prompts[0]["untrusted_data"]["sources"] == []
    assert {item["text"] for item in model.prompts[1]["untrusted_data"]["sources"]} == {
        "公开标题",
        "公共正文",
    }
    assert "私密备注" not in str(model.prompts)
    checkpoint = str([saved async for saved in saver.alist(None)])
    assert (
        "正文" not in checkpoint
        and "当前用户" not in checkpoint
        and str(e_case.member.user_id) not in checkpoint
    )
    async with e_case.uows() as uow:
        run = await uow.repositories.runs.get_or_raise(result.run_id)
        message = await uow.session.get(Message, result.message_id)
        assert (
            message.body_text == "根据公开资料形成的回复" and message.role is MessageRole.ASSISTANT
        )
        sources = await uow.repositories.knowledge.sources(
            run.id, context_generation=run.execution_generation
        )
        assert {item["source_id"] for item in model.prompts[1]["untrusted_data"]["sources"]} == {
            str(item.id) for item in sources
        }
        assert all(
            item.revision_id == e_case.revision_id
            and item.publication_id == e_case.publication_id
            and item.index_id
            and item.chunk_id
            and item.locator
            for item in sources
        )
        assert set(run.config_snapshot["model_inputs"][1]["source_ids"]) == {
            str(item.id) for item in sources
        }
        assert run.config_snapshot["agent_runtime"]["usage"]["model_calls"] == 2
        assert (await uow.repositories.jobs.get_or_raise(job_id)).status is JobStatus.SUCCEEDED
        calls = (
            await uow.session.scalars(select(ProviderCall).where(ProviderCall.run_id == run.id))
        ).all()
        assert len(calls) == 3 and all(
            call.status is ProviderCallStatus.SUCCEEDED for call in calls
        )
        assert sum(call.purpose is ProviderCallPurpose.CHAT for call in calls) == 2
        assert sum(call.purpose is ProviderCallPurpose.EMBEDDING for call in calls) == 1
    quota = await QuotaService(e_case.uows).current(e_case.member)
    assert (quota.used, quota.reserved) == (1, 0)


async def test_input_wait_and_new_job_restore_same_run_sources_and_budget(
    e_case: ServiceCase,
) -> None:
    await build(e_case, service_for(e_case), e_case.publication_id)
    job_id, token = await accepted_job(e_case)
    model = Model(
        SEARCH,
        {"kind": "input", "prompt": "需要哪种摘要？", "options": ["简短", "详细"]},
        {"kind": "reply", "text": "简短摘要"},
    )
    runtime = runtime_for(e_case, model, saver=InMemorySaver())
    waiting = await runtime.execute(job_id, token)
    assert waiting.status is RunStatus.WAITING_INPUT
    async with e_case.uows() as uow:
        assert (await uow.repositories.jobs.get_or_raise(job_id)).status is JobStatus.SUCCEEDED
    await InputWaitService(e_case.uows).answer(
        e_case.member, waiting.run_id, wait_id=waiting.input_request_id, answer="简短"
    )
    async with e_case.uows() as uow:
        job = await uow.repositories.jobs.claim(kinds=("run.resume",))
        assert job is not None and job.lease_token is not None
    result = await runtime.execute(job.id, job.lease_token)
    assert result.run_id == waiting.run_id and result.status is RunStatus.SUCCEEDED
    assert model.prompts[-1]["current_user_request"].endswith("简短")
    assert {item["text"] for item in model.prompts[-1]["untrusted_data"]["sources"]} == {
        "公开标题",
        "公共正文",
    }
    assert len(set(model.keys)) == 3
    async with e_case.uows() as uow:
        run = await uow.repositories.runs.get_or_raise(result.run_id)
        assert run.config_snapshot["agent_runtime"]["usage"]["model_calls"] == 3
        assert run.config_snapshot["agent_runtime"]["usage"]["tool_calls"] == 1
    quota = await QuotaService(e_case.uows).current(e_case.member)
    assert (quota.used, quota.reserved) == (1, 0)


async def test_acl_change_during_request_stops_upstream_and_discards_reply(
    e_case: ServiceCase,
) -> None:
    await build(e_case, service_for(e_case), e_case.publication_id)
    job_id, token = await accepted_job(e_case)

    async def revoke_while_waiting():
        async with e_case.uows() as uow:
            await uow.repositories.publications.revoke(
                e_case.resource_id, expected_version=0, expected_acl_version=1
            )
        await asyncio.Event().wait()

    model = Model(SEARCH, revoke_while_waiting)
    result = await runtime_for(e_case, model).execute(job_id, token)
    assert result.status is RunStatus.FAILED and result.error_code == "ACL_CONTEXT_INVALIDATED"
    assert model.canceled
    async with e_case.uows() as uow:
        assert not (
            await uow.session.scalars(
                select(Message.id).where(
                    Message.run_id == result.run_id, Message.role == MessageRole.ASSISTANT
                )
            )
        ).all()
        calls = (
            await uow.session.scalars(
                select(ProviderCall)
                .where(ProviderCall.run_id == result.run_id)
                .order_by(ProviderCall.started_at)
            )
        ).all()
        assert [call.status for call in calls] == [
            ProviderCallStatus.SUCCEEDED,
            ProviderCallStatus.SUCCEEDED,
            ProviderCallStatus.UNKNOWN,
        ]
        assert calls[-1].actual_cost is None
    quota = await QuotaService(e_case.uows).current(e_case.member)
    assert quota.used == 1


async def test_provider_error_body_is_not_saved_in_checkpoint(e_case: ServiceCase) -> None:
    job_id, token = await accepted_job(e_case)

    async def failure():
        raise RuntimeError("private-provider-error-content")

    saver = InMemorySaver()
    result = await runtime_for(e_case, Model(failure), saver=saver).execute(job_id, token)
    assert result.status is RunStatus.FAILED and result.error_code == "PROVIDER_ERROR"
    assert "private-provider-error-content" not in str([saved async for saved in saver.alist(None)])


async def test_confirmed_action_result_resumes_in_new_acl_context(e_case: ServiceCase) -> None:
    job_id, token = await accepted_job(e_case, owner=True)
    model = Model(
        {
            "kind": "tool",
            "call": {
                "name": "propose_action",
                "arguments": {"command": command(e_case).model_dump(mode="json")},
            },
        },
        {"kind": "reply", "text": "指定版本已发布"},
    )
    runtime = runtime_for(e_case, model)
    waiting = await runtime.execute(job_id, token)
    assert waiting.status is RunStatus.WAITING_APPROVAL
    actions = ActionService(e_case.uows)
    action = await actions.read(e_case.owner, waiting.action_id)
    await actions.confirm(
        e_case.owner,
        action.id,
        expected_action_version=action.version,
        parameters_hash=action.parameters_hash,
    )
    async with e_case.uows() as uow:
        job = await uow.repositories.jobs.claim(kinds=("action.execute",))
        assert job is not None and job.lease_token is not None

    async def writer(uow, action, preview):
        publication = await uow.repositories.publications.publish_under_resource_lock(
            resource_id=preview.target_id,
            revision_id=preview.revision_id,
            expected_version=preview.expected_version,
            expected_acl_version=preview.expected_acl_version,
            published_by=e_case.owner.user_id,
            projection=PublicationProjection(frozenset(preview.public_fields), ai_enabled=True),
        )
        return {"publication_id": str(publication.id)}

    await ExecutionService(e_case.uows).commit_action_result(
        e_case.owner, job.id, job.lease_token, writer
    )
    async with e_case.uows() as uow:
        resume = await uow.repositories.jobs.claim(kinds=("run.resume",))
        assert resume is not None and resume.lease_token is not None
    result = await runtime.execute(resume.id, resume.lease_token)
    assert result.status is RunStatus.SUCCEEDED and result.generation > waiting.generation
    assert model.prompts[-1]["execution_results"][0]["action_id"] == str(action.id)
    assert model.prompts[-1]["execution_results"][0]["status"] == "succeeded"
    quota = await QuotaService(e_case.uows).current(e_case.owner)
    assert quota.used == 1

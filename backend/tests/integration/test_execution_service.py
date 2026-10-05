import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from autumn_backend.db.enums import (
    ActionStatus,
    JobStatus,
    MessageRole,
    ProviderCallPurpose,
    RunStatus,
)
from autumn_backend.db.models import Message
from autumn_backend.errors import LeaseLostError
from autumn_backend.io_boundary import active_uows
from autumn_backend.repositories.jobs import JobSpec
from autumn_backend.repositories.publications import PublicationProjection
from autumn_backend.services.access import AuthorizationError
from autumn_backend.services.actions import ActionService
from autumn_backend.services.execution import ExecutionService, ModelResult
from autumn_backend.services.quota import QuotaService
from tests.integration.service_cases import ServiceCase
from tests.integration.test_action_service import command, prepared_run
from tests.integration.test_knowledge_service import build, service_for
from tests.integration.test_storage_service import store_for_test

pytestmark = pytest.mark.integration


class Model:
    async def complete(self, *, external_idempotency_key):
        assert active_uows.get() == 0 and external_idempotency_key.startswith("autumn-")
        return ModelResult("基础回复", input_tokens=2, output_tokens=3)


async def model_job(e_case: ServiceCase):
    run_id, _ = await prepared_run(e_case)
    async with e_case.uows() as uow:
        run = await uow.repositories.runs.get_or_raise(run_id)
        await uow.repositories.jobs.enqueue(
            JobSpec(
                kind="run.dispatch",
                idempotency_key=f"run.dispatch:{run.id}",
                actor_id=run.user_id,
                auth_session_id=e_case.member.auth_session_id,
                run_id=run.id,
                payload={"run_id": str(run.id), "execution_generation": run.execution_generation},
            )
        )
        job = await uow.repositories.jobs.claim(kinds=("run.dispatch",))
        assert job is not None and job.lease_token is not None
        call = (
            await uow.repositories.provider_calls.prepare(
                provider="deepseek",
                purpose=ProviderCallPurpose.CHAT,
                run_id=run.id,
                job_id=job.id,
                logical_call_key=f"model:{run.id}:{run.execution_generation}",
            )
        ).record
    return run.id, job.id, job.lease_token, call.id


async def assert_no_reply(e_case: ServiceCase, run_id):
    async with e_case.uows() as uow:
        assert (
            await uow.session.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.run_id == run_id, Message.role == MessageRole.ASSISTANT)
            )
        ) == 0
        assert (await uow.repositories.runs.get_or_raise(run_id)).status is not RunStatus.SUCCEEDED


async def test_model_io_outside_uow_and_joint_success(e_case: ServiceCase) -> None:
    run_id, job_id, token, call_id = await model_job(e_case)
    result = await ExecutionService(e_case.uows).call_model(
        e_case.member, job_id, token, call_id, Model()
    )
    assert result.run_id == run_id
    async with e_case.uows() as uow:
        assert (await uow.repositories.jobs.get_or_raise(job_id)).status is JobStatus.SUCCEEDED
        run = await uow.repositories.runs.get_or_raise(run_id)
        assert run.status is RunStatus.SUCCEEDED and run.current_message_id == result.message_id
        assert (await uow.session.get(Message, result.message_id)).body_text == "基础回复"
        # 真数据库验证新 CHECK，不用 Python 断言替代结构约束。
        with pytest.raises(IntegrityError):
            async with uow.session.begin_nested():
                await uow.session.execute(
                    text("UPDATE runs SET execution_generation = 0 WHERE id = :id"), {"id": run_id}
                )
    quota = await QuotaService(e_case.uows).current(e_case.member)
    assert (quota.used, quota.reserved) == (1, 0)


async def test_old_lease_and_generation_cannot_commit(e_case: ServiceCase) -> None:
    service = ExecutionService(e_case.uows)
    for invalidation in ("lease", "generation"):
        run_id, job_id, token, call_id = await model_job(e_case)
        fence = await service.start_model_call(e_case.member, job_id, token, call_id)
        async with e_case.uows() as uow:
            if invalidation == "lease":
                job = await uow.repositories.jobs.get_for_update_or_raise(job_id)
                job.lease_expires_at = await uow.repositories.users.database_time() - timedelta(
                    seconds=1
                )
            else:
                run = await uow.repositories.runs.get_for_update_or_raise(run_id)
                run.execution_generation += 1
        expected = LeaseLostError if invalidation == "lease" else AuthorizationError
        with pytest.raises(expected):
            await service.commit_model_result(e_case.member, fence, ModelResult("旧回复"))
        await assert_no_reply(e_case, run_id)


async def test_revoked_source_during_model_io_discards_reply(e_case: ServiceCase) -> None:
    knowledge = service_for(e_case)
    await build(e_case, knowledge, e_case.publication_id)
    run_id, job_id, token, call_id = await model_job(e_case)
    await knowledge.retrieve(e_case.member, run_id, "正文")
    service = ExecutionService(e_case.uows)
    fence = await service.start_model_call(e_case.member, job_id, token, call_id)
    async with e_case.uows() as uow:
        await uow.repositories.publications.revoke(
            e_case.resource_id, expected_version=0, expected_acl_version=1
        )
    with pytest.raises(AuthorizationError) as denial:
        await service.commit_model_result(e_case.member, fence, ModelResult("失权回复"))
    assert denial.value.decision.code.value == "ACL_CONTEXT_INVALIDATED"
    await assert_no_reply(e_case, run_id)


async def test_action_result_and_late_lease_loss_are_one_transaction(e_case: ServiceCase) -> None:
    actions = ActionService(e_case.uows)
    first = await actions.preview(e_case.owner, command(e_case), idempotency_key="execute")
    await actions.confirm(
        e_case.owner,
        first.id,
        expected_action_version=first.version,
        parameters_hash=first.parameters_hash,
    )
    async with e_case.uows() as uow:
        job = await uow.repositories.jobs.claim(kinds=("action.execute",))
        assert job is not None and job.lease_token is not None

    async def writer(uow, action, preview, *, lose_lease=False):
        publication = await uow.repositories.publications.publish_under_resource_lock(
            resource_id=preview.target_id,
            revision_id=preview.revision_id,
            expected_version=preview.expected_version,
            expected_acl_version=preview.expected_acl_version,
            published_by=e_case.owner.user_id,
            projection=PublicationProjection(frozenset(preview.public_fields), ai_enabled=True),
        )
        if lose_lease:
            current_job = await uow.repositories.jobs.get_for_update_or_raise(job.id)
            current_job.lease_expires_at = await uow.repositories.users.database_time() - timedelta(
                seconds=1
            )
        return {"publication_id": str(publication.id)}

    async def stale_writer(uow, action, preview):
        return await writer(uow, action, preview, lose_lease=True)

    service = ExecutionService(e_case.uows)
    with pytest.raises(LeaseLostError):
        await service.commit_action_result(e_case.owner, job.id, job.lease_token, stale_writer)
    async with e_case.uows() as uow:
        assert (await uow.repositories.resources.get_or_raise(e_case.resource_id)).acl_version == 1
        assert (await uow.repositories.actions.get_or_raise(first.id)).status is ActionStatus.READY
    assert (
        await service.commit_action_result(e_case.owner, job.id, job.lease_token, writer)
        == first.id
    )
    async with e_case.uows() as uow:
        assert (await uow.repositories.resources.get_or_raise(e_case.resource_id)).acl_version == 2
        assert (
            await uow.repositories.actions.get_or_raise(first.id)
        ).status is ActionStatus.SUCCEEDED
        assert (await uow.repositories.jobs.get_or_raise(job.id)).status is JobStatus.SUCCEEDED


async def test_object_io_guard_includes_child_tasks(e_case: ServiceCase) -> None:
    store = store_for_test()
    key = f"objects/{uuid4().hex}.pdf"
    async with e_case.uows() as uow:
        assert active_uows.get() == 1
        with pytest.raises(RuntimeError, match="UnitOfWork"):
            await store.stage(key, b"%PDF-1.7")
        with pytest.raises(RuntimeError, match="UnitOfWork"):
            await asyncio.create_task(store.stage(key, b"%PDF-1.7"))
        await uow.session.execute(text("SELECT 1"))
    assert active_uows.get() == 0
    await store.stage(key, b"%PDF-1.7")

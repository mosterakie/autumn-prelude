from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from autumn_backend.db.enums import ActionStatus, RunStatus
from autumn_backend.db.models import Job, Message
from autumn_backend.errors import IdempotencyConflictError, NotFoundError, OptimisticLockError
from autumn_backend.repositories.jobs import JobRepository
from autumn_backend.services.actions import ActionService, ResourcePreview, SettingsPreview
from autumn_backend.services.input_waits import InputWaitService, load_input
from autumn_backend.services.quota import QuotaService
from tests.integration.service_cases import ServiceCase
from tests.integration.test_knowledge_service import service_for

pytestmark = pytest.mark.integration


def command(e_case: ServiceCase, *, kind="publish"):
    return ResourcePreview(
        kind=kind,
        target_id=e_case.resource_id,
        expected_version=0,
        expected_acl_version=1,
        revision_id=e_case.revision_id if kind == "publish" else None,
        public_fields=("body", "title") if kind == "publish" else (),
        ai_enabled=kind == "publish",
    )


async def prepared_run(e_case: ServiceCase, *, owner=False):
    actor = e_case.owner if owner else e_case.member
    async with e_case.uows() as uow:
        run = await e_case.run(uow, owner=owner)
        message = await uow.repositories.messages.append_user(
            conversation_id=run.conversation_id,
            run_id=run.id,
            client_message_id=uuid4(),
            body="请帮我处理",
        )
    await service_for(e_case).capture_dependencies(actor, run.id)
    return run.id, message.id


async def test_preview_confirm_retry_and_nullable_target_identity(e_case: ServiceCase) -> None:
    service = ActionService(e_case.uows)
    run_id, message_id = await prepared_run(e_case, owner=True)
    first = await service.preview(
        e_case.owner,
        command(e_case),
        idempotency_key="preview",
        run_id=run_id,
        authorization_message_id=message_id,
    )
    assert first.status is ActionStatus.AWAITING_CONFIRMATION
    assert (
        await service.preview(
            e_case.owner,
            command(e_case),
            idempotency_key="preview",
            run_id=run_id,
            authorization_message_id=message_id,
        )
        == first
    )
    with pytest.raises(IdempotencyConflictError):
        await service.preview(
            e_case.owner,
            command(e_case, kind="revoke"),
            idempotency_key="preview",
            run_id=run_id,
            authorization_message_id=message_id,
        )
    with pytest.raises(NotFoundError):
        await service.read(e_case.member, first.id)
    confirmed = await service.confirm(
        e_case.owner,
        first.id,
        expected_action_version=first.version,
        parameters_hash=first.parameters_hash,
    )
    assert confirmed.status is ActionStatus.READY
    assert (
        await service.confirm(
            e_case.owner,
            first.id,
            expected_action_version=first.version,
            parameters_hash=first.parameters_hash,
        )
        == confirmed
    )
    async with e_case.uows() as uow:
        assert (
            await uow.session.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.kind == "action.execute", Job.run_id == run_id)
            )
        ) == 1
        assert (await uow.repositories.runs.get_or_raise(run_id)).status is RunStatus.QUEUED
        assert (await uow.repositories.resources.get_or_raise(e_case.resource_id)).acl_version == 1
    settings = SettingsPreview(
        expected_version=0,
        value={"daily_limit": 10, "cooldown_hours": 24, "per_minute": 3, "concurrency": 1},
    )
    setting_action = await service.preview(e_case.owner, settings, idempotency_key="settings")
    assert (
        await service.preview(e_case.owner, settings, idempotency_key="settings") == setting_action
    )
    with pytest.raises(IdempotencyConflictError):
        await service.preview(
            e_case.owner,
            settings.model_copy(update={"value": {**settings.value, "daily_limit": 11}}),
            idempotency_key="settings",
        )


async def test_stale_preview_and_persisted_expiry(e_case: ServiceCase) -> None:
    service = ActionService(e_case.uows)
    first = await service.preview(e_case.owner, command(e_case), idempotency_key="stale")
    async with e_case.uows() as uow:
        await uow.repositories.publications.revoke(
            e_case.resource_id, expected_version=0, expected_acl_version=1
        )
    with pytest.raises(OptimisticLockError):
        await service.confirm(
            e_case.owner,
            first.id,
            expected_action_version=first.version,
            parameters_hash=first.parameters_hash,
        )
    # 过期只改变等待状态，不执行目标动作；查看仍返回真实状态。
    async with e_case.uows() as uow:
        action = await uow.repositories.actions.get_for_update_or_raise(first.id)
        now = await uow.repositories.users.database_time()
        action.created_at = now - timedelta(hours=1)
        action.expires_at = now - timedelta(seconds=1)
    expired = await service.confirm(
        e_case.owner,
        first.id,
        expected_action_version=first.version,
        parameters_hash=first.parameters_hash,
    )
    assert expired.status is ActionStatus.EXPIRED
    assert (await service.read(e_case.owner, first.id)).status is ActionStatus.EXPIRED


async def test_input_consumption_is_once_and_does_not_recharge(e_case: ServiceCase) -> None:
    run_id, _ = await prepared_run(e_case)
    service = InputWaitService(e_case.uows)
    wait = await service.request(
        e_case.member, run_id, wait_id=uuid4(), prompt="请选择", options=("A", "B")
    )
    assert (
        await service.request(
            e_case.member, run_id, wait_id=wait.id, prompt="请选择", options=("A", "B")
        )
        == wait
    )
    async with e_case.uows() as uow:
        user = await uow.repositories.users.get_for_update_or_raise(e_case.member.user_id)
        user.ai_cooldown_until = await uow.repositories.users.database_time() + timedelta(days=1)
    before = await QuotaService(e_case.uows).current(e_case.member)
    consumed = await service.answer(e_case.member, run_id, wait_id=wait.id, answer="A")
    assert consumed.consumed_at is not None
    assert await service.answer(e_case.member, run_id, wait_id=wait.id, answer="A") == consumed
    with pytest.raises(IdempotencyConflictError):
        await service.answer(e_case.member, run_id, wait_id=wait.id, answer="B")
    after = await QuotaService(e_case.uows).current(e_case.member)
    assert (after.used, after.reserved) == (before.used, before.reserved)
    async with e_case.uows() as uow:
        assert (
            await uow.session.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.client_message_id == wait.id)
            )
        ) == 1
        assert (
            await uow.session.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.kind == "run.resume", Job.run_id == run_id)
            )
        ) == 1


async def test_answer_late_failure_rolls_back_consumption_message_and_job(
    e_case: ServiceCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, _ = await prepared_run(e_case)
    service = InputWaitService(e_case.uows)
    wait = await service.request(e_case.member, run_id, wait_id=uuid4(), prompt="补充说明")
    original = JobRepository.enqueue

    async def fail_after_insert(self, spec):
        await original(self, spec)
        raise RuntimeError("late failure")

    monkeypatch.setattr(JobRepository, "enqueue", fail_after_insert)
    with pytest.raises(RuntimeError):
        await service.answer(e_case.member, run_id, wait_id=wait.id, answer="说明")
    async with e_case.uows() as uow:
        run = await uow.repositories.runs.get_or_raise(run_id)
        assert (
            run.status is RunStatus.WAITING_INPUT
            and load_input(run.input_request).consumed_at is None
        )
        assert (
            await uow.session.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.client_message_id == wait.id)
            )
        ) == 0

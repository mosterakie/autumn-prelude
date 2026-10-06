from uuid import uuid4

import pytest

from autumn_backend.db.enums import ActionStatus
from autumn_backend.services.actions import ActionService, MemoryPreview, SettingsPreview
from autumn_backend.workers.bootstrap import configured_worker
from tests.integration.service_cases import ServiceCase
from tests.integration.test_action_service import command
from tests.integration.test_knowledge_service import service_for

pytestmark = pytest.mark.integration


async def test_confirmed_action_worker_publication_settings_and_memory(e_case: ServiceCase) -> None:
    actions = ActionService(e_case.uows)
    knowledge = service_for(e_case)
    worker = configured_worker(e_case.uows, knowledge.storage)
    for requested in (
        command(e_case),
        SettingsPreview(
            expected_version=0,
            value={
                "daily_limit": 10,
                "cooldown_hours": 24,
                "per_minute": 3,
                "concurrency": 1,
            },
        ),
        MemoryPreview(kind="create_memory", content_text="喜欢秋天"),
    ):
        preview = await actions.preview(e_case.owner, requested, idempotency_key=uuid4().hex)
        await actions.confirm(
            e_case.owner,
            preview.id,
            expected_action_version=preview.version,
            parameters_hash=preview.parameters_hash,
        )
        assert await worker.run_once()
        result = await actions.read(e_case.owner, preview.id)
        assert result.status is ActionStatus.SUCCEEDED
    async with e_case.uows() as uow:
        assert (await uow.repositories.settings.get("ai_limits")).version == 1
        memories = await uow.repositories.memories.for_user(e_case.owner.user_id)
        assert memories.items[0].confirmed_at is not None


async def test_stale_confirmed_target_fails_action_without_partial_write(
    e_case: ServiceCase,
) -> None:
    actions = ActionService(e_case.uows)
    preview = await actions.preview(e_case.owner, command(e_case), idempotency_key=uuid4().hex)
    await actions.confirm(
        e_case.owner,
        preview.id,
        expected_action_version=preview.version,
        parameters_hash=preview.parameters_hash,
    )
    async with e_case.uows() as uow:
        await uow.repositories.resources.set_archived(
            e_case.resource_id, expected_version=0, expected_acl_version=1, archived=True
        )
    assert await configured_worker(e_case.uows, service_for(e_case).storage).run_once()
    result = await actions.read(e_case.owner, preview.id)
    assert result.status is ActionStatus.FAILED

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from autumn_backend.db.enums import RunEventType, RunSourceType, RunStatus
from autumn_backend.errors import NotFoundError
from autumn_backend.services.access import AuthorizationError
from autumn_backend.services.events import EventService
from autumn_backend.services.input_waits import InputWaitService
from autumn_backend.services.quota import QuotaService
from tests.integration.service_cases import ServiceCase
from tests.integration.test_action_service import prepared_run
from tests.integration.test_chat_api import chat_app

pytestmark = pytest.mark.integration


async def test_sse_returns_current_snapshot_and_resume_does_not_charge(e_case: ServiceCase) -> None:
    run_id, _ = await prepared_run(e_case)
    async with e_case.uows() as uow:
        run = await uow.repositories.runs.get_for_update_or_raise(run_id)
        message = await uow.repositories.messages.append_assistant_complete(
            conversation_id=run.conversation_id,
            run_id=run_id,
            client_message_id=uuid4(),
            body="数据库里的完整回复",
        )
        run.current_message_id = message.id
        run.status, run.finished_at = RunStatus.SUCCEEDED, datetime.now(UTC)
        await uow.repositories.run_events.emit(
            run_id,
            RunEventType.MESSAGE_SNAPSHOT,
            {
                "message_id": str(message.id),
                "content_version": message.content_version,
                "execution_generation": run.execution_generation,
                "body": "历史事件中的错误正文",
            },
        )
        done = await uow.repositories.run_events.emit(run_id, RunEventType.DONE)
    app, member, owner = await chat_app(e_case)
    app.state.events = EventService(e_case.uows)
    before = await QuotaService(e_case.uows).current(member.session.actor)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set("autumn_session", member.token)
        response = await client.get(f"/api/runs/{run_id}/events")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "event: message.snapshot" in response.text
        assert "数据库里的完整回复" in response.text and "历史事件中的错误正文" not in response.text
        assert f"id: {done.seq}" in response.text and "event: done" in response.text
        resumed = await client.get(
            f"/api/runs/{run_id}/events", headers={"Last-Event-ID": str(done.seq)}
        )
        assert resumed.status_code == 200 and "数据库里的完整回复" in resumed.text
        assert (
            await client.get(f"/api/runs/{run_id}/events?after=0", headers={"Last-Event-ID": "1"})
        ).status_code == 422
        client.cookies.clear()
        client.cookies.set("autumn_session", owner.token)
        assert (await client.get(f"/api/runs/{run_id}/events")).status_code == 404
    after = await QuotaService(e_case.uows).current(member.session.actor)
    assert (before.used, before.reserved) == (after.used, after.reserved)


async def test_event_batches_recheck_source_and_session(e_case: ServiceCase) -> None:
    run_id, _ = await prepared_run(e_case)
    async with e_case.uows() as uow:
        run = await uow.repositories.runs.get_for_update_or_raise(run_id)
        await uow.repositories.knowledge.record_values(
            run_id,
            "dependency:public",
            {
                "source_type": RunSourceType.RESOURCE,
                "resource_id": e_case.resource_id,
                "revision_id": e_case.revision_id,
                "publication_id": e_case.publication_id,
                "observed_acl_version": 1,
            },
            context_generation=run.execution_generation,
        )
    service = EventService(e_case.uows)
    first = await service.batch(e_case.member, run_id, after=0, snapshot=True)
    assert not first.close
    with pytest.raises(NotFoundError):
        await service.batch(e_case.owner, run_id, after=0)
    async with e_case.uows() as uow:
        await uow.repositories.publications.revoke(
            e_case.resource_id, expected_version=0, expected_acl_version=1
        )
    with pytest.raises(AuthorizationError):
        await service.batch(e_case.member, run_id, after=first.cursor)
    async with e_case.uows() as uow:
        session = await uow.repositories.auth_sessions.get_for_update_or_raise(
            e_case.member.auth_session_id
        )
        session.revoked_at = datetime.now(UTC)
    with pytest.raises(AuthorizationError):
        await service.batch(e_case.member, run_id, after=first.cursor)


async def test_waiting_stream_closes_without_cancelling_run(e_case: ServiceCase) -> None:
    run_id, _ = await prepared_run(e_case)
    wait = await InputWaitService(e_case.uows).request(
        e_case.member, run_id, wait_id=uuid4(), prompt="请选择资料范围"
    )
    app, member, _ = await chat_app(e_case)
    app.state.events = EventService(e_case.uows)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set("autumn_session", member.token)
        response = await client.get(f"/api/runs/{run_id}/events")
        assert response.status_code == 200 and "waiting_input" in response.text
        assert "请选择资料范围" in response.text and "event: done" not in response.text
    async with e_case.uows() as uow:
        run = await uow.repositories.runs.get_or_raise(run_id)
        assert run.status is RunStatus.WAITING_INPUT and run.input_request["id"] == str(wait.id)
        assert run.execution_generation == 2

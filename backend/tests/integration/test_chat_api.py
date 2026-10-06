from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from autumn_backend.app import create_app
from autumn_backend.auth.service import AuthService
from autumn_backend.config import Environment, Settings
from autumn_backend.db.enums import RunSourceType, RunStatus
from autumn_backend.db.models import Job
from autumn_backend.services.chats import ChatService
from autumn_backend.services.quota import QuotaService
from autumn_backend.services.runs import RunService
from tests.integration.service_cases import ServiceCase
from tests.integration.test_action_service import prepared_run

pytestmark = pytest.mark.integration


async def chat_app(e_case: ServiceCase):
    settings = Settings(environment=Environment.TEST)
    auth = AuthService(e_case.uows, settings)
    app = create_app(settings)
    app.state.auth, app.state.chats, app.state.runs = (
        auth,
        ChatService(e_case.uows),
        RunService(e_case.uows, settings=settings),
    )
    async with e_case.uows() as uow:
        member = await uow.repositories.users.get_for_update(e_case.member.user_id)
        first = await auth._new_session(uow, member)
        owner = await uow.repositories.users.get_for_update(e_case.owner.user_id)
        second = await auth._new_session(uow, owner, step_up=e_case.owner.step_up_expires_at)
    return app, first, second


def headers(login, key=None):
    result = {"Origin": "http://localhost:3000", "X-CSRF-Token": login.session.csrf_token}
    if key:
        result["Idempotency-Key"] = key
    return result


async def test_ask_http_idempotency_cancel_and_owner_cannot_read_member_chat(
    e_case: ServiceCase,
) -> None:
    app, member, owner = await chat_app(e_case)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set("autumn_session", member.token)
        conversation = (
            await client.post(
                "/api/conversations", json={"mode": "public"}, headers=headers(member)
            )
        ).json()["data"]
        body = {
            "conversation_id": conversation["id"],
            "client_message_id": str(uuid4()),
            "message": "请总结公开文章",
            "resource_ids": [str(e_case.resource_id)],
        }
        key = uuid4().hex
        web = await client.post(
            "/api/ask", json={**body, "search_mode": "web"}, headers=headers(member, uuid4().hex)
        )
        assert web.status_code == 403
        accepted = await client.post("/api/ask", json=body, headers=headers(member, key))
        assert accepted.status_code == 202
        run_id = accepted.json()["data"]["run_id"]
        repeat = await client.post("/api/ask", json=body, headers=headers(member, key))
        assert repeat.json()["data"]["run_id"] == run_id
        assert repeat.json()["data"]["quota"]["reserved"] == 1
        changed = await client.post(
            "/api/ask", json={**body, "message": "不同问题"}, headers=headers(member, key)
        )
        assert (
            changed.status_code == 409 and changed.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        )
        client.cookies.clear()
        client.cookies.set("autumn_session", owner.token)
        for path in (
            f"/api/conversations/{conversation['id']}",
            f"/api/conversations/{conversation['id']}/messages",
            f"/api/runs/{run_id}",
        ):
            assert (await client.get(path)).status_code == 404
        client.cookies.clear()
        client.cookies.set("autumn_session", member.token)
        cancelled = await client.post(f"/api/runs/{run_id}/cancel", headers=headers(member))
        assert cancelled.status_code == 202 and cancelled.json()["data"]["status"] == "cancelled"
        generation = cancelled.json()["data"]["execution_generation"]
        assert generation == 2
        assert (await client.post(f"/api/runs/{run_id}/cancel", headers=headers(member))).json()[
            "data"
        ]["execution_generation"] == generation
        assert (await QuotaService(e_case.uows).current(member.session.actor)).reserved == 0


async def test_history_and_citation_hide_revoked_sources(e_case: ServiceCase) -> None:
    run_id, _ = await prepared_run(e_case)
    async with e_case.uows() as uow:
        run = await uow.repositories.runs.get_for_update_or_raise(run_id)
        source = await uow.repositories.knowledge.record_values(
            run_id,
            "dependency:public",
            {
                "source_type": RunSourceType.RESOURCE,
                "resource_id": e_case.resource_id,
                "revision_id": e_case.revision_id,
                "publication_id": e_case.publication_id,
                "observed_acl_version": 1,
                "excerpt": "获准资料片段",
            },
            context_generation=run.execution_generation,
        )
        message = await uow.repositories.messages.append_assistant_complete(
            conversation_id=run.conversation_id,
            run_id=run_id,
            client_message_id=uuid4(),
            body="依赖资料生成的回复",
        )
        run.current_message_id, run.status, run.finished_at = (
            message.id,
            RunStatus.SUCCEEDED,
            datetime.now(UTC),
        )
        conversation_id = run.conversation_id
    app, member, owner = await chat_app(e_case)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set("autumn_session", member.token)
        assert (await client.get(f"/api/runs/{run_id}")).json()["data"]["message"][
            "body"
        ] == "依赖资料生成的回复"
        citation = await client.get(f"/api/citations/{source.id}")
        assert citation.status_code == 200 and "私密备注" not in citation.text
        client.cookies.clear()
        client.cookies.set("autumn_session", owner.token)
        assert (await client.get(f"/api/citations/{source.id}")).status_code == 404
        async with e_case.uows() as uow:
            await uow.repositories.publications.revoke(
                e_case.resource_id, expected_version=0, expected_acl_version=1
            )
        client.cookies.clear()
        client.cookies.set("autumn_session", member.token)
        hidden = (await client.get(f"/api/conversations/{conversation_id}/messages")).json()[
            "data"
        ]["items"]
        assistant = next(item for item in hidden if item["role"] == "assistant")
        assert assistant["body"] == "" and assistant["status"] == "hidden"
        assert (await client.get(f"/api/citations/{source.id}")).status_code == 404


async def test_waiting_auth_resume_rebinds_rotated_session_without_new_quota(
    e_case: ServiceCase,
) -> None:
    run_id, _ = await prepared_run(e_case, owner=True)
    async with e_case.uows() as uow:
        run = await uow.repositories.runs.get_for_update_or_raise(run_id)
        run.status = RunStatus.WAITING_AUTH
    app, _, owner = await chat_app(e_case)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set("autumn_session", owner.token)
        result = await client.post(
            f"/api/runs/{run_id}/resume", json={"resume": True}, headers=headers(owner)
        )
        assert result.status_code == 202
        repeated = await client.post(
            f"/api/runs/{run_id}/resume", json={"resume": True}, headers=headers(owner)
        )
        assert (
            repeated.json()["data"]["execution_generation"]
            == result.json()["data"]["execution_generation"]
            == 2
        )
        quota = await QuotaService(e_case.uows).current(owner.session.actor)
        assert quota.reserved == 1 and quota.used == 0
        async with e_case.uows() as uow:
            run = await uow.repositories.runs.get_or_raise(UUID(str(run_id)))
            assert run.auth_session_id == owner.session.actor.auth_session_id
            assert (
                await uow.session.execute(
                    select(func.count())
                    .select_from(Job)
                    .where(Job.run_id == run_id, Job.kind == "run.resume")
                )
            ).scalar_one() == 1

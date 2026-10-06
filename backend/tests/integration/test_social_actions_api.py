from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from autumn_backend.db.models import Job
from autumn_backend.services.actions import ActionService
from autumn_backend.services.comments import CommentService
from autumn_backend.services.publication import PublicationService, RevokeCommand
from autumn_backend.services.quota import QuotaService
from tests.integration.service_cases import ServiceCase
from tests.integration.test_action_service import command
from tests.integration.test_chat_api import chat_app, headers

pytestmark = pytest.mark.integration


async def social_app(e_case: ServiceCase):
    app, member, owner = await chat_app(e_case)
    app.state.comments = CommentService(e_case.uows)
    app.state.quota = QuotaService(e_case.uows)
    app.state.actions = ActionService(e_case.uows)
    return app, member, owner


def login(client, identity):
    client.cookies.clear()
    client.cookies.set("autumn_session", identity.token)


async def test_comments_pending_moderation_edit_and_parent_visibility(e_case: ServiceCase) -> None:
    async with e_case.uows() as uow:
        user = await uow.repositories.users.get_for_update_or_raise(e_case.member.user_id)
        user.ai_cooldown_until = datetime.now(UTC) + timedelta(hours=24)
    app, member, owner = await social_app(e_case)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login(client, member)
        body = {
            "client_id": str(uuid4()),
            "resource_id": str(e_case.resource_id),
            "body": "普通用户留言",
        }
        first = await client.post("/api/comments", json=body, headers=headers(member))
        assert first.status_code == 201 and first.json()["data"]["status"] == "pending"
        comment = first.json()["data"]
        assert (await client.post("/api/comments", json=body, headers=headers(member))).json()[
            "data"
        ]["id"] == comment["id"]
        assert (
            await client.post(
                "/api/comments", json={**body, "body": "改变正文"}, headers=headers(member)
            )
        ).status_code == 409
        listing = f"/api/public/comments?resource_id={e_case.resource_id}"
        client.cookies.clear()
        assert (await client.get(listing)).json()["data"]["items"] == []
        login(client, owner)
        assert (
            await client.patch(
                f"/api/comments/{comment['id']}",
                json={"body": "不能代写", "expected_version": 0},
                headers=headers(owner),
            )
        ).status_code == 404
        approved = (
            await client.post(
                f"/api/moderation/comments/{comment['id']}/decision",
                json={
                    "decision": "approve",
                    "reason": "审核",
                    "expected_version": comment["version"],
                },
                headers=headers(owner),
            )
        ).json()["data"]
        assert (await client.get(listing)).json()["data"]["items"][0]["body"] == "普通用户留言"
        login(client, member)
        edited = await client.patch(
            f"/api/comments/{comment['id']}",
            json={"body": "修改后重新审核", "expected_version": approved["version"]},
            headers=headers(member),
        )
        assert edited.status_code == 200 and edited.json()["data"]["status"] == "pending"
        assert (await client.get(listing)).json()["data"]["items"] == []
        assert (
            await client.patch(
                f"/api/comments/{comment['id']}",
                json={"body": "旧版本覆盖", "expected_version": approved["version"]},
                headers=headers(member),
            )
        ).status_code == 409
        login(client, owner)
        approved = (
            await client.post(
                f"/api/moderation/comments/{comment['id']}/decision",
                json={
                    "decision": "approve",
                    "reason": "复核",
                    "expected_version": edited.json()["data"]["version"],
                },
                headers=headers(owner),
            )
        ).json()["data"]
        login(client, member)
        reply = (
            await client.post(
                "/api/comments",
                json={
                    **body,
                    "client_id": str(uuid4()),
                    "parent_id": comment["id"],
                    "body": "一级回复",
                },
                headers=headers(member),
            )
        ).json()["data"]
        login(client, owner)
        await client.post(
            f"/api/moderation/comments/{reply['id']}/decision",
            json={"decision": "approve", "reason": "审核", "expected_version": reply["version"]},
            headers=headers(owner),
        )
        assert len((await client.get(listing)).json()["data"]["items"]) == 2
        hidden = (
            await client.post(
                f"/api/moderation/comments/{comment['id']}/decision",
                json={
                    "decision": "hide",
                    "reason": "隐藏",
                    "expected_version": approved["version"],
                },
                headers=headers(owner),
            )
        ).json()["data"]
        assert (await client.get(listing)).json()["data"]["items"] == []
        login(client, member)
        assert (
            await client.request(
                "DELETE",
                f"/api/comments/{comment['id']}",
                json={"expected_version": hidden["version"]},
                headers=headers(member),
            )
        ).status_code == 204
        async with e_case.uows() as uow:
            await uow.repositories.publications.revoke(
                e_case.resource_id, expected_version=0, expected_acl_version=1
            )
        assert (await client.get(listing)).status_code == 404


async def test_reports_only_visible_comments_and_owner_resolves(e_case: ServiceCase) -> None:
    app, member, owner = await social_app(e_case)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login(client, member)
        comment = (
            await client.post(
                "/api/comments",
                json={"client_id": str(uuid4()), "body": "留言板留言"},
                headers=headers(member),
            )
        ).json()["data"]
        body = {"comment_id": comment["id"], "reason": "需要人工审核"}
        assert (
            await client.post("/api/reports", json=body, headers=headers(member))
        ).status_code == 404
        assert (await client.get("/api/moderation/comments")).status_code == 403
        login(client, owner)
        await client.post(
            f"/api/moderation/comments/{comment['id']}/decision",
            json={"decision": "approve", "reason": "审核", "expected_version": comment["version"]},
            headers=headers(owner),
        )
        login(client, member)
        response = await client.post("/api/reports", json=body, headers=headers(member))
        assert response.status_code == 201
        report = response.json()["data"]
        assert (await client.post("/api/reports", json=body, headers=headers(member))).json()[
            "data"
        ]["id"] == report["id"]
        assert (
            await client.post(
                "/api/reports", json={**body, "reason": "另一原因"}, headers=headers(member)
            )
        ).status_code == 409
        assert (await client.get("/api/moderation/reports")).status_code == 403
        login(client, owner)
        assert len((await client.get("/api/moderation/reports")).json()["data"]["items"]) == 1
        resolved = await client.post(
            f"/api/moderation/reports/{report['id']}/resolve",
            json={"resolution": "reviewed", "expected_version": report["version"]},
            headers=headers(owner),
        )
        assert resolved.status_code == 200 and resolved.json()["data"]["status"] == "resolved"
        assert (await client.get("/api/moderation/reports")).json()["data"]["items"] == []


async def test_action_http_ownership_confirmation_idempotency_and_completed_replay(
    e_case: ServiceCase,
) -> None:
    app, member, owner = await social_app(e_case)
    proposed = await app.state.actions.preview(
        owner.session.actor, command(e_case), idempotency_key=uuid4().hex
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login(client, member)
        assert (await client.get(f"/api/actions/{proposed.id}")).status_code == 404
        login(client, owner)
        view = (await client.get(f"/api/actions/{proposed.id}")).json()["data"]
        assert view["summary"] and view["changes"]["public_fields"] == ["body", "title"]
        assert view["expected_acl_version"] == 1 and not view["can_undo"]
        body = {
            "parameters_hash": proposed.parameters_hash,
            "expected_action_version": proposed.version,
        }
        assert (
            await client.post(
                f"/api/actions/{proposed.id}/execute",
                json={**body, "expected_action_version": proposed.version + 100},
                headers=headers(owner),
            )
        ).status_code == 409
        assert (
            await client.post(
                f"/api/actions/{proposed.id}/execute",
                json=body,
                headers={"Origin": "http://localhost:3000"},
            )
        ).status_code == 403
        assert (
            await client.post(
                f"/api/actions/{proposed.id}/execute",
                json={**body, "parameters_hash": "0" * 64},
                headers=headers(owner),
            )
        ).status_code == 409
        assert (
            await client.post(
                f"/api/actions/{proposed.id}/execute",
                json={**body, "target_id": str(uuid4())},
                headers=headers(owner),
            )
        ).status_code == 422
        response = await client.post(
            f"/api/actions/{proposed.id}/execute", json=body, headers=headers(owner)
        )
        assert response.status_code == 202 and response.json()["data"]["status"] == "ready"
        assert (
            await client.post(
                f"/api/actions/{proposed.id}/execute", json=body, headers=headers(owner)
            )
        ).json()["data"]["version"] == response.json()["data"]["version"]
        async with e_case.uows() as uow:
            assert (
                await uow.session.scalar(
                    select(func.count()).select_from(Job).where(Job.kind == "action.execute")
                )
            ) == 1
        cancelled = await client.post(
            f"/api/actions/{proposed.id}/cancel",
            json={"expected_action_version": response.json()["data"]["version"]},
            headers=headers(owner),
        )
        assert cancelled.status_code == 200 and cancelled.json()["data"]["status"] == "cancelled"
        completed = await PublicationService(e_case.uows).revoke_explicit(
            owner.session.actor,
            RevokeCommand(
                resource_id=e_case.resource_id,
                expected_version=0,
                expected_acl_version=1,
                idempotency_key=uuid4().hex,
            ),
        )
        action = (await client.get(f"/api/actions/{completed.action_id}")).json()["data"]
        repeated = await client.post(
            f"/api/actions/{completed.action_id}/execute",
            json={
                "parameters_hash": action["parameters_hash"],
                "expected_action_version": action["version"],
            },
            headers=headers(owner),
        )
        assert repeated.status_code == 202 and repeated.json()["data"]["status"] == "succeeded"


async def test_quota_read_is_own_and_writes_share_origin_csrf_gate(e_case: ServiceCase) -> None:
    app, member, owner = await social_app(e_case)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/me/quota")).status_code == 401
        login(client, member)
        quota = (await client.get("/api/me/quota")).json()["data"]
        assert quota["daily_limit"] == 10 and quota["remaining"] == 10
        assert "user_id" not in quota
        for path in (
            "/api/comments",
            "/api/reports",
            f"/api/actions/{uuid4()}/execute",
            f"/api/actions/{uuid4()}/cancel",
            f"/api/comments/{uuid4()}",
        ):
            method = "PATCH" if path.startswith("/api/comments/") else "POST"
            response = await client.request(
                method,
                path,
                json={},
                headers={
                    "Origin": "https://untrusted.invalid",
                    "X-CSRF-Token": member.session.csrf_token,
                },
            )
            assert (
                response.status_code == 403
                and response.json()["error"]["code"] == "ORIGIN_FORBIDDEN"
            )
        login(client, owner)
        for path in (
            f"/api/moderation/comments/{uuid4()}/decision",
            f"/api/moderation/reports/{uuid4()}/resolve",
        ):
            assert (
                await client.post(path, json={}, headers={"Origin": "http://localhost:3000"})
            ).status_code == 403

from uuid import uuid4

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ConfigDict

from autumn_backend.api.deps import authenticated_actor
from autumn_backend.api.responses import success
from autumn_backend.app import create_app
from autumn_backend.auth.contracts import SessionView
from autumn_backend.config import Environment, Settings
from autumn_backend.errors import DomainError, IdempotencyConflictError
from autumn_backend.policies import ActorContext, ActorRole

pytestmark = pytest.mark.unit


class WriteRejected(DomainError):
    code = "csrf_invalid"


class AuthBoundary:
    async def authenticate(self, token: str) -> SessionView | None:
        if token != "real-cookie":
            return None
        return SessionView(
            ActorContext(
                user_id=uuid4(),
                role=ActorRole.MEMBER,
                auth_session_id=uuid4(),
                step_up_expires_at=None,
                capabilities=frozenset(),
                scope_epoch=0,
            ),
            {},
            "csrf",
            1,
        )

    def verify_write(
        self, session: SessionView, *, origin: str | None, csrf_token: str | None
    ) -> None:
        if origin != "http://localhost:3000" or csrf_token != "csrf":
            raise WriteRejected("secret must not appear")


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int


def foundation_app():  # type: ignore[no-untyped-def]
    app = create_app(Settings(environment=Environment.TEST))
    app.state.auth = AuthBoundary()

    @app.post("/api/test")
    async def write(request: Request, body: Input):  # type: ignore[no-untyped-def]
        authenticated_actor(request)
        return success(request, {"value": body.value})

    @app.get("/api/conflict")
    async def conflict() -> None:
        raise IdempotencyConflictError("PRIVATE DATABASE CONTENT")

    return app


async def test_authenticated_writes_always_require_origin_and_csrf() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=foundation_app()), base_url="http://test"
    ) as client:
        client.cookies.set("autumn_session", "real-cookie")
        for headers in ({}, {"Origin": "http://localhost:3000"}, {"X-CSRF-Token": "csrf"}):
            result = await client.post("/api/test", json={"value": 1}, headers=headers)
            assert result.status_code == 403
            assert "secret" not in result.text
        result = await client.post(
            "/api/test",
            json={"value": 1},
            headers={
                "Origin": "http://localhost:3000",
                "X-CSRF-Token": "csrf",
            },
        )
        assert result.status_code == 200
        assert result.json()["request_id"] == result.headers["x-request-id"]


async def test_validation_errors_do_not_echo_input_or_trust_request_ids() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=foundation_app()), base_url="http://test"
    ) as client:
        result = await client.post(
            "/api/test",
            json={"value": "PASSWORD-SENT-BY-MISTAKE"},
            headers={"X-Request-ID": "attacker"},
        )
        assert result.status_code == 422
        assert result.json()["error"]["code"] == "VALIDATION_ERROR"
        assert "PASSWORD" not in result.text
        assert result.headers["x-request-id"].startswith("req-")


async def test_domain_errors_are_stable_and_private_details_are_redacted() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=foundation_app()), base_url="http://test"
    ) as client:
        result = await client.get("/api/conflict")
        assert result.status_code == 409
        assert result.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        assert "PRIVATE" not in result.text

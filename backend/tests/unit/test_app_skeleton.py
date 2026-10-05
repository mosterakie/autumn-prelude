"""阶段 A1 骨架自检：应用可构造、配置校验正确、探针可用。"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from autumn_backend import __version__
from autumn_backend.config import Environment, Settings
from autumn_backend.observability.logging import redact_sensitive

pytestmark = pytest.mark.unit


class TestSettings:
    def test_defaults_are_local_and_parse(self) -> None:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.environment is Environment.LOCAL
        assert settings.quota_timezone == "Asia/Shanghai"
        assert settings.db_pool_size >= 1

    def test_async_url_normalises_plain_postgres_scheme(self) -> None:
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            database_url="postgresql://user:pw@localhost:5432/db",
        )
        assert settings.async_database_url.startswith("postgresql+asyncpg://")
        assert settings.sync_database_url.startswith("postgresql+psycopg://")

    def test_blank_secret_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="安全密钥不得为空"):
            Settings(_env_file=None, session_secret="   ")  # type: ignore[call-arg]

    def test_invalid_cookie_name_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="非法 Cookie 名"):
            Settings(_env_file=None, cookie_name="bad name")  # type: ignore[call-arg]

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"session_secret": "short"}, "AUTUMN_SESSION_SECRET"),
            ({"csrf_secret": "short"}, "AUTUMN_CSRF_SECRET"),
            ({"cookie_secure": False}, "Secure Cookie"),
            ({"cookie_name": "autumn_session"}, "__Host- 前缀"),
        ],
    )
    def test_production_invariants_fail_closed(
        self, overrides: dict[str, object], message: str
    ) -> None:
        base: dict[str, object] = {
            "environment": Environment.PROD,
            "session_secret": "s" * 40,
            "csrf_secret": "c" * 40,
            "cookie_secure": True,
            "cookie_name": "__Host-autumn_session",
        }
        base.update(overrides)
        with pytest.raises(ValueError, match=message):
            Settings(_env_file=None, **base)  # type: ignore[call-arg]

    def test_production_accepts_compliant_secrets(self) -> None:
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            environment=Environment.PROD,
            session_secret="s" * 40,
            csrf_secret="c" * 40,
            cookie_secure=True,
            cookie_name="__Host-autumn_session",
        )
        assert settings.is_production
        assert not settings.is_test


class TestLoggingRedaction:
    def test_sensitive_keys_are_redacted_recursively(self) -> None:
        event = {
            "event": "auth.login",
            "email": "user@example.com",
            "session_token": "super-secret",
            "nested": {"api_key": "abc", "keep": 1},
            "items": [{"password": "pw"}],
        }
        redacted = redact_sensitive(None, "info", dict(event))
        assert redacted["session_token"] == "[redacted]"
        assert redacted["nested"]["api_key"] == "[redacted]"
        assert redacted["nested"]["keep"] == 1
        assert redacted["items"][0]["password"] == "[redacted]"
        assert redacted["email"] == "user@example.com"


class TestAppSkeleton:
    def test_app_factory_returns_app(self, app: FastAPI) -> None:
        assert isinstance(app, FastAPI)
        assert app.state.settings.environment is Environment.TEST

    def test_healthz(self, client: TestClient) -> None:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "version": __version__}

    def test_readyz_reports_environment(self, client: TestClient) -> None:
        response = client.get("/readyz")
        assert response.status_code == 200
        body = response.json()
        assert body["environment"] == "test"
        assert body["database_configured"] is True
        # 探针不得泄露任何密钥或完整 DSN。
        assert "password" not in response.text.lower()
        assert "asyncpg" not in response.text

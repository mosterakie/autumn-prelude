from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from autumn_backend.app import create_app
from autumn_backend.auth.security import AuthError, token_hash, totp
from autumn_backend.auth.service import AuthService
from autumn_backend.config import Environment, Settings
from autumn_backend.db.enums import AuthTokenPurpose, UserRole
from autumn_backend.db.models import AuthToken, Job, RateLimitBucket, User
from autumn_backend.db.session import UnitOfWorkFactory
from autumn_backend.errors import ConflictError

pytestmark = pytest.mark.integration
PASSWORD = "秋序密码-strong-password"


@pytest.fixture
async def auth_service(engine: AsyncEngine) -> AsyncIterator[AuthService]:
    async with engine.connect() as connection:
        async with connection.begin():
            uows = UnitOfWorkFactory(
                async_sessionmaker(
                    connection,
                    expire_on_commit=False,
                    autoflush=False,
                    join_transaction_mode="create_savepoint",
                )
            )
            yield AuthService(uows, Settings(environment=Environment.TEST))
            await connection.rollback()


async def register(service: AuthService) -> str:
    email = f"f2-{uuid4().hex}@example.com"
    await service.register(
        email=email, password=PASSWORD, display_name="秋序读者", remote_address="127.0.0.1"
    )
    return email


async def mail_token(service: AuthService, purpose: AuthTokenPurpose) -> tuple[str, Job]:
    async with service.uows() as uow:
        job = (
            await uow.session.execute(
                select(Job)
                .where(Job.kind == "auth.email", Job.payload["purpose"].astext == purpose.value)
                .order_by(Job.created_at.desc(), Job.id.desc())
                .limit(1)
            )
        ).scalar_one()
        raw = service.box.open(job.payload["ciphertext"])
        return raw, job


async def test_registration_verification_cooldown_and_encrypted_mail(
    auth_service: AuthService,
) -> None:
    service = auth_service
    email = await register(service)
    await service.register(
        email="  " + email.upper() + "  ",
        password="different-password-123",
        display_name="重复请求",
        remote_address="127.0.0.1",
    )
    raw, job = await mail_token(service, AuthTokenPurpose.VERIFY_EMAIL)
    async with service.uows() as uow:
        user = await uow.repositories.users.by_email(email)
        assert user is not None and user.role is UserRole.MEMBER
        assert user.password_hash.startswith("$argon2id$")
        assert (
            await uow.session.execute(
                select(func.count()).select_from(User).where(User.email_normalized == email)
            )
        ).scalar_one() == 1
        token = await uow.repositories.auth_credentials.token(
            token_hash(raw), AuthTokenPurpose.VERIFY_EMAIL
        )
        assert token is not None and token.token_hash != raw
        assert raw not in str(job.payload) and email not in str(job.payload)
    await service.consume_token(
        raw=raw, purpose=AuthTokenPurpose.VERIFY_EMAIL, remote_address="127.0.0.1"
    )
    async with service.uows() as uow:
        user = await uow.repositories.users.by_email(email)
        assert user is not None and user.verified_at is not None
        assert user.ai_cooldown_until == user.verified_at + timedelta(hours=24)
        refreshed = await uow.repositories.jobs.get(job.id)
        assert refreshed is not None and "ciphertext" not in refreshed.payload
    with pytest.raises(AuthError, match="invalid_token"):
        await service.consume_token(
            raw=raw, purpose=AuthTokenPurpose.VERIFY_EMAIL, remote_address="127.0.0.1"
        )


async def test_login_rotation_csrf_and_password_reset_revoke_sessions(
    auth_service: AuthService,
) -> None:
    service = auth_service
    email = await register(service)
    login = await service.login(email=email, password=PASSWORD, remote_address="127.0.0.1")
    assert (await service.authenticate(login.token)) is not None
    service.verify_write(
        login.session, origin="http://localhost:3000", csrf_token=login.session.csrf_token
    )
    with pytest.raises(AuthError, match="origin_forbidden"):
        service.verify_write(
            login.session, origin="https://evil.example", csrf_token=login.session.csrf_token
        )
    rotated = await service.login(
        email=email,
        password=PASSWORD,
        remote_address="127.0.0.1",
        previous=login.session.actor.auth_session_id,
    )
    assert rotated.session.csrf_version > login.session.csrf_version
    assert rotated.token != login.token and await service.authenticate(login.token) is None
    with pytest.raises(AuthError, match="csrf_invalid"):
        service.verify_write(
            rotated.session, origin="http://localhost:3000", csrf_token=login.session.csrf_token
        )
    await service.request_email(
        email=email, purpose=AuthTokenPurpose.RESET_PASSWORD, remote_address="127.0.0.1"
    )
    raw, _ = await mail_token(service, AuthTokenPurpose.RESET_PASSWORD)
    await service.consume_token(
        raw=raw,
        purpose=AuthTokenPurpose.RESET_PASSWORD,
        new_password="new-password-with-length",
        remote_address="127.0.0.1",
    )
    assert await service.authenticate(rotated.token) is None
    with pytest.raises(AuthError, match="invalid_credentials"):
        await service.login(email=email, password=PASSWORD, remote_address="127.0.0.1")
    new = await service.login(
        email=email, password="new-password-with-length", remote_address="127.0.0.1"
    )
    assert new.session.csrf_version > rotated.session.csrf_version


async def test_owner_bootstrap_requires_totp_and_recovery_rotates_once(
    auth_service: AuthService,
) -> None:
    service = auth_service
    email, secret = f"owner-{uuid4().hex}@example.com", service.new_totp_secret()
    async with service.uows() as uow:
        now = await uow.repositories.users.database_time()
    code = totp(secret, int(now.timestamp()) // 30)
    recovery = await service.bootstrap_owner(
        email=email, password=PASSWORD, display_name="站长", totp_secret=secret, code=code
    )
    with pytest.raises(ConflictError):
        await service.bootstrap_owner(
            email=f"another-{email}",
            password=PASSWORD,
            display_name="另一站长",
            totp_secret=secret,
            code=code,
        )
    login = await service.login(email=email, password=PASSWORD, remote_address="127.0.0.1")
    assert login.session.actor.step_up_expires_at is None
    with pytest.raises(AuthError, match="invalid_credentials"):
        await service.step_up(
            login.session.actor, code=code, recovery_code=None, remote_address="127.0.0.1"
        )
    elevated = await service.step_up(
        login.session.actor, code=None, recovery_code=recovery[0], remote_address="127.0.0.1"
    )
    assert elevated.session.actor.step_up_expires_at is not None
    assert "search_web" in elevated.session.user["capabilities"]
    assert await service.authenticate(login.token) is None
    with pytest.raises(AuthError, match="invalid_credentials"):
        await service.step_up(
            elevated.session.actor, code=None, recovery_code=recovery[0], remote_address="127.0.0.1"
        )


async def test_failed_login_keeps_rate_evidence_and_invalid_token_does_not_modify_users(
    auth_service: AuthService,
) -> None:
    service = auth_service
    email = await register(service)
    with pytest.raises(AuthError, match="invalid_credentials"):
        await service.login(email=email, password="bad-password", remote_address="127.0.0.1")
    with pytest.raises(AuthError, match="invalid_token"):
        await service.consume_token(
            raw="x" * 43, purpose=AuthTokenPurpose.VERIFY_EMAIL, remote_address="127.0.0.1"
        )
    async with service.uows() as uow:
        counts = (
            (
                await uow.session.execute(
                    select(RateLimitBucket.hits).where(
                        RateLimitBucket.policy_key == "auth.login.identity"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert counts == [1]
        user = await uow.repositories.users.by_email(email)
        assert user is not None and user.verified_at is None
        assert (
            await uow.session.execute(
                select(func.count())
                .select_from(AuthToken)
                .where(AuthToken.consumed_at.is_not(None))
            )
        ).scalar_one() == 0


async def test_auth_routes_cookie_csrf_origin_and_uniform_registration(
    auth_service: AuthService,
) -> None:
    app = create_app(auth_service.settings)
    app.state.auth = auth_service
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/auth/me")).json()["data"]["user"] is None
        email = f"api-{uuid4().hex}@example.com"
        body = {"email": email, "password": PASSWORD, "display_name": "访客"}
        assert (await client.post("/api/auth/register", json=body)).status_code == 403
        origin = {"Origin": "http://localhost:3000"}
        first = await client.post("/api/auth/register", json=body, headers=origin)
        duplicate = await client.post("/api/auth/register", json=body, headers=origin)
        assert first.status_code == duplicate.status_code == 202
        assert first.json()["data"] == duplicate.json()["data"] == {"accepted": True}
        injected = await client.post(
            "/api/auth/register", json={**body, "role": "owner"}, headers=origin
        )
        assert injected.status_code == 422 and PASSWORD not in injected.text
        raw, _ = await mail_token(auth_service, AuthTokenPurpose.VERIFY_EMAIL)
        verified = await client.post("/api/auth/verify-email", json={"token": raw}, headers=origin)
        assert verified.status_code == 200
        login = await client.post(
            "/api/auth/login", json={"email": email, "password": PASSWORD}, headers=origin
        )
        assert login.status_code == 200
        assert (
            "HttpOnly" in login.headers["set-cookie"]
            and "SameSite=lax" in login.headers["set-cookie"]
        )
        assert (
            "Path=/" in login.headers["set-cookie"] and "Domain=" not in login.headers["set-cookie"]
        )
        assert login.headers["cache-control"] == "no-store"
        assert login.json()["data"]["server_time"].endswith("Z")
        assert "password_hash" not in login.text and raw not in login.text
        csrf = login.json()["data"]["csrf_token"]
        assert (await client.post("/api/auth/logout", headers=origin)).status_code == 403
        assert (
            await client.post(
                "/api/auth/logout", headers={"Origin": "https://evil.example", "X-CSRF-Token": csrf}
            )
        ).status_code == 403
        me = await client.get("/api/auth/me")
        assert me.json()["data"]["user"]["email"] == email
        logout = await client.post("/api/auth/logout", headers={**origin, "X-CSRF-Token": csrf})
        assert logout.status_code == 204
        assert (await client.get("/api/auth/me")).json()["data"]["user"] is None


async def test_http_step_up_rotates_cookie_and_csrf(auth_service: AuthService) -> None:
    service = auth_service
    async with service.uows() as uow:
        now = await uow.repositories.users.database_time()
    email, secret = f"api-owner-{uuid4().hex}@example.com", service.new_totp_secret()
    recovery = await service.bootstrap_owner(
        email=email,
        password=PASSWORD,
        display_name="站长",
        totp_secret=secret,
        code=totp(secret, int(now.timestamp()) // 30),
    )
    app = create_app(service.settings)
    app.state.auth = service
    origin = {"Origin": "http://localhost:3000"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post(
            "/api/auth/login", json={"email": email, "password": PASSWORD}, headers=origin
        )
        old = client.cookies.get("autumn_session")
        assert login.json()["data"]["user"]["step_up_expires_at"] is None
        csrf = login.json()["data"]["csrf_token"]
        upgraded = await client.post(
            "/api/auth/step-up",
            json={"recovery_code": recovery[0]},
            headers={**origin, "X-CSRF-Token": csrf},
        )
        assert upgraded.status_code == 200 and client.cookies.get("autumn_session") != old
        assert upgraded.json()["data"]["csrf_token"] != csrf
        assert "search_web" in upgraded.json()["data"]["user"]["capabilities"]
        assert secret not in upgraded.text and recovery[0] not in upgraded.text
        client.cookies.clear()
        client.cookies.set("autumn_session", old)
        expired = await client.get("/api/auth/me")
        assert expired.json()["data"]["user"] is None
        assert "Max-Age=0" in expired.headers["set-cookie"]


async def test_cors_and_two_browser_sessions_remain_independent(auth_service: AuthService) -> None:
    app = create_app(auth_service.settings)
    app.state.auth = auth_service
    first_email, second_email = await register(auth_service), await register(auth_service)
    origin = {"Origin": "http://localhost:3000"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as first:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as second:
            for client, email in ((first, first_email), (second, second_email)):
                login = await client.post(
                    "/api/auth/login", json={"email": email, "password": PASSWORD}, headers=origin
                )
                assert login.status_code == 200
                assert login.headers["access-control-allow-origin"] == origin["Origin"]
            assert (await first.get("/api/auth/me")).json()["data"]["user"]["email"] == first_email
            assert (await second.get("/api/auth/me")).json()["data"]["user"][
                "email"
            ] == second_email
            allowed = await first.options(
                "/api/auth/login",
                headers={
                    **origin,
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type,x-csrf-token",
                },
            )
            assert allowed.status_code == 200
            denied = await first.options(
                "/api/auth/login",
                headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
            )
            assert denied.status_code == 400

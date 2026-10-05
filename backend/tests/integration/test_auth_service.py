from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

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

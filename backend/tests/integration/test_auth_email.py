"""邮件边界基础检查：真实 PostgreSQL 回滚，SMTP 传输替身，不向外发信。"""

import smtplib
import ssl
from collections.abc import AsyncIterator
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from autumn_backend.auth.security import AuthError
from autumn_backend.auth.service import AuthService
from autumn_backend.config import Environment, Settings
from autumn_backend.db.enums import AuthTokenPurpose, JobStatus, ProviderCallStatus
from autumn_backend.db.models import AuthToken, Job, ProviderCall
from autumn_backend.db.session import UnitOfWorkFactory
from autumn_backend.io_boundary import require_outside_uow
from autumn_backend.jobs.queue import LeasedJob
from autumn_backend.providers.mail import MailMessage, MailSendError, SMTPMailer
from autumn_backend.services.auth_email import AuthEmailService
from autumn_backend.services.storage import StorageService
from autumn_backend.storage.local import LocalObjectStore
from autumn_backend.workers.bootstrap import configured_worker

pytestmark = pytest.mark.integration
PASSWORD = "autumn-test-password-123"


def mail_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment=Environment.TEST,
        smtp_host="smtp.example.com",
        smtp_username="sender@example.com",
        smtp_password=SecretStr("test-only-authorization-code"),
        smtp_from_email="sender@example.com",
        frontend_base_url="http://localhost:3000",
    )


@pytest.fixture
async def mail_uows(engine: AsyncEngine) -> AsyncIterator[UnitOfWorkFactory]:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            yield UnitOfWorkFactory(
                async_sessionmaker(
                    connection,
                    expire_on_commit=False,
                    autoflush=False,
                    join_transaction_mode="create_savepoint",
                )
            )
        finally:
            await transaction.rollback()


class CaptureMailer:
    def __init__(self, error: MailSendError | None = None) -> None:
        self.messages: list[MailMessage] = []
        self.error = error

    async def send(self, message: MailMessage) -> None:
        require_outside_uow()
        self.messages.append(message)
        if self.error:
            raise self.error


async def issue(
    uows: UnitOfWorkFactory, purpose: AuthTokenPurpose = AuthTokenPurpose.VERIFY_EMAIL
) -> tuple[AuthService, str, str, Job]:
    auth = AuthService(uows, mail_settings())
    email = f"mail-{uuid4().hex}@example.com"
    remote = f"2001:db8::{uuid4().hex[:4]}"
    await auth.register(
        email=email, password=PASSWORD, display_name="邮件测试", remote_address=remote
    )
    if purpose is AuthTokenPurpose.RESET_PASSWORD:
        await auth.request_email(email=email, purpose=purpose, remote_address=remote)
    async with uows() as uow:
        user = await uow.repositories.users.by_email(email)
        assert user is not None
        job = (
            await uow.session.scalars(
                select(Job).where(
                    Job.kind == "auth.email",
                    Job.actor_id == user.id,
                    Job.payload["purpose"].astext == purpose.value,
                )
            )
        ).one()
        return auth, email, remote, job


async def lease(uows: UnitOfWorkFactory, identifier: UUID) -> LeasedJob:
    # 只租用本用例的任务，避免 claim 到验收库中用户自己创建的邮件。
    async with uows() as uow:
        job = await uow.repositories.jobs.get_for_update_or_raise(identifier)
        now = await uow.repositories.jobs.database_time()
        job.status, job.lease_token = JobStatus.RUNNING, uuid4()
        job.lease_expires_at, job.heartbeat_at = now + timedelta(seconds=60), now
        job.attempts += 1
        job.version += 1
        await uow.session.flush()
        return LeasedJob(job.id, job.kind, job.lease_token)


@pytest.mark.parametrize("mode", ["accepted", "authentication", "disconnect"])
async def test_smtp_tls_acceptance_and_safe_failure(monkeypatch, mode: str) -> None:
    captured = {}

    class Transport:
        def __init__(self, host, port, *, timeout, context):
            assert (host, port, timeout) == ("smtp.example.com", 465, 20)
            assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
            assert context.minimum_version >= ssl.TLSVersion.TLSv1_2

        def login(self, username, password):
            assert username == "sender@example.com"
            assert password == "test-only-authorization-code"
            if mode == "authentication":
                raise smtplib.SMTPAuthenticationError(535, b"private-provider-detail")

        def send_message(self, message, *, from_addr, to_addrs):
            assert from_addr == "sender@example.com" and to_addrs == ["reader@example.com"]
            captured["mail"] = message
            if mode == "disconnect":
                raise smtplib.SMTPServerDisconnected("private-provider-detail")
            return {}

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(smtplib, "SMTP_SSL", Transport)
    message = MailMessage("reader@example.com", "秋序 · 验证邮箱", "正文", "<test@example.com>")
    mailer = SMTPMailer(mail_settings())
    if mode == "accepted":
        await mailer.send(message)
        assert captured["mail"]["Subject"] == message.subject
        assert captured["mail"].get_content().strip() == "正文"
    else:
        with pytest.raises(MailSendError) as caught:
            await mailer.send(message)
        assert caught.value.outcome == ("failed" if mode == "authentication" else "unknown")
        assert str(caught.value) == (
            "SMTP_AUTH_FAILED" if mode == "authentication" else "SMTP_OUTCOME_UNKNOWN"
        )
    assert captured["closed"] is True


@pytest.mark.parametrize("purpose", list(AuthTokenPurpose))
async def test_email_link_consumption_and_cleanup(mail_uows, purpose) -> None:
    auth, email, remote, job = await issue(mail_uows, purpose)
    mailer = CaptureMailer()
    service = AuthEmailService(mail_uows, mail_settings(), mailer)
    storage = StorageService(
        mail_uows, LocalObjectStore(mail_settings().storage_root / "mail-tests-never-written")
    )
    assert "auth.email" not in configured_worker(mail_uows, storage).registry.kinds
    assert "auth.email" in configured_worker(mail_uows, storage, email=service).registry.kinds
    assert not mail_settings().model_copy(update={"smtp_password": SecretStr("")}).mail_enabled
    await service.execute(await lease(mail_uows, job.id))
    assert len(mailer.messages) == 1 and mailer.messages[0].recipient == email
    link = next(line for line in mailer.messages[0].body.splitlines() if line.startswith("http"))
    parsed = urlsplit(link)
    assert parsed.query == "" and parsed.netloc == "localhost:3000"
    assert parsed.path == (
        "/verify-email" if purpose is AuthTokenPurpose.VERIFY_EMAIL else "/reset-password"
    )
    raw = parse_qs(parsed.fragment)["token"][0]
    async with mail_uows() as uow:
        saved = await uow.repositories.jobs.get_or_raise(job.id)
        call = (
            await uow.session.scalars(select(ProviderCall).where(ProviderCall.job_id == job.id))
        ).one()
        assert saved.status is JobStatus.SUCCEEDED and "ciphertext" not in saved.payload
        assert call.status is ProviderCallStatus.SUCCEEDED
        assert raw not in str(saved.payload) and raw not in repr(mailer.messages[0])
    await auth.consume_token(
        raw=raw,
        purpose=purpose,
        remote_address=remote,
        new_password="changed-autumn-password-123"
        if purpose is AuthTokenPurpose.RESET_PASSWORD
        else None,
    )
    with pytest.raises(AuthError, match="invalid_token"):
        await auth.consume_token(
            raw=raw,
            purpose=purpose,
            remote_address=remote,
            new_password="changed-autumn-password-123"
            if purpose is AuthTokenPurpose.RESET_PASSWORD
            else None,
        )
    if purpose is AuthTokenPurpose.RESET_PASSWORD:
        await auth.login(email=email, password="changed-autumn-password-123", remote_address=remote)
    else:
        async with mail_uows() as uow:
            user = await uow.repositories.users.by_email(email)
            assert user is not None and user.verified_at is not None


async def test_expired_mail_is_cancelled_without_smtp(mail_uows) -> None:
    _, _, _, job = await issue(mail_uows)
    mailer = CaptureMailer()
    async with mail_uows() as uow:
        token = await uow.session.get(AuthToken, UUID(job.payload["token_id"]))
        assert token is not None
        token.expires_at = await uow.repositories.users.database_time() - timedelta(seconds=1)
        token.created_at = token.expires_at - timedelta(hours=1)
    await AuthEmailService(mail_uows, mail_settings(), mailer).execute(
        await lease(mail_uows, job.id)
    )
    async with mail_uows() as uow:
        saved = await uow.repositories.jobs.get_or_raise(job.id)
        assert saved.status is JobStatus.CANCELLED and "ciphertext" not in saved.payload
    assert not mailer.messages
    # 未启用 SMTP 时，维护也能清除排队的过期凭据。
    _, _, _, queued = await issue(mail_uows)
    async with mail_uows() as uow:
        token = await uow.session.get(AuthToken, UUID(queued.payload["token_id"]))
        assert token is not None
        token.expires_at = await uow.repositories.users.database_time() - timedelta(seconds=1)
        token.created_at = token.expires_at - timedelta(hours=1)
        await uow.session.flush()
        await uow.repositories.auth_credentials.scrub_expired_mail(limit=1000)
    async with mail_uows() as uow:
        saved = await uow.repositories.jobs.get_or_raise(queued.id)
        assert saved.status is JobStatus.CANCELLED and "ciphertext" not in saved.payload


async def test_dispatch_reentry_is_blocked(mail_uows) -> None:
    _, _, _, job = await issue(mail_uows)
    mailer = CaptureMailer()
    service = AuthEmailService(mail_uows, mail_settings(), mailer)
    leased = await lease(mail_uows, job.id)
    assert await service.prepare(leased) is not None
    # 已派发后异常退出：重入不能把结果未知的调用当成未发送。
    await service.execute(leased)
    async with mail_uows() as uow:
        saved = await uow.repositories.jobs.get_or_raise(job.id)
        calls = (
            await uow.session.scalars(select(ProviderCall).where(ProviderCall.job_id == job.id))
        ).all()
        assert saved.status is JobStatus.FAILED and "ciphertext" not in saved.payload
        assert len(calls) == 1 and calls[0].status is ProviderCallStatus.UNKNOWN
    assert not mailer.messages


async def test_unknown_smtp_outcome_settles_without_retry(mail_uows) -> None:
    _, _, _, job = await issue(mail_uows)
    mailer = CaptureMailer(MailSendError("SMTP_OUTCOME_UNKNOWN", outcome="unknown"))
    await AuthEmailService(mail_uows, mail_settings(), mailer).execute(
        await lease(mail_uows, job.id)
    )
    async with mail_uows() as uow:
        saved = await uow.repositories.jobs.get_or_raise(job.id)
        call = (
            await uow.session.scalars(select(ProviderCall).where(ProviderCall.job_id == job.id))
        ).one()
        assert saved.status is JobStatus.FAILED and saved.error_code == "SMTP_OUTCOME_UNKNOWN"
        assert "ciphertext" not in saved.payload and call.status is ProviderCallStatus.UNKNOWN
    assert len(mailer.messages) == 1

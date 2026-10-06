"""auth.email 固定处理器；匿名注册令牌不依赖已登录 Session，未知投递不重发。"""

import hmac
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlencode
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from autumn_backend.auth.security import SecretBox, token_hash
from autumn_backend.config import Settings
from autumn_backend.db.enums import (
    AuthTokenPurpose,
    JobStatus,
    ProviderCallPurpose,
    ProviderCallStatus,
    UserStatus,
)
from autumn_backend.db.models import AuthToken, Job, ProviderCall
from autumn_backend.db.session import UnitOfWork, UnitOfWorkFactory
from autumn_backend.jobs.queue import LeasedJob
from autumn_backend.providers.mail import Mailer, MailMessage, MailSendError


class MailPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    schema_version: Literal[1]
    token_id: UUID
    purpose: AuthTokenPurpose
    encryption_key_version: Literal[1]
    ciphertext: str = Field(min_length=1, repr=False)


@dataclass(frozen=True, slots=True)
class PreparedMail:
    call_id: UUID
    message: MailMessage = field(repr=False)


class AuthEmailService:
    def __init__(self, uows: UnitOfWorkFactory, settings: Settings, mailer: Mailer) -> None:
        self.uows, self.settings, self.mailer = uows, settings, mailer
        self.box = SecretBox(settings.auth_encryption_key.get_secret_value())

    @staticmethod
    def _scrub(job: Job) -> None:
        job.payload = {
            key: value for key, value in (job.payload or {}).items() if key != "ciphertext"
        }
        job.version += 1

    async def _cancel(self, uow: UnitOfWork, job: Job, leased: LeasedJob, code: str) -> None:
        self._scrub(job)
        await uow.session.flush()
        await uow.repositories.jobs.finish(
            job.id, leased.token, status=JobStatus.CANCELLED, error_code=code
        )

    async def prepare(self, leased: LeasedJob) -> PreparedMail | None:
        async with self.uows() as uow:
            probe = await uow.repositories.jobs.get_or_raise(leased.id)
            user = (
                await uow.repositories.users.get_for_update(probe.actor_id)
                if probe.actor_id
                else None
            )
            job = await uow.repositories.jobs.require_lease(leased.id, leased.token)
            try:
                payload = MailPayload.model_validate(job.payload)
            except ValidationError:
                await self._cancel(uow, job, leased, "MAIL_PAYLOAD_INVALID")
                return None
            token = await uow.session.scalar(
                select(AuthToken)
                .where(
                    AuthToken.id == payload.token_id,
                    AuthToken.user_id == job.actor_id,
                    AuthToken.purpose == payload.purpose,
                )
                .with_for_update()
            )
            now = await uow.repositories.users.database_time()
            if (
                leased.kind != "auth.email"
                or job.kind != leased.kind
                or user is None
                or user.deleted_at is not None
                or user.status is UserStatus.DISABLED
                or token is None
                or token.consumed_at is not None
                or token.expires_at <= now
                or (
                    payload.purpose is AuthTokenPurpose.VERIFY_EMAIL
                    and user.verified_at is not None
                )
            ):
                await self._cancel(uow, job, leased, "MAIL_TOKEN_INACTIVE")
                return None
            try:
                raw = self.box.open(payload.ciphertext)
            except Exception:
                await self._cancel(uow, job, leased, "MAIL_TOKEN_INVALID")
                return None
            if not hmac.compare_digest(token_hash(raw), token.token_hash):
                await self._cancel(uow, job, leased, "MAIL_TOKEN_INVALID")
                return None
            logical = f"auth.email:{job.id}"
            call = (
                await uow.repositories.provider_calls.prepare(
                    provider="smtp",
                    purpose=ProviderCallPurpose.EMAIL,
                    logical_call_key=logical,
                    job_id=job.id,
                    model="smtp-ssl",
                )
            ).record
            if call.status is not ProviderCallStatus.PREPARED:
                if call.status is ProviderCallStatus.DISPATCHED:
                    await uow.repositories.provider_calls.settle_unknown(
                        call.id, error_code="MAIL_REPLAY_BLOCKED"
                    )
                self._scrub(job)
                await uow.session.flush()
                await uow.repositories.jobs.finish(
                    job.id,
                    leased.token,
                    status=JobStatus.SUCCEEDED
                    if call.status is ProviderCallStatus.SUCCEEDED
                    else JobStatus.FAILED,
                    error_code=None
                    if call.status is ProviderCallStatus.SUCCEEDED
                    else "MAIL_REPLAY_BLOCKED",
                )
                return None
            verify = payload.purpose is AuthTokenPurpose.VERIFY_EMAIL
            path = "/verify-email" if verify else "/reset-password"
            link = self.settings.frontend_base_url + path + "#" + urlencode({"token": raw})
            message = MailMessage(
                recipient=user.email_normalized,
                subject="秋序 · 验证邮箱" if verify else "秋序 · 重置密码",
                body=(
                    "请打开以下链接"
                    + ("验证邮箱" if verify else "重置密码")
                    + "：\n\n"
                    + link
                    + "\n\n有效期至 "
                    + token.expires_at.isoformat()
                    + "（UTC）。链接只能使用一次。\n如果不是你发起的请求，可以忽略这封邮件。"
                ),
                message_id=f"<autumn-{job.id}@{self.settings.smtp_from_email.split('@')[-1] if self.settings.smtp_from_email else 'autumn.local'}>",
            )
            await uow.repositories.provider_calls.mark_dispatched(call.id)
            return PreparedMail(call.id, message)

    async def execute(self, leased: LeasedJob) -> None:
        prepared = await self.prepare(leased)
        if prepared is None:
            return
        try:
            await self.mailer.send(prepared.message)
        except MailSendError as error:
            await self.finish(leased, prepared.call_id, outcome=error.outcome, code=error.code)
        except Exception:
            await self.finish(
                leased, prepared.call_id, outcome="unknown", code="SMTP_OUTCOME_UNKNOWN"
            )
        else:
            await self.finish(leased, prepared.call_id, outcome="succeeded")

    async def finish(
        self,
        leased: LeasedJob,
        call_id: UUID,
        *,
        outcome: Literal["succeeded", "failed", "unknown"],
        code: str | None = None,
    ) -> None:
        async with self.uows() as uow:
            probe = await uow.repositories.jobs.get_or_raise(leased.id)
            if probe.actor_id:
                await uow.repositories.users.get_for_update_or_raise(probe.actor_id)
            job = await uow.repositories.jobs.require_lease(leased.id, leased.token)
            if outcome == "succeeded":
                await uow.repositories.provider_calls.settle_succeeded(call_id)
            elif outcome == "failed":
                await uow.repositories.provider_calls.settle_failed(
                    call_id, error_code=code or "SMTP_FAILED"
                )
            else:
                await uow.repositories.provider_calls.settle_unknown(
                    call_id, error_code=code or "SMTP_OUTCOME_UNKNOWN"
                )
            self._scrub(job)
            await uow.session.flush()
            await uow.repositories.jobs.finish(
                job.id,
                leased.token,
                status=JobStatus.SUCCEEDED if outcome == "succeeded" else JobStatus.FAILED,
                result={"accepted_by_smtp": True} if outcome == "succeeded" else {},
                error_code=code,
            )

    async def failure(self, leased: LeasedJob, error: Exception) -> None:
        async with self.uows() as uow:
            probe = await uow.repositories.jobs.get_or_raise(leased.id)
            if probe.actor_id:
                await uow.repositories.users.get_for_update_or_raise(probe.actor_id)
            job = await uow.repositories.jobs.require_lease(leased.id, leased.token)
            calls = (
                await uow.session.scalars(
                    select(ProviderCall)
                    .where(
                        ProviderCall.job_id == job.id,
                        ProviderCall.status == ProviderCallStatus.DISPATCHED,
                    )
                    .with_for_update()
                )
            ).all()
            for call in calls:
                await uow.repositories.provider_calls.settle_unknown(
                    call.id, error_code="SMTP_OUTCOME_UNKNOWN"
                )
            self._scrub(job)
            await uow.session.flush()
            await uow.repositories.jobs.finish(
                job.id, leased.token, status=JobStatus.FAILED, error_code="MAIL_HANDLER_FAILED"
            )

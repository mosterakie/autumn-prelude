"""固定邮件端口与 TLS SMTP；凭据/正文不进异常，网络在线程中且在 UoW 外。"""

import asyncio
import smtplib
import ssl
from contextlib import suppress
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr
from typing import Literal, Protocol

from autumn_backend.config import Settings
from autumn_backend.io_boundary import require_outside_uow


@dataclass(frozen=True, slots=True)
class MailMessage:
    recipient: str = field(repr=False)
    subject: str
    body: str = field(repr=False)
    message_id: str


class Mailer(Protocol):
    async def send(self, message: MailMessage) -> None: ...


class MailSendError(Exception):
    def __init__(self, code: str, *, outcome: Literal["failed", "unknown"]) -> None:
        self.code, self.outcome = code, outcome
        super().__init__(code)


class SMTPMailer:
    def __init__(self, settings: Settings) -> None:
        if not settings.mail_enabled:
            raise ValueError("SMTP 未配置授权码")
        self._settings = settings

    async def send(self, message: MailMessage) -> None:
        require_outside_uow()
        await asyncio.to_thread(self._send, message)

    def _send(self, message: MailMessage) -> None:
        settings = self._settings
        assert settings.smtp_host and settings.smtp_username and settings.smtp_password
        assert settings.smtp_from_email
        mail = EmailMessage()
        mail["From"] = formataddr((settings.smtp_from_name, settings.smtp_from_email))
        mail["To"], mail["Subject"], mail["Message-ID"] = (
            message.recipient,
            message.subject,
            message.message_id,
        )
        mail.set_content(message.body)
        client: smtplib.SMTP_SSL | None = None
        submitting = False
        try:
            context = ssl.create_default_context()
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            client = smtplib.SMTP_SSL(
                settings.smtp_host,
                settings.smtp_port,
                timeout=settings.smtp_timeout_seconds,
                context=context,
            )
            client.login(settings.smtp_username, settings.smtp_password.get_secret_value())
            submitting = True
            refused = client.send_message(
                mail, from_addr=settings.smtp_from_email, to_addrs=[message.recipient]
            )
            if refused:
                raise MailSendError("SMTP_RECIPIENT_REJECTED", outcome="failed")
        except smtplib.SMTPAuthenticationError:
            raise MailSendError("SMTP_AUTH_FAILED", outcome="failed") from None
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError):
            raise MailSendError("SMTP_REJECTED", outcome="failed") from None
        except MailSendError:
            raise
        except (OSError, smtplib.SMTPException):
            raise MailSendError(
                "SMTP_OUTCOME_UNKNOWN" if submitting else "SMTP_CONNECTION_FAILED",
                outcome="unknown" if submitting else "failed",
            ) from None
        finally:
            if client is not None:
                # DATA 成功后不因 QUIT 的网络错误把已接受邮件标为失败。
                with suppress(OSError, smtplib.SMTPException):
                    client.close()

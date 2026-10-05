"""密码/令牌/CSRF/TOTP 原语；密码计算在线程中且事务外执行。"""

import asyncio
import base64
import hashlib
import hmac
import re
import secrets
import struct
from uuid import UUID

from cryptography.exceptions import InvalidKey
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

from autumn_backend.errors import ConfigurationError, DomainError, InvalidInputError
from autumn_backend.io_boundary import require_outside_uow


class AuthError(DomainError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def normalize_email(email: str) -> str:
    value = email.strip().lower()
    if len(value) > 254 or not re.fullmatch(
        r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,63}", value
    ):
        raise InvalidInputError("邮箱格式无效")
    local, domain = value.split("@")
    if len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in value:
        raise InvalidInputError("邮箱格式无效")
    if any(
        not label or label.startswith("-") or label.endswith("-") or len(label) > 63
        for label in domain.split(".")
    ):
        raise InvalidInputError("邮箱格式无效")
    return value


def validate_password(password: str) -> None:
    if not 12 <= len(password) <= 256 or len(password.encode()) > 1024:
        raise InvalidInputError("密码长度必须为 12–256 字符")


def random_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _hash(password: str) -> str:
    return Argon2id(
        salt=secrets.token_bytes(16), length=32, iterations=3, lanes=1, memory_cost=64 * 1024
    ).derive_phc_encoded(password.encode())


async def hash_password(password: str) -> str:
    require_outside_uow()
    validate_password(password)
    return await asyncio.to_thread(_hash, password)


def _verify(password: str, encoded: str) -> bool:
    try:
        Argon2id.verify_phc_encoded(password.encode(), encoded)
        return True
    except (InvalidKey, ValueError):
        return False


async def verify_password(password: str, encoded: str | None) -> bool:
    require_outside_uow()
    if encoded is None:
        await asyncio.to_thread(_hash, "dummy-account-password")
        return False
    return await asyncio.to_thread(_verify, password, encoded)


def csrf_signature(secret: str, session_id: UUID, version: int) -> str:
    return hmac.new(
        secret.encode(), f"csrf:v1:{session_id}:{version}".encode(), hashlib.sha256
    ).hexdigest()


def totp(secret: str, time_step: int, *, digits: int = 6) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", time_step), hashlib.sha1).digest()
    offset = digest[-1] & 15
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**digits).zfill(digits)


def totp_step(secret: str, code: str, current: int, last: int | None) -> int | None:
    if not re.fullmatch(r"[0-9]{6}", code):
        return None
    for step in (current, current - 1, current + 1):
        if (
            step >= 0
            and (last is None or step > last)
            and hmac.compare_digest(totp(secret, step), code)
        ):
            return step
    return None


class SecretBox:
    """v1 主密钥来自环境配置；更换主密钥须先迁移密文。"""

    def __init__(self, key: str) -> None:
        self._fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))

    def seal(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def open(self, ciphertext: str, *, ttl: int | None = None) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode(), ttl=ttl).decode()
        except (InvalidToken, UnicodeError) as error:
            raise ConfigurationError("认证密文无效或密钥版本不匹配") from error

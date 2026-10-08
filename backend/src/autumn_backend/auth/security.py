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
    # 邮箱格式验证
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
    # 密码格式验证，目前只做了长度校验
    if not 12 <= len(password) <= 256 or len(password.encode()) > 1024:
        raise InvalidInputError("密码长度必须为 12–256 字符")


def random_token() -> str:
    # 生成安全的随机token
    # secrets 模块是 Python 标准库的一部分，专门用于生成适用于密码、账户验证、会话令牌等安全敏感场景的加密安全随机数。
    # secrets.token_urlsafe() 生成的字符串特别适用于 URL 或 文件系统，因为它只包含 A-Z、a-z、0-9、连字符（-）和下划线（_），避免了需要进行 URL 编码的问题。
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    # 对 token 做 SHA-256，返回十六进制摘要字符串。
    # 在 Python 的 hashlib 模块中，hexdigest() 是一个用于返回散列对象摘要的方法。它会将二进制数据转换为十六进制字符串形式表示的哈希值
    return hashlib.sha256(token.encode()).hexdigest()


def _hash(password: str) -> str:
    # 同步密码哈希：随机 16 字节盐，输出 32 字节，3 次迭代，
    # 1 条并行通道，内存成本 64 MiB（64*1024 KiB）。
    # derive_phc_encoded 返回包含算法、参数、盐和哈希的 PHC 字符串。
    return Argon2id(
        salt=secrets.token_bytes(16), length=32, iterations=3, lanes=1, memory_cost=64 * 1024
    ).derive_phc_encoded(password.encode())


async def hash_password(password: str) -> str:
    require_outside_uow()
    # 异步入口：要求不在 UoW（工作单元/事务）内调用。
    validate_password(password)
    # 将 CPU 密集的 Argon2 放入线程池，避免阻塞事件循环。
    return await asyncio.to_thread(_hash, password)


def _verify(password: str, encoded: str) -> bool:
    # 同步验证 PHC 编码的密码哈希。
    try:
        Argon2id.verify_phc_encoded(password.encode(), encoded)
        return True
    except (InvalidKey, ValueError):
        # 编码格式错误、密钥无效或校验失败统一返回 False。
        return False


async def verify_password(password: str, encoded: str | None) -> bool:
    # 异步验证：要求不在 UoW 内调用。
    require_outside_uow()
    if encoded is None:
        # 账户不存在/无密码时，仍执行一次 dummy 哈希，
        # 使响应时间接近真实验证，降低用户名枚举的时序侧信道。
        await asyncio.to_thread(_hash, "dummy-account-password")
        return False
    # 线程池中执行同步验证。
    return await asyncio.to_thread(_verify, password, encoded)


def csrf_signature(secret: str, session_id: UUID, version: int) -> str:
    # 生成 CSRF 令牌签名：HMAC-SHA256(secret, "csrf:v1:<session_id>:<version>")。
    # 返回十六进制字符串；version 可用于失效旧令牌。
    return hmac.new(
        secret.encode(), f"csrf:v1:{session_id}:{version}".encode(), hashlib.sha256
    ).hexdigest()


def totp(secret: str, time_step: int, *, digits: int = 6) -> str:
    # 按 RFC 6238 思路生成 TOTP。
    # 1) Base32 解码密钥，补齐长度到 8 的倍数，casefold 允许小写。
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    # 2) 将时间步编码为 8 字节大端整数，做 HMAC-SHA1。
    digest = hmac.new(key, struct.pack(">Q", time_step), hashlib.sha1).digest()
    # 3) 动态截断：取最后字节低 4 位作为偏移。
    offset = digest[-1] & 15
    # 4) 取 4 字节，屏蔽最高位，避免有符号影响。
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    # 5) 取模得到 digits 位数字，左侧补零。
    return str(value % 10**digits).zfill(digits)


def totp_step(secret: str, code: str, current: int, last: int | None) -> int | None:
    if not re.fullmatch(r"[0-9]{6}", code):
        # 只接受 6 位纯数字验证码。
        return None
    for step in (current, current - 1, current + 1):
        # 容忍时钟漂移：依次检查当前、上一、下一时间步。
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
        # 将字符串主密钥做 SHA-256，得到 32 字节密钥；
        # 再用 urlsafe Base64 编码，作为 Fernet 所需 key。
        self._fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))

    def seal(self, value: str) -> str:
        # 加密明文字符串，返回 Fernet token 字符串。
        return self._fernet.encrypt(value.encode()).decode()

    def open(self, ciphertext: str, *, ttl: int | None = None) -> str:
        try:
            # 解密 Fernet token；ttl 可选，用于限制密文年龄。
            return self._fernet.decrypt(ciphertext.encode(), ttl=ttl).decode()
        except (InvalidToken, UnicodeError) as error:
            raise ConfigurationError("认证密文无效或密钥版本不匹配") from error

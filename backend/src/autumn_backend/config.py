"""应用配置。

约定：
- 所有环境变量统一带 ``AUTUMN_`` 前缀，只通过环境变量或 ``.env`` 传入。
- 数据库时间统一 UTC；仅"每日额度切分"这类业务时间语义使用命名时区。
- 生产环境缺少安全密钥时启动即失败（fail-closed），不落回可预测的默认值。
"""

from __future__ import annotations

import enum
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, PostgresDsn, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ 目录（本文件位于 backend/src/autumn_backend/config.py）
BACKEND_ROOT = Path(__file__).resolve().parents[2]


class Environment(enum.StrEnum):
    """运行环境。"""

    LOCAL = "local"
    DEV = "dev"
    TEST = "test"
    PROD = "prod"


# 非生产环境使用的占位密钥；只用于本地开发。
# 生产环境会**按值拒绝**这两个常量，并且要求调用方显式提供密钥——
# 仅检查长度是不够的，因为它们本身已经超过 32 字符。
_DEV_SESSION_SECRET = "dev-only-session-secret-do-not-use-in-production"
_DEV_CSRF_SECRET = "dev-only-csrf-secret-do-not-use-in-production"

#: 生产环境必须显式提供、且不得等于这两个占位值的密钥字段。
_REQUIRED_IN_PRODUCTION = ("session_secret", "csrf_secret")

_PLACEHOLDER_SECRETS = frozenset({_DEV_SESSION_SECRET, _DEV_CSRF_SECRET})


class Settings(BaseSettings):
    """进程级配置。只允许在进程启动时构造一次。"""

    model_config = SettingsConfigDict(
        env_prefix="AUTUMN_",
        env_file=BACKEND_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------- 运行模式 --
    environment: Environment = Environment.LOCAL
    debug: bool = False

    # ------------------------------------------------------------- 数据库 ----
    database_url: SecretStr = SecretStr("postgresql+asyncpg://autumn:autumn@127.0.0.1:5432/autumn")
    db_echo: bool = False
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    db_pool_timeout: float = Field(default=30.0, gt=0)
    db_pool_recycle: int = Field(default=1800, ge=-1)

    # ----------------------------------------------------------- 会话与安全 --
    session_secret: SecretStr = SecretStr(_DEV_SESSION_SECRET)
    csrf_secret: SecretStr = SecretStr(_DEV_CSRF_SECRET)
    session_ttl_hours: int = Field(default=720, ge=1)
    cookie_name: str = "autumn_session"
    cookie_secure: bool = False
    cookie_domain: str | None = None

    # ------------------------------------------------------------- 时间语义 --
    quota_timezone: str = "Asia/Shanghai"

    # ------------------------------------------------------------- 可观测性 --
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_json: bool = False
    metrics_enabled: bool = False
    tracing_enabled: bool = False

    # ------------------------------------------------------------- 外部依赖 --
    # A1 只登记配置，适配器实现留给 providers / storage 阶段。
    llm_base_url: str | None = None
    llm_api_key: SecretStr | None = None
    search_api_key: SecretStr | None = None
    embedding_base_url: str | None = None
    embedding_api_key: SecretStr | None = None
    storage_root: Path = BACKEND_ROOT / "var" / "storage"

    # -------------------------------------------------------------- 校验 ----
    @field_validator("session_secret", "csrf_secret", mode="before")
    @classmethod
    def _reject_blank_secret(cls, value: Any) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError("安全密钥不得为空；请通过环境变量提供")
        return value

    @field_validator("cookie_name")
    @classmethod
    def _validate_cookie_name(cls, value: str) -> str:
        # Cookie 名只允许 RFC 6265 token 字符；`__Host-` 前缀由生产环境显式使用。
        if not re.fullmatch(r"[A-Za-z0-9!#$%&'*+\-.^_`|~]+", value):
            raise ValueError(f"非法 Cookie 名：{value!r}")
        return value

    @model_validator(mode="after")
    def _enforce_production_invariants(self) -> Settings:
        if self.environment is Environment.PROD:
            self._require_explicit_production_secrets()
            if not self.cookie_secure:
                raise ValueError("生产环境必须启用 Secure Cookie")
            if not self.cookie_name.startswith("__Host-"):
                raise ValueError("生产环境会话 Cookie 必须使用 __Host- 前缀")
        return self

    def _require_explicit_production_secrets(self) -> None:
        """生产环境必须**显式**提供密钥，且不得使用开发占位值。

        两条规则缺一不可：

        1. 字段必须出现在 ``model_fields_set`` 里——否则用的是代码内默认值。
           只比较长度会漏掉它，因为开发默认值本身就超过 32 字符。
        2. 值不得等于开发占位常量——显式传入占位值同样不可接受。

        错误信息只包含变量名，绝不回显密钥内容（可能被日志收走）。
        """
        env_names = {
            "session_secret": "AUTUMN_SESSION_SECRET",
            "csrf_secret": "AUTUMN_CSRF_SECRET",
        }
        for field_name in _REQUIRED_IN_PRODUCTION:
            env_name = env_names[field_name]
            provided = field_name in self.model_fields_set
            value = getattr(self, field_name).get_secret_value()

            if not provided:
                raise ValueError(f"生产环境必须显式提供 {env_name}；不接受代码内的开发默认值")
            if value in _PLACEHOLDER_SECRETS:
                raise ValueError(f"生产环境不得使用开发占位密钥：{env_name}")
            if len(value) < 32:
                raise ValueError(f"生产环境 {env_name} 至少需要 32 字符")

    # ------------------------------------------------------------ 便捷属性 --
    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PROD

    @property
    def is_test(self) -> bool:
        return self.environment is Environment.TEST

    @property
    def async_database_url(self) -> str:
        """SQLAlchemy 异步 URL（asyncpg 驱动）。"""
        url = self.database_url.get_secret_value()
        if url.startswith("postgresql://") or url.startswith("postgres://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1).replace(
                "postgres://", "postgresql+asyncpg://", 1
            )
        return url

    @property
    def sync_database_url(self) -> str:
        """同步 URL（psycopg）：只用于 ``alembic upgrade --sql`` 的离线方言。"""
        return self.async_database_url.replace("+asyncpg", "+psycopg")

    def validate_database_dsn(self) -> PostgresDsn:
        """启动自检：确认 DSN 可解析且指向 PostgreSQL。"""
        return PostgresDsn(self.async_database_url)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程级单例配置。测试中如需覆盖，先调用 ``get_settings.cache_clear()``。"""
    return Settings()

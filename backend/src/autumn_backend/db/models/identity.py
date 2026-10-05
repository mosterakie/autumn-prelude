"""第一批模型：身份与账号基础。

覆盖实施顺序 A3 的六张表：

- :class:`User` —— 账号、角色、邮箱规范化后的唯一性
- :class:`AuthSession` —— 浏览器会话，**数据库只存令牌哈希**
- :class:`AuthToken` —— 一次性令牌（验证邮箱 / 重置密码 / step-up / 站长引导）
- :class:`AdminFactor` —— 站长 TOTP 与恢复码，密钥**密文**存储
- :class:`RateLimitBucket` —— 速率窗口计数
- :class:`Setting` —— 全站配置，含 ``content_acl_epoch``

归属说明：模型放在 ``db/models`` 而不是 ``auth``，因为 ORM 模型属于数据库契约层；
``auth`` 模块负责密码、会话、CSRF、TOTP 的**行为**，由它导入这些模型，而不是反过来。

CHECK 约束的 ``name=`` 一律只写**标签**（如 ``"role_valid"``），
完整名字 ``ck_<table>_<label>`` 由 ``Base.metadata`` 的命名约定补齐。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from autumn_backend.db.base import Base
from autumn_backend.db.enums import (
    AdminFactorType,
    AuthTokenPurpose,
    Role,
    SettingValueType,
    enum_check_expression,
    enum_column_type,
)
from autumn_backend.db.mixins import Timestamped, UUIDPrimaryKey, Versioned

# 令牌与验证码哈希统一使用 SHA-256 十六进制（64 字符）。
_HASH_LENGTH = 64
# Argon2id 编码串长度随参数变化，留足空间。
_PASSWORD_HASH_LENGTH = 255
_EMAIL_LENGTH = 320  # RFC 5321 上限
_TOTP_SECRET_LENGTH = 512  # 应用层对称加密后的密文长度上限


class User(UUIDPrimaryKey, Timestamped, Versioned, Base):
    """账号。

    设计要点：

    - ``email_canonical`` 承载唯一性（规范化后），``email`` 保留用户原始书写形式；
      规范化规则由 auth service 在写入前应用，数据库用 CHECK 兜住"必须是规范化形式"。
    - 密码只存 Argon2id 编码串；明文永不落库。
    - ``session_version`` 是会话失效闸门：禁用账号、重置密码、强制登出时递增。
    - ``cooldown_until`` 与 ``daily_quota_override`` 支撑"验证邮箱后冷却 24 小时、
      每日 10 次 AI 请求，参数可调"；数值由 service 决定，本表只存结果。
    - **不**设置自动过期时间：文章、收藏、聊天与日志默认永久保留。
    """

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(_EMAIL_LENGTH), nullable=False)
    email_canonical: Mapped[str] = mapped_column(String(_EMAIL_LENGTH), nullable=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    password_hash: Mapped[str] = mapped_column(String(_PASSWORD_HASH_LENGTH), nullable=False)
    password_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    role: Mapped[Role] = mapped_column(
        enum_column_type(Role, length=16),
        nullable=False,
        default=Role.MEMBER,
    )

    display_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locale: Mapped[str] = mapped_column(String(16), nullable=False, default="zh-CN")

    # 会话失效闸门：``auth_sessions.session_version`` 必须等于该值才仍然有效。
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # ``messages.content_version`` 的兼容版本，用于消息内容结构演进。
    content_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # 账号状态与治理（v1 验收：账号禁用可阻断待执行工具与后续流式输出）。
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    daily_quota_override: Mapped[int | None] = mapped_column(Integer, nullable=True)

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    sessions: Mapped[list[AuthSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="raise"
    )
    tokens: Mapped[list[AuthToken]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="raise"
    )
    admin_factors: Mapped[list[AdminFactor]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="raise"
    )

    __table_args__ = (
        # 邮箱规范化后唯一：唯一性建立在规范化列上，而不是用户书写形式。
        UniqueConstraint("email_canonical", name="uq_users_email_canonical"),
        # 数据库兜底：落库的规范化邮箱必须已经小写且无首尾空白。
        CheckConstraint(
            "email_canonical = lower(btrim(email_canonical))",
            name="email_canonical_normalized",
        ),
        CheckConstraint(enum_check_expression("role", Role), name="role_valid"),
        CheckConstraint("session_version >= 1", name="session_version_positive"),
        CheckConstraint("content_schema_version >= 1", name="content_schema_version_positive"),
        # 覆盖额度为 0 是有意义的（临时停用 AI），但不允许负数。
        CheckConstraint(
            "daily_quota_override IS NULL OR daily_quota_override >= 0",
            name="daily_quota_override_non_negative",
        ),
        # 站长账号不可通过公开注册产生：引导流程必须显式写入 owner 角色，
        # 因此按 role 建索引，便于运维核对"是否存在多个站长"。
        Index("ix_users_role_created_at", "role", "created_at"),
        Index("ix_users_cooldown_until", "cooldown_until"),
    )


class AuthSession(UUIDPrimaryKey, Timestamped, Versioned, Base):
    """浏览器会话。

    设计要点：

    - **数据库只存令牌哈希**（``token_hash`` 唯一）；明文令牌只在 Set-Cookie 时出现一次。
    - ``csrf_version`` 与 ``session_version`` 支撑"登录 / step-up / 重置成功后轮换会话
      并递增 csrf_version"；轮换保留旧行并在 ``rotated_at`` 标记，便于审计追溯。
    - ``revoked_at`` 让撤销即时生效（v1 验收：账号禁用或 step-up 过期可阻断后续输出）。
    """

    __tablename__ = "auth_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    token_hash: Mapped[str] = mapped_column(String(_HASH_LENGTH), nullable=False)
    # 必须与 users.session_version 一致才有效：账号级失效闸门。
    session_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 每次关键认证成功后递增；CSRF 令牌由服务端密钥绑定 (session_id, csrf_version)。
    csrf_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    created_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)

    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 轮换链：本条会话被哪条新会话取代；为 NULL 表示仍是当前会话。
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replaced_by_session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("auth_sessions.id", ondelete="SET NULL"),
        nullable=True,
    )

    # step-up 升级验证到期时刻；过期即视为权限降回基础能力。
    step_up_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 已使用的 TOTP 时间步，防止同一验证码重放。
    last_totp_step: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions", lazy="raise")

    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash"),
        CheckConstraint("session_version >= 1", name="session_version_positive"),
        CheckConstraint("csrf_version >= 1", name="csrf_version_positive"),
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
        # 已被取代的会话必须同时有 rotated_at，避免"半截轮换"状态。
        CheckConstraint(
            "(replaced_by_session_id IS NULL) = (rotated_at IS NULL)",
            name="rotation_marker_consistent",
        ),
        Index("ix_auth_sessions_user_id_revoked_at", "user_id", "revoked_at"),
        Index("ix_auth_sessions_expires_at", "expires_at"),
    )


class AuthToken(UUIDPrimaryKey, Timestamped, Versioned, Base):
    """一次性令牌。

    设计要点：

    - **只存哈希**，明文只在邮件/响应中出现一次；``token_hash`` 唯一。
    - 幂等身份 ``(purpose, user_id, client_id)``：同一客户端重复提交返回原令牌，
      而不是生成第二条；语义判等用 ``request_hash``（稳定 JSON 的 SHA-256）。
      无浏览器场景 ``client_id`` 为 NULL，NULL 之间不参与唯一性，正是所需语义。
    - ``consumed_at`` 一次性消费；``failed_attempts`` 支持错误次数上限。
    - 站长 bootstrap 令牌同样走这张表（``purpose='admin_bootstrap'``）。
    """

    __tablename__ = "auth_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    purpose: Mapped[AuthTokenPurpose] = mapped_column(
        enum_column_type(AuthTokenPurpose, length=32), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(_HASH_LENGTH), nullable=False)
    # 发起方可提供的幂等标识（由浏览器端生成）。
    client_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 同一幂等身份的语义判等键：稳定 JSON 序列化后的 SHA-256。
    request_hash: Mapped[str] = mapped_column(String(_HASH_LENGTH), nullable=False)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)

    user: Mapped[User] = relationship(back_populates="tokens", lazy="raise")

    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_auth_tokens_token_hash"),
        CheckConstraint(enum_check_expression("purpose", AuthTokenPurpose), name="purpose_valid"),
        CheckConstraint("failed_attempts >= 0", name="failed_attempts_non_negative"),
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
        UniqueConstraint(
            "purpose", "user_id", "client_id", name="uq_auth_tokens_purpose_user_client"
        ),
        Index("ix_auth_tokens_user_id_purpose", "user_id", "purpose"),
        Index("ix_auth_tokens_expires_at", "expires_at"),
    )


class AdminFactor(UUIDPrimaryKey, Timestamped, Versioned, Base):
    """站长额外验证因子。

    设计要点：

    - TOTP 密钥与恢复码**只存密文/哈希**，任何明文都不落库、不进日志。
    - 恢复码以「盐 + 哈希」列表存放（JSONB），逐条单次使用。
    - 同一用户的同一类型因子只允许一条仍然有效的记录（Partial Unique Index）。
    """

    __tablename__ = "admin_factors"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    factor_type: Mapped[AdminFactorType] = mapped_column(
        enum_column_type(AdminFactorType, length=32), nullable=False
    )
    # TOTP 共享密钥的密文（应用层对称加密）；列名刻意不含 secret 明文语义。
    secret_ciphertext: Mapped[str] = mapped_column(String(_TOTP_SECRET_LENGTH), nullable=False)
    # 恢复码：{"salt": "...", "codes": ["<hash>", ...]}；单个码消费后从列表移除。
    recovery_codes: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="admin_factors", lazy="raise")

    __table_args__ = (
        CheckConstraint(
            enum_check_expression("factor_type", AdminFactorType), name="factor_type_valid"
        ),
        CheckConstraint("length(secret_ciphertext) > 0", name="secret_ciphertext_not_empty"),
        # 这是 Partial Unique Index（可见性谓词），不是 CHECK——CHECK 不能跨行。
        Index(
            "ix_admin_factors_user_type_active",
            "user_id",
            "factor_type",
            unique=True,
            postgresql_where=text("is_active AND disabled_at IS NULL"),
        ),
    )


class RateLimitBucket(UUIDPrimaryKey, Timestamped, Base):
    """速率窗口计数。

    与 A6 的 ``quota_buckets`` 分工不同：本表管**速率**（单位时间的请求次数上限，
    例如登录尝试、邮件发送、AI 速率窗口），``quota_buckets`` 管**额度**
    （每日 10 次 AI 请求这类可预留、可结算的配额）。

    并发纪律：首次创建必须 ``INSERT ... ON CONFLICT (user_id, window_start) DO NOTHING``
    再 ``SELECT ... FOR UPDATE``——不能指望 ``FOR UPDATE`` 锁住不存在的行。
    """

    __tablename__ = "rate_limit_buckets"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("user_id", "window_start", name="uq_rate_limit_buckets_user_window"),
        CheckConstraint("count >= 0", name="count_non_negative"),
        CheckConstraint("window_seconds > 0", name="window_seconds_positive"),
    )


class Setting(Versioned, Timestamped, Base):
    """全站配置（键值表）。

    设计要点：

    - **str 主键**，因此不继承 ``UUIDPrimaryKey``，也不能套用 ``UUIDRepository``；
      访问方法必须自己实现（阶段 B2）。
    - ``content_acl_epoch`` 就存在这里：任何影响公开可见范围的事务都要递增它
      （架构文档 §5）。原子递增形如
      ``UPDATE settings SET value = to_jsonb((value #>> '{}')::bigint + 1) ...``，
      见阶段 B/E 的实现。
    - ``is_sensitive=true`` 的项由 observability 层保证不进入日志与公开响应。
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    value_type: Mapped[SettingValueType] = mapped_column(
        enum_column_type(SettingValueType, length=16), nullable=False
    )
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            enum_check_expression("value_type", SettingValueType), name="value_type_valid"
        ),
        CheckConstraint("length(key) > 0", name="key_not_empty"),
        # 键统一小写点分命名（如 content_acl_epoch），避免出现大小写不同的同义键。
        CheckConstraint("key = lower(key)", name="key_lowercase"),
    )


__all__ = [
    "AdminFactor",
    "AuthSession",
    "AuthToken",
    "RateLimitBucket",
    "Setting",
    "User",
]

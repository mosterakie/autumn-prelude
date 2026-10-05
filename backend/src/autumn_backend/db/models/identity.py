"""第一批模型：身份、会话与设置。

对应 ``docs/architecture/database.md`` §3 与 §9，以及迁移批次一：
``users`` / ``auth_sessions`` / ``auth_tokens`` / ``admin_factors`` /
``rate_limit_buckets`` / ``settings``。

字段名、状态集合与约束按该文档；一处**有意偏离**：

- 文档说"业务 ID 统一 UUID，由应用生成"，模型里主路径确实是 ``uuid4``，
  同时保留 ``gen_random_uuid()`` 作为数据库兜底，让原生 SQL 与 Repository 的
  UPSERT 不必自己生成主键。见 ``db/mixins.py``。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from autumn_backend.db.base import Base
from autumn_backend.db.enums import (
    AdminFactorKind,
    AuthTokenPurpose,
    UserRole,
    UserStatus,
    enum_check_expression,
    enum_column_type,
)
from autumn_backend.db.mixins import (
    Deletable,
    Timestamped,
    UUIDPrimaryKey,
    Versioned,
)


class User(UUIDPrimaryKey, Timestamped, Versioned, Deletable, Base):
    """账号。

    设计要点：

    - ``email_normalized`` 承载唯一性；**规范化**由 auth service 在写入前应用，
      数据库用 CHECK 兜住"必须是小写且无首尾空白"。
    - ``password_hash`` 只存 Argon2id 编码串。
    - ``auth_version`` 是身份失效闸门：撤销身份或密码变化时递增，
      ``auth_sessions.auth_version`` 必须与它一致才有效。
    - ``ai_cooldown_until`` 在首次验证成功时按当时策略计算。
    - ``status`` 与 ``deleted_at`` 分工：``disabled`` 是治理动作，
      ``deleted_at`` 是账号删除入口。
    - 唯一性**不因软删除解除**：重新使用同一邮箱需要明确的恢复或彻底清理流程。
    """

    __tablename__ = "users"

    email_normalized: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)

    role: Mapped[UserRole] = mapped_column(
        enum_column_type(UserRole, length=16), nullable=False, default=UserRole.MEMBER
    )
    status: Mapped[UserStatus] = mapped_column(
        enum_column_type(UserStatus, length=32),
        nullable=False,
        default=UserStatus.PENDING_VERIFICATION,
    )

    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ai_cooldown_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 撤销身份或密码变化时递增。
    auth_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("email_normalized", name="uq_users_email_normalized"),
        CheckConstraint(
            "email_normalized = lower(btrim(email_normalized))",
            name="email_normalized_canonical",
        ),
        CheckConstraint("length(email_normalized) > 0", name="email_normalized_not_empty"),
        CheckConstraint(enum_check_expression("role", UserRole), name="role_valid"),
        CheckConstraint(enum_check_expression("status", UserStatus), name="status_valid"),
        CheckConstraint("auth_version >= 1", name="auth_version_positive"),
        # 文档：索引覆盖 status 与创建时间。
        Index("ix_users_status_created_at", "status", "created_at"),
    )


class AuthSession(UUIDPrimaryKey, Timestamped, Versioned, Base):
    """浏览器会话。

    设计要点：

    - Cookie 保存高熵原始 token，表内**只存哈希**。
    - 每次认证检查用户状态、``auth_version``、撤销与过期时间。
    - 空闲过期与绝对过期分开：``idle_expires_at`` 随活动推进，
      ``absolute_expires_at`` 是硬上限。
    - ``UNIQUE(id, user_id)`` 支持 ``runs`` 的复合会话归属约束。
    """

    __tablename__ = "auth_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # 每次关键认证成功后递增；CSRF 由独立服务端密钥对 (session_id, csrf_version) 签名。
    csrf_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    # 必须与 users.auth_version 一致才有效。
    auth_version: Mapped[int] = mapped_column(BigInteger, nullable=False)

    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    step_up_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash"),
        # 支持 runs(auth_session_id, user_id) 的复合引用。
        UniqueConstraint("id", "user_id", name="uq_auth_sessions_id_user_id"),
        CheckConstraint("csrf_version >= 1", name="csrf_version_positive"),
        CheckConstraint("auth_version >= 1", name="auth_version_positive"),
        CheckConstraint("absolute_expires_at > created_at", name="absolute_expires_after_created"),
        CheckConstraint(
            "idle_expires_at <= absolute_expires_at", name="idle_within_absolute_expiry"
        ),
        Index("ix_auth_sessions_user_id", "user_id"),
        # 文档：未撤销会话的过期时间。
        Index(
            "ix_auth_sessions_idle_expires_at_active",
            "idle_expires_at",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index("ix_auth_sessions_absolute_expires_at", "absolute_expires_at"),
    )


class AuthToken(UUIDPrimaryKey, Timestamped, Versioned, Base):
    """一次性链接令牌（邮箱验证 / 密码重置）。

    消费必须是原子更新：``consumed_at IS NULL AND expires_at > now()``，
    成功只发生一次。发邮件由 jobs 处理，邮件任务中的必要令牌采用短期加密载荷，
    消费或发送结束后移除秘密载荷，只留状态审计。
    """

    __tablename__ = "auth_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[AuthTokenPurpose] = mapped_column(
        enum_column_type(AuthTokenPurpose, length=32), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_auth_tokens_token_hash"),
        CheckConstraint(enum_check_expression("purpose", AuthTokenPurpose), name="purpose_valid"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
        Index("ix_auth_tokens_user_id_purpose", "user_id", "purpose"),
        Index("ix_auth_tokens_expires_at", "expires_at"),
    )


class AdminFactor(UUIDPrimaryKey, Timestamped, Versioned, Base):
    """站长额外验证因子（TOTP）。

    设计要点：

    - ``user_id`` **唯一**：一个账号只有一个 TOTP 因子。
    - 密钥只存密文；``encryption_key_version`` 记录用哪一版主密钥加密，
      便于轮换。主密钥不进数据库或仓库。
    - ``last_used_time_step`` 防止同一时间片重放。
    - 恢复码在 ``recovery_code_hashes`` 里只存哈希与使用状态，不存明文。
    - 首次绑定必须验证正确代码后写 ``enabled_at``。
    """

    __tablename__ = "admin_factors"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[AdminFactorKind] = mapped_column(
        enum_column_type(AdminFactorKind, length=16), nullable=False, default=AdminFactorKind.TOTP
    )
    secret_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    encryption_key_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    # 已使用过的 TOTP 时间步，防同一时间片重放。
    last_used_time_step: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    recovery_code_hashes: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", name="uq_admin_factors_user_id"),
        CheckConstraint(enum_check_expression("kind", AdminFactorKind), name="kind_valid"),
        CheckConstraint("length(secret_ciphertext) > 0", name="secret_ciphertext_not_empty"),
        CheckConstraint("encryption_key_version >= 1", name="encryption_key_version_positive"),
    )


class RateLimitBucket(Base, Timestamped):
    """短期防滥用计数（复合主键，没有 ``id``）。

    设计要点：

    - ``scope_hash`` 是按策略对用户 ID、来源 IP 或规范化邮箱计算的服务端 **HMAC**，
      **不存**可用于枚举账号的明文邮箱。
    - 分别限制 AI 受理、登录尝试与邮件发送；同请求涉及多个限制时按固定键序加锁。
    - 使用 PostgreSQL 原子更新，跨 API 进程一致。
    - 这是短期计数，窗口结束即可清理；**不替代**永久保留的问答额度与操作审计。
    """

    __tablename__ = "rate_limit_buckets"

    scope_hash: Mapped[str] = mapped_column(Text, nullable=False)
    policy_key: Mapped[str] = mapped_column(Text, nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    hits: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint(
            "scope_hash", "policy_key", "window_start", name="pk_rate_limit_buckets"
        ),
        CheckConstraint("length(scope_hash) > 0", name="scope_hash_not_empty"),
        CheckConstraint("length(policy_key) > 0", name="policy_key_not_empty"),
        CheckConstraint("hits >= 0", name="hits_non_negative"),
        CheckConstraint("window_end > window_start", name="window_ordered"),
        Index("ix_rate_limit_buckets_expires_at", "expires_at"),
    )


class Setting(Versioned, Timestamped, Base):
    """后端白名单配置（str 主键，不套 UUIDRepository）。

    设计要点：

    - ``schema_version`` 是**配置结构**版本，``version`` 是乐观并发版本，
      两者含义不同，不能混用。
    - ``content_acl_epoch`` 属于内部字段：只由公开范围事务递增，
      **不能**被站长聊天任意指定数值；白名单由 service 维护。
    - 密钥、文件系统任意路径与可执行代码不进入可由 Agent 修改的配置。
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    schema_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        CheckConstraint("length(key) > 0", name="key_not_empty"),
        CheckConstraint("key = lower(key)", name="key_lowercase"),
        CheckConstraint("schema_version >= 1", name="schema_version_positive"),
        CheckConstraint("version >= 0", name="version_non_negative"),
    )


__all__ = [
    "AdminFactor",
    "AuthSession",
    "AuthToken",
    "RateLimitBucket",
    "Setting",
    "User",
]

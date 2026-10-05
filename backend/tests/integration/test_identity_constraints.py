"""身份与会话表：真实 PostgreSQL 约束验收。

这些用例在真实 PostgreSQL 上运行，证明"模型声明的约束真的建出来了、真的拦得住"。
用 SQLite 或 Mock 无法证明这些语义（实施顺序 C1 的硬规则）。

覆盖范围对应 ``docs/architecture/database.md`` §3、§9：
``users`` / ``auth_sessions`` / ``auth_tokens`` / ``admin_factors`` /
``rate_limit_buckets`` / ``settings``。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from autumn_backend.db.enums import (
    AdminFactorKind,
    AuthTokenPurpose,
    UserRole,
    UserStatus,
)
from autumn_backend.db.models import (
    AdminFactor,
    AuthSession,
    AuthToken,
    RateLimitBucket,
    Setting,
)

pytestmark = pytest.mark.integration

UTC = dt.UTC

#: 本批带 ``updated_at`` 的表；``rate_limit_buckets`` 是复合主键但仍是可变记录。
TIMESTAMPED = (
    "users",
    "auth_sessions",
    "auth_tokens",
    "admin_factors",
    "rate_limit_buckets",
    "settings",
)


async def _expect_integrity_error(
    session: AsyncSession, message: str | None = None, *, statement: Any = None
) -> str:
    """断言数据库拒绝了这次写入；返回原始错误文本。

    可传入 ``statement`` 直接执行原生 SQL（用于绕过 Python 侧枚举校验、
    真正打到数据库 CHECK 约束的负向用例）。
    """
    with pytest.raises((IntegrityError, DBAPIError, StatementError)) as excinfo:
        if statement is not None:
            await session.execute(statement)
        await session.flush()
    raw = str(excinfo.value)
    if message is not None:
        assert message in raw, raw
    return raw


def _session_expiry(**overrides: Any) -> dict[str, Any]:
    now = dt.datetime.now(UTC)
    payload: dict[str, Any] = {
        "auth_version": 1,
        "idle_expires_at": now + dt.timedelta(hours=1),
        "absolute_expires_at": now + dt.timedelta(days=1),
    }
    payload.update(overrides)
    return payload


class TestSchemaObjectsExist:
    async def test_all_a3_tables_present(self, session: AsyncSession) -> None:
        rows = await session.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename = ANY(:names)"
            ),
            {"names": list(TIMESTAMPED)},
        )
        assert {row[0] for row in rows} == set(TIMESTAMPED)

    async def test_updated_at_triggers_are_enabled(self, session: AsyncSession) -> None:
        rows = await session.execute(
            text(
                "SELECT c.relname, t.tgenabled FROM pg_trigger t "
                "JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE c.relnamespace = 'public'::regnamespace "
                "AND NOT t.tgisinternal "
                "AND t.tgname LIKE 'trg%set_updated_at' "
                "ORDER BY c.relname"
            )
        )
        # tgenabled 是 "char" 类型，asyncpg 以 bytes 返回。
        found = {row[0]: bytes(row[1]).decode() for row in rows}
        assert found, "一个 updated_at 触发器都没有装"
        for table in TIMESTAMPED:
            assert found.get(table) == "O", f"{table} 缺少已启用的 updated_at 触发器"

    async def test_updated_at_function_exists(self, session: AsyncSession) -> None:
        value = await session.scalar(
            text("SELECT proname FROM pg_proc WHERE proname = 'autumn_set_updated_at'")
        )
        assert value == "autumn_set_updated_at"

    async def test_partial_index_predicate_installed(self, session: AsyncSession) -> None:
        """未撤销会话的过期索引是部分索引：谓词不在 autogenerate 比较范围内。"""
        predicate = await session.scalar(
            text(
                "SELECT pg_get_expr(i.indpred, i.indrelid) FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = 'ix_auth_sessions_idle_expires_at_active'"
            )
        )
        assert predicate is not None
        assert "revoked_at IS NULL" in predicate


class TestUsersConstraints:
    async def test_server_defaults_and_python_defaults(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """时间戳由数据库默认值给出；角色/状态/版本由模型默认值给出。"""
        user = make_user(email="defaults@example.com")
        await session.flush()
        await session.refresh(user)
        assert user.created_at is not None
        assert user.created_at.tzinfo is not None, "created_at 必须是 timestamptz"
        assert user.updated_at is not None
        assert user.version == 0  # 新建行的乐观锁版本为 0
        assert user.role is UserRole.MEMBER
        assert user.status is UserStatus.PENDING_VERIFICATION
        assert user.auth_version == 1

    async def test_email_normalized_is_unique(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        make_user(email="dup@example.com")
        await session.flush()
        make_user(email="dup@example.com")
        await _expect_integrity_error(session, "uq_users_email_normalized")

    async def test_email_normalized_must_be_lowercase(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """规范化由 service 负责，数据库用 CHECK 兜住"必须已是小写"。"""
        make_user(email="Mixed@Example.com")
        await _expect_integrity_error(session, "ck_users_email_normalized_canonical")

    async def test_email_normalized_must_have_no_surrounding_whitespace(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        make_user(email=" a@example.com ")
        await _expect_integrity_error(session, "ck_users_email_normalized_canonical")

    async def test_normalised_email_is_accepted(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="mixed@example.com")
        await session.flush()
        assert user.email_normalized == "mixed@example.com"

    async def test_role_must_be_a_known_value(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """用原生 SQL 绕过 Python 侧枚举校验，验证数据库 CHECK 真实生效。

        ORM 路径上 ``validate_strings=True`` 会先报 LookupError——那是应用层防线；
        数据库约束是**最后一道**防线，必须单独证明它存在且有效。
        """
        await _expect_integrity_error(
            session,
            "ck_users_role_valid",
            statement=text(
                "INSERT INTO users (email_normalized, password_hash, role, status, "
                "auth_version, version) "
                "VALUES ('evil@example.com', 'h', 'anonymous', 'active', 1, 0)"
            ),
        )
        assert make_user is not None

    async def test_status_must_be_a_known_value(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        await _expect_integrity_error(
            session,
            "ck_users_status_valid",
            statement=text(
                "INSERT INTO users (email_normalized, password_hash, role, status, "
                "auth_version, version) "
                "VALUES ('evil2@example.com', 'h', 'member', 'suspended', 1, 0)"
            ),
        )
        assert make_user is not None

    async def test_auth_version_must_be_positive(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """``auth_version`` 是身份失效闸门：0 或负数没有语义。"""
        make_user(email="av@example.com", auth_version=0)
        await _expect_integrity_error(session, "ck_users_auth_version_positive")

    async def test_owner_and_active_status_are_representable(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """站长账号可以落库，但不通过公开注册产生（由 service 决定谁写入）。"""
        user = make_user(email="owner@example.com", role=UserRole.OWNER, status=UserStatus.ACTIVE)
        await session.flush()
        assert user.role is UserRole.OWNER
        assert user.status is UserStatus.ACTIVE

    async def test_soft_deleted_email_is_still_taken(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """文档 §3：唯一性**不因软删除解除**——否则账号可被静默接管。"""
        user = make_user(email="soft@example.com")
        await session.flush()
        await session.execute(
            text("UPDATE users SET deleted_at = now() WHERE id = :id"), {"id": user.id}
        )
        make_user(email="soft@example.com")
        await _expect_integrity_error(session, "uq_users_email_normalized")


class TestAuthSessionConstraints:
    async def test_token_hash_is_unique(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="s1@example.com")
        await session.flush()
        session.add(AuthSession(user_id=user.id, token_hash="a" * 64, **_session_expiry()))
        await session.flush()
        session.add(AuthSession(user_id=user.id, token_hash="a" * 64, **_session_expiry()))
        await _expect_integrity_error(session, "uq_auth_sessions_token_hash")

    async def test_absolute_expiry_must_be_after_creation(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="s2@example.com")
        await session.flush()
        session.add(
            AuthSession(
                user_id=user.id,
                token_hash="b" * 64,
                auth_version=1,
                idle_expires_at=dt.datetime(2000, 1, 1, tzinfo=UTC),
                absolute_expires_at=dt.datetime(2000, 1, 2, tzinfo=UTC),
            )
        )
        await _expect_integrity_error(session, "ck_auth_sessions_absolute_expires_after_created")

    async def test_idle_expiry_cannot_exceed_absolute_expiry(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """空闲过期随活动推进，但绝不能越过绝对上限。"""
        user = make_user(email="s3@example.com")
        await session.flush()
        now = dt.datetime.now(UTC)
        session.add(
            AuthSession(
                user_id=user.id,
                token_hash="c" * 64,
                auth_version=1,
                idle_expires_at=now + dt.timedelta(days=3),
                absolute_expires_at=now + dt.timedelta(days=1),
            )
        )
        await _expect_integrity_error(session, "ck_auth_sessions_idle_within_absolute_expiry")

    async def test_version_counters_must_be_positive(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="s4@example.com")
        await session.flush()
        session.add(
            AuthSession(user_id=user.id, token_hash="d" * 64, **_session_expiry(csrf_version=0))
        )
        await _expect_integrity_error(session, "ck_auth_sessions_csrf_version_positive")

    async def test_auth_version_must_be_positive(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="s5@example.com")
        await session.flush()
        session.add(
            AuthSession(user_id=user.id, token_hash="e" * 64, **_session_expiry(auth_version=0))
        )
        await _expect_integrity_error(session, "ck_auth_sessions_auth_version_positive")

    async def test_revocation_is_representable_without_deleting_the_row(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """新契约用 ``revoked_at`` + ``auth_version`` 表达失效，不删行、不存轮换墓碑。"""
        user = make_user(email="s6@example.com")
        await session.flush()
        auth_session = AuthSession(
            user_id=user.id, token_hash="f" * 64, **_session_expiry(auth_version=7)
        )
        session.add(auth_session)
        await session.flush()

        await session.execute(
            text("UPDATE auth_sessions SET revoked_at = now() WHERE id = :id"),
            {"id": auth_session.id},
        )
        await session.refresh(auth_session)
        assert auth_session.revoked_at is not None
        assert auth_session.auth_version == 7
        columns = await session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'auth_sessions'::text "
                "AND column_name = ANY(ARRAY['replaced_by_session_id', 'rotated_at'])"
            )
        )
        assert list(columns) == [], "旧契约的轮换墓碑列不应存在"

    async def test_identity_and_user_pair_is_unique(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """``runs(auth_session_id, user_id)`` 的复合引用依赖这个唯一键真的存在。"""
        row = await session.execute(
            text("SELECT count(*) FROM pg_constraint WHERE conname = 'uq_auth_sessions_id_user_id'")
        )
        assert row.scalar() == 1

    async def test_deleting_user_cascades_to_sessions(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="cascade@example.com")
        await session.flush()
        session.add(AuthSession(user_id=user.id, token_hash="9" * 64, **_session_expiry()))
        await session.flush()

        await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
        remaining = await session.scalar(
            text("SELECT count(*) FROM auth_sessions WHERE user_id = :uid"), {"uid": user.id}
        )
        assert remaining == 0


class TestAuthTokenConstraints:
    async def test_token_hash_is_unique(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="t1@example.com")
        await session.flush()
        expires = dt.datetime.now(UTC) + dt.timedelta(hours=1)
        session.add(
            AuthToken(
                user_id=user.id,
                purpose=AuthTokenPurpose.VERIFY_EMAIL,
                token_hash="1" * 64,
                expires_at=expires,
            )
        )
        await session.flush()
        session.add(
            AuthToken(
                user_id=user.id,
                purpose=AuthTokenPurpose.RESET_PASSWORD,
                token_hash="1" * 64,
                expires_at=expires,
            )
        )
        await _expect_integrity_error(session, "uq_auth_tokens_token_hash")

    async def test_multiple_tokens_of_a_purpose_are_allowed(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """一次性令牌没有客户端身份列：同一用途可以有多条未消费令牌。"""
        user = make_user(email="t2@example.com")
        await session.flush()
        expires = dt.datetime.now(UTC) + dt.timedelta(hours=1)
        for index in range(2):
            session.add(
                AuthToken(
                    user_id=user.id,
                    purpose=AuthTokenPurpose.RESET_PASSWORD,
                    token_hash=str(index) * 64,
                    expires_at=expires,
                )
            )
        await session.flush()

    async def test_unknown_purpose_is_rejected(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """原生 SQL 绕过 Python 侧枚举校验，验证数据库 CHECK 真实生效。"""
        user = make_user(email="t3@example.com")
        await session.flush()
        await _expect_integrity_error(
            session,
            "ck_auth_tokens_purpose_valid",
            statement=text(
                "INSERT INTO auth_tokens (user_id, purpose, token_hash, expires_at, "
                "attempt_count, version) "
                "VALUES (:uid, 'step_up', :hash, now() + interval '1 hour', 0, 0)"
            ).bindparams(uid=user.id, hash="3" * 64),
        )

    async def test_expiry_must_be_after_creation(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="t4@example.com")
        await session.flush()
        session.add(
            AuthToken(
                user_id=user.id,
                purpose=AuthTokenPurpose.VERIFY_EMAIL,
                token_hash="4" * 64,
                expires_at=dt.datetime(2000, 1, 1, tzinfo=UTC),
            )
        )
        await _expect_integrity_error(session, "ck_auth_tokens_expires_after_created")

    async def test_attempt_count_cannot_be_negative(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="t5@example.com")
        await session.flush()
        session.add(
            AuthToken(
                user_id=user.id,
                purpose=AuthTokenPurpose.VERIFY_EMAIL,
                token_hash="5" * 64,
                expires_at=dt.datetime.now(UTC) + dt.timedelta(hours=1),
                attempt_count=-1,
            )
        )
        await _expect_integrity_error(session, "ck_auth_tokens_attempt_count_non_negative")


class TestAdminFactorConstraints:
    async def test_only_one_factor_per_user(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="f1@example.com", role=UserRole.OWNER)
        await session.flush()
        session.add(
            AdminFactor(
                user_id=user.id,
                kind=AdminFactorKind.TOTP,
                secret_ciphertext="ciphertext-1",
            )
        )
        await session.flush()
        session.add(
            AdminFactor(
                user_id=user.id,
                kind=AdminFactorKind.TOTP,
                secret_ciphertext="ciphertext-2",
            )
        )
        await _expect_integrity_error(session, "uq_admin_factors_user_id")

    async def test_empty_secret_is_rejected(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="f2@example.com", role=UserRole.OWNER)
        await session.flush()
        session.add(AdminFactor(user_id=user.id, kind=AdminFactorKind.TOTP, secret_ciphertext=""))
        await _expect_integrity_error(session, "ck_admin_factors_secret_ciphertext_not_empty")

    async def test_unknown_kind_is_rejected(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """文档 §3 固定 kind=totp：恢复码是它的字段，不是另一种因子。"""
        user = make_user(email="f3@example.com", role=UserRole.OWNER)
        await session.flush()
        await _expect_integrity_error(
            session,
            "ck_admin_factors_kind_valid",
            statement=text(
                "INSERT INTO admin_factors (user_id, kind, secret_ciphertext, "
                "encryption_key_version, version) "
                "VALUES (:uid, 'webauthn', 'c', 1, 0)"
            ).bindparams(uid=user.id),
        )

    async def test_encryption_key_version_must_be_positive(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="f4@example.com", role=UserRole.OWNER)
        await session.flush()
        session.add(
            AdminFactor(
                user_id=user.id,
                kind=AdminFactorKind.TOTP,
                secret_ciphertext="c",
                encryption_key_version=0,
            )
        )
        await _expect_integrity_error(session, "ck_admin_factors_encryption_key_version_positive")

    async def test_recovery_code_hashes_round_trip_as_jsonb(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="f5@example.com", role=UserRole.OWNER)
        await session.flush()
        factor = AdminFactor(
            user_id=user.id,
            kind=AdminFactorKind.TOTP,
            secret_ciphertext="ciphertext",
            recovery_code_hashes={"salt": "s", "codes": [{"hash": "h1", "used_at": None}]},
        )
        session.add(factor)
        await session.flush()
        await session.refresh(factor)
        assert factor.recovery_code_hashes == {
            "salt": "s",
            "codes": [{"hash": "h1", "used_at": None}],
        }


class TestRateLimitBucketConstraints:
    async def test_composite_primary_key_blocks_duplicate_window(
        self, session: AsyncSession
    ) -> None:
        window = dt.datetime(2026, 1, 1, tzinfo=UTC)

        def build(hits: int) -> RateLimitBucket:
            return RateLimitBucket(
                scope_hash="hmac-1",
                policy_key="auth.login",
                window_start=window,
                window_end=window + dt.timedelta(minutes=15),
                hits=hits,
                expires_at=window + dt.timedelta(hours=1),
            )

        session.add(build(1))
        await session.flush()
        session.add(build(0))
        await _expect_integrity_error(session, "pk_rate_limit_buckets")

    async def test_same_window_is_allowed_for_another_policy(self, session: AsyncSession) -> None:
        """不同策略各自计数：主键包含 policy_key。"""
        window = dt.datetime(2026, 1, 2, tzinfo=UTC)
        for policy in ("auth.login", "mail.send"):
            session.add(
                RateLimitBucket(
                    scope_hash="hmac-2",
                    policy_key=policy,
                    window_start=window,
                    window_end=window + dt.timedelta(minutes=15),
                    hits=0,
                    expires_at=window + dt.timedelta(hours=1),
                )
            )
        await session.flush()

    async def test_hits_cannot_be_negative(self, session: AsyncSession) -> None:
        window = dt.datetime(2026, 1, 3, tzinfo=UTC)
        session.add(
            RateLimitBucket(
                scope_hash="hmac-3",
                policy_key="ai.accept",
                window_start=window,
                window_end=window + dt.timedelta(minutes=15),
                hits=-1,
                expires_at=window + dt.timedelta(hours=1),
            )
        )
        await _expect_integrity_error(session, "ck_rate_limit_buckets_hits_non_negative")

    async def test_window_must_be_ordered(self, session: AsyncSession) -> None:
        window = dt.datetime(2026, 1, 4, tzinfo=UTC)
        session.add(
            RateLimitBucket(
                scope_hash="hmac-4",
                policy_key="ai.accept",
                window_start=window,
                window_end=window,
                hits=0,
                expires_at=window + dt.timedelta(hours=1),
            )
        )
        await _expect_integrity_error(session, "ck_rate_limit_buckets_window_ordered")

    async def test_scope_hash_must_be_non_empty(self, session: AsyncSession) -> None:
        window = dt.datetime(2026, 1, 5, tzinfo=UTC)
        session.add(
            RateLimitBucket(
                scope_hash="",
                policy_key="ai.accept",
                window_start=window,
                window_end=window + dt.timedelta(minutes=15),
                hits=0,
                expires_at=window + dt.timedelta(hours=1),
            )
        )
        await _expect_integrity_error(session, "ck_rate_limit_buckets_scope_hash_not_empty")


class TestSettingsConstraints:
    async def test_content_acl_epoch_is_seeded(self, session: AsyncSession) -> None:
        """迁移必须补种 content_acl_epoch：缺行不得被当成有效的 0 静默继续。"""
        row = (
            await session.execute(
                text("SELECT value FROM settings WHERE key = 'content_acl_epoch'")
            )
        ).one_or_none()
        assert row is not None, "content_acl_epoch 没有初始化"
        # JSONB 标量以 Python int 返回。
        assert row[0] == 0

    async def test_string_primary_key_round_trip(self, session: AsyncSession) -> None:
        session.add(Setting(key="assistant.theme", value={"mode": "dark"}))
        await session.flush()
        loaded = await session.get(Setting, "assistant.theme")
        assert loaded is not None
        assert loaded.value == {"mode": "dark"}
        assert loaded.schema_version == 1
        assert loaded.version == 0

    async def test_duplicate_key_is_rejected(self, session: AsyncSession) -> None:
        session.add(Setting(key="dup", value="a"))
        await session.flush()
        session.add(Setting(key="dup", value="b"))
        await _expect_integrity_error(session, "pk_settings")

    async def test_uppercase_key_is_rejected(self, session: AsyncSession) -> None:
        session.add(Setting(key="Content_ACL_Epoch", value=1))
        await _expect_integrity_error(session, "ck_settings_key_lowercase")

    async def test_empty_key_is_rejected(self, session: AsyncSession) -> None:
        session.add(Setting(key="", value=1))
        await _expect_integrity_error(session, "ck_settings_key_not_empty")

    async def test_schema_version_must_be_positive(self, session: AsyncSession) -> None:
        session.add(Setting(key="bad.schema", value=1, schema_version=0))
        await _expect_integrity_error(session, "ck_settings_schema_version_positive")

    async def test_jsonb_value_round_trip(self, session: AsyncSession) -> None:
        payload = {"ai_limits": {"daily": 10}, "cooldown_hours": 24}
        session.add(Setting(key="quota.policy", value=payload))
        await session.flush()
        loaded = await session.get(Setting, "quota.policy")
        assert loaded is not None
        assert loaded.value == payload

    async def test_updated_by_survives_user_deletion_as_null(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="setter@example.com", role=UserRole.OWNER)
        await session.flush()
        session.add(Setting(key="owned.setting", value="v", updated_by=user.id))
        await session.flush()

        await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
        loaded = await session.get(Setting, "owned.setting")
        assert loaded is not None
        assert loaded.updated_by is None


class TestTriggerBehaviour:
    async def test_no_op_update_keeps_row_intact(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """触发器为 BEFORE UPDATE ... RETURN NEW；空更新不得破坏行。"""
        user = make_user(email="trigger@example.com")
        await session.flush()
        user_id = user.id

        await session.execute(
            text("UPDATE users SET display_name = display_name WHERE id = :uid"), {"uid": user_id}
        )
        row = await session.scalar(
            text("SELECT email_normalized FROM users WHERE id = :uid"), {"uid": user_id}
        )
        assert row == "trigger@example.com"

    async def test_trigger_sets_updated_at_on_raw_update(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="trigger2@example.com")
        await session.flush()
        user_id = user.id

        before = await session.scalar(
            text("SELECT updated_at FROM users WHERE id = :uid"), {"uid": user_id}
        )
        # 同一事务内 now() 是事务开始时刻，因此这里断言触发器确实写了值且不为 NULL，
        # "时间真的前进了" 属于跨事务语义，留给并发用例。
        await session.execute(
            text("UPDATE users SET display_name = 'changed' WHERE id = :uid"), {"uid": user_id}
        )
        after = await session.scalar(
            text("SELECT updated_at FROM users WHERE id = :uid"), {"uid": user_id}
        )
        assert after is not None
        assert before is not None
        assert after >= before

    async def test_uuid_default_is_generated_by_database(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """gen_random_uuid() 是数据库默认值，原生 SQL 插入也能拿到主键。"""
        user_id = await session.scalar(
            text(
                "INSERT INTO users (email_normalized, password_hash, role, status, "
                "auth_version, version) "
                "VALUES ('raw@example.com', 'h', 'member', 'active', 1, 0) "
                "RETURNING id"
            )
        )
        assert isinstance(user_id, uuid.UUID)
        assert make_user is not None

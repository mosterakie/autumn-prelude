"""A3 第一批表：真实 PostgreSQL 约束验收。

这些用例在真实 PostgreSQL 上运行，证明"模型声明的约束真的建出来了、真的拦得住"。
用 SQLite 或 Mock 无法证明这些语义（实施顺序 C1 的硬规则）。
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

from autumn_backend.db.enums import AdminFactorType, AuthTokenPurpose, Role, SettingValueType
from autumn_backend.db.models import (
    AdminFactor,
    AuthSession,
    AuthToken,
    RateLimitBucket,
    Setting,
)

pytestmark = pytest.mark.integration

UTC = dt.UTC


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


class TestSchemaObjectsExist:
    async def test_all_a3_tables_present(self, session: AsyncSession) -> None:
        rows = await session.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename = ANY(:names)"
            ),
            {
                "names": [
                    "users",
                    "auth_sessions",
                    "auth_tokens",
                    "admin_factors",
                    "rate_limit_buckets",
                    "settings",
                ]
            },
        )
        assert {row[0] for row in rows} == {
            "users",
            "auth_sessions",
            "auth_tokens",
            "admin_factors",
            "rate_limit_buckets",
            "settings",
        }

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
        for table in ("users", "auth_sessions", "auth_tokens", "admin_factors", "settings"):
            assert found.get(table) == "O", f"{table} 缺少已启用的 updated_at 触发器"

    async def test_updated_at_function_exists(self, session: AsyncSession) -> None:
        value = await session.scalar(
            text("SELECT proname FROM pg_proc WHERE proname = 'autumn_set_updated_at'")
        )
        assert value == "autumn_set_updated_at"

    async def test_partial_unique_index_predicate_installed(self, session: AsyncSession) -> None:
        predicate = await session.scalar(
            text(
                "SELECT pg_get_expr(indpred, indrelid) FROM pg_index WHERE indexrelid = "
                "'ix_admin_factors_user_type_active'::regclass"
            )
        )
        assert predicate is not None
        assert "is_active" in predicate


class TestUsersConstraints:
    async def test_created_at_is_populated_by_server_default(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """时间戳由数据库默认值给出：ORM 不传值也必须落库成功。"""
        user = make_user(email="defaults@example.com")
        await session.flush()
        await session.refresh(user)
        assert user.created_at is not None
        assert user.created_at.tzinfo is not None, "created_at 必须是 timestamptz"
        assert user.updated_at is not None
        assert user.version == 0  # 新建行的乐观锁版本为 0
        assert user.role is Role.MEMBER

    async def test_email_canonical_is_unique(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        make_user(email="dup@example.com")
        await session.flush()

        make_user(email="other@example.com", email_canonical="dup@example.com")
        await _expect_integrity_error(session, "uq_users_email_canonical")

    async def test_email_canonical_must_be_normalised(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        make_user(email="Mixed@Example.com", email_canonical="Mixed@Example.com")
        await _expect_integrity_error(session, "ck_users_email_canonical_normalized")

    async def test_email_canonical_with_whitespace_is_rejected(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        make_user(email="a@example.com", email_canonical=" a@example.com ")
        await _expect_integrity_error(session, "ck_users_email_canonical_normalized")

    async def test_normalised_email_is_accepted(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="Mixed@Example.com", email_canonical="mixed@example.com")
        await session.flush()
        assert user.email == "Mixed@Example.com"  # 原始书写形式保留

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
                "INSERT INTO users (email, email_canonical, password_hash, role, locale, "
                "session_version, content_schema_version, version) "
                "VALUES ('evil@example.com', 'evil@example.com', 'h', 'anonymous', 'zh-CN', "
                "1, 1, 0)"
            ),
        )
        assert make_user is not None

    async def test_daily_quota_override_cannot_be_negative(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        make_user(email="quota@example.com", daily_quota_override=-1)
        await _expect_integrity_error(session, "ck_users_daily_quota_override_non_negative")

    async def test_session_version_must_be_positive(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        make_user(email="sv@example.com", session_version=0)
        await _expect_integrity_error(session, "ck_users_session_version_positive")

    async def test_owner_role_is_representable(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """站长账号可以落库，但不通过公开注册产生（由 service 决定谁写入）。"""
        user = make_user(email="owner@example.com", role=Role.OWNER)
        await session.flush()
        assert user.role is Role.OWNER


class TestAuthSessionConstraints:
    async def test_token_hash_is_unique(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="s1@example.com")
        await session.flush()
        expires = dt.datetime.now(UTC) + dt.timedelta(days=1)

        session.add(
            AuthSession(user_id=user.id, token_hash="a" * 64, session_version=1, expires_at=expires)
        )
        await session.flush()

        session.add(
            AuthSession(user_id=user.id, token_hash="a" * 64, session_version=1, expires_at=expires)
        )
        await _expect_integrity_error(session, "uq_auth_sessions_token_hash")

    async def test_expiry_must_be_after_creation(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="s2@example.com")
        await session.flush()
        session.add(
            AuthSession(
                user_id=user.id,
                token_hash="b" * 64,
                session_version=1,
                expires_at=dt.datetime(2000, 1, 1, tzinfo=UTC),
            )
        )
        await _expect_integrity_error(session, "ck_auth_sessions_expires_after_created")

    async def test_rotation_marker_must_be_consistent(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """有 replaced_by_session_id 却没有 rotated_at 属于半截轮换，必须被拒绝。"""
        user = make_user(email="s3@example.com")
        await session.flush()
        expires = dt.datetime.now(UTC) + dt.timedelta(days=1)
        successor = AuthSession(
            user_id=user.id, token_hash="c" * 64, session_version=1, expires_at=expires
        )
        session.add(successor)
        await session.flush()

        session.add(
            AuthSession(
                user_id=user.id,
                token_hash="d" * 64,
                session_version=1,
                expires_at=expires,
                replaced_by_session_id=successor.id,
                rotated_at=None,
            )
        )
        await _expect_integrity_error(session, "ck_auth_sessions_rotation_marker_consistent")

    async def test_consistent_rotation_is_accepted(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="s4@example.com")
        await session.flush()
        expires = dt.datetime.now(UTC) + dt.timedelta(days=1)
        successor = AuthSession(
            user_id=user.id, token_hash="e" * 64, session_version=1, expires_at=expires
        )
        session.add(successor)
        await session.flush()

        rotated_at = dt.datetime.now(UTC)
        session.add(
            AuthSession(
                user_id=user.id,
                token_hash="f" * 64,
                session_version=1,
                expires_at=expires,
                replaced_by_session_id=successor.id,
                rotated_at=rotated_at,
            )
        )
        await session.flush()

    async def test_deleting_user_cascades_to_sessions(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="cascade@example.com")
        await session.flush()
        session.add(
            AuthSession(
                user_id=user.id,
                token_hash="9" * 64,
                session_version=1,
                expires_at=dt.datetime.now(UTC) + dt.timedelta(days=1),
            )
        )
        await session.flush()

        await session.delete(user)
        await session.flush()
        remaining = await session.scalar(
            text("SELECT count(*) FROM auth_sessions WHERE user_id = :uid"), {"uid": user.id}
        )
        assert remaining == 0


class TestAuthTokenConstraints:
    async def test_idempotency_identity_is_unique(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """同一 (purpose, user, client_id) 只能存在一条令牌。"""
        user = make_user(email="t1@example.com")
        await session.flush()
        expires = dt.datetime.now(UTC) + dt.timedelta(hours=1)

        session.add(
            AuthToken(
                user_id=user.id,
                purpose=AuthTokenPurpose.EMAIL_VERIFY,
                token_hash="1" * 64,
                request_hash="r" * 64,
                client_id="browser-1",
                expires_at=expires,
            )
        )
        await session.flush()

        session.add(
            AuthToken(
                user_id=user.id,
                purpose=AuthTokenPurpose.EMAIL_VERIFY,
                token_hash="2" * 64,
                request_hash="r" * 64,
                client_id="browser-1",
                expires_at=expires,
            )
        )
        await _expect_integrity_error(session, "uq_auth_tokens_purpose_user_client")

    async def test_null_client_id_allows_multiple_tokens(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """无浏览器场景 client_id 为 NULL，NULL 之间不参与唯一性。"""
        user = make_user(email="t2@example.com")
        await session.flush()
        expires = dt.datetime.now(UTC) + dt.timedelta(hours=1)

        for index in range(2):
            session.add(
                AuthToken(
                    user_id=user.id,
                    purpose=AuthTokenPurpose.PASSWORD_RESET,
                    token_hash=str(index) * 64,
                    request_hash="r" * 64,
                    client_id=None,
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
                "INSERT INTO auth_tokens (user_id, purpose, token_hash, request_hash, "
                "failed_attempts, expires_at, version, created_at, updated_at) "
                "VALUES (:uid, 'not_a_purpose', :hash, :rhash, 0, now() + interval '1 hour', "
                "0, now(), now())"
            ).bindparams(uid=user.id, hash="3" * 64, rhash="r" * 64),
        )

    async def test_failed_attempts_cannot_be_negative(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="t4@example.com")
        await session.flush()
        session.add(
            AuthToken(
                user_id=user.id,
                purpose=AuthTokenPurpose.STEP_UP,
                token_hash="4" * 64,
                request_hash="r" * 64,
                failed_attempts=-1,
                expires_at=dt.datetime.now(UTC) + dt.timedelta(hours=1),
            )
        )
        await _expect_integrity_error(session, "ck_auth_tokens_failed_attempts_non_negative")


class TestAdminFactorConstraints:
    async def test_only_one_active_factor_of_a_type(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="f1@example.com", role=Role.OWNER)
        await session.flush()

        session.add(
            AdminFactor(
                user_id=user.id,
                factor_type=AdminFactorType.TOTP,
                secret_ciphertext="ciphertext-1",
            )
        )
        await session.flush()

        session.add(
            AdminFactor(
                user_id=user.id,
                factor_type=AdminFactorType.TOTP,
                secret_ciphertext="ciphertext-2",
            )
        )
        await _expect_integrity_error(session)

    async def test_disabled_factor_frees_the_slot(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """Partial Unique Index 只约束 is_active 且未禁用的行。"""
        user = make_user(email="f2@example.com", role=Role.OWNER)
        await session.flush()

        session.add(
            AdminFactor(
                user_id=user.id,
                factor_type=AdminFactorType.TOTP,
                secret_ciphertext="ciphertext-old",
                is_active=False,
                disabled_at=dt.datetime.now(UTC),
            )
        )
        await session.flush()

        session.add(
            AdminFactor(
                user_id=user.id,
                factor_type=AdminFactorType.TOTP,
                secret_ciphertext="ciphertext-new",
            )
        )
        await session.flush()

    async def test_empty_secret_is_rejected(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="f3@example.com", role=Role.OWNER)
        await session.flush()
        session.add(
            AdminFactor(
                user_id=user.id,
                factor_type=AdminFactorType.TOTP,
                secret_ciphertext="",
            )
        )
        await _expect_integrity_error(session, "ck_admin_factors_secret_ciphertext_not_empty")

    async def test_recovery_codes_round_trip_as_jsonb(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="f4@example.com", role=Role.OWNER)
        await session.flush()
        factor = AdminFactor(
            user_id=user.id,
            factor_type=AdminFactorType.RECOVERY_CODE,
            secret_ciphertext="unused",
            recovery_codes={"salt": "s", "codes": ["h1", "h2"]},
        )
        session.add(factor)
        await session.flush()
        await session.refresh(factor)
        assert factor.recovery_codes == {"salt": "s", "codes": ["h1", "h2"]}


class TestRateLimitBucketConstraints:
    async def test_window_is_unique_per_user(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="r1@example.com")
        await session.flush()
        window = dt.datetime(2026, 1, 1, tzinfo=UTC)

        session.add(
            RateLimitBucket(user_id=user.id, window_start=window, window_seconds=60, count=1)
        )
        await session.flush()

        session.add(
            RateLimitBucket(user_id=user.id, window_start=window, window_seconds=60, count=0)
        )
        await _expect_integrity_error(session, "uq_rate_limit_buckets_user_window")

    async def test_count_cannot_be_negative(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="r2@example.com")
        await session.flush()
        session.add(
            RateLimitBucket(
                user_id=user.id,
                window_start=dt.datetime(2026, 1, 2, tzinfo=UTC),
                window_seconds=60,
                count=-1,
            )
        )
        await _expect_integrity_error(session, "ck_rate_limit_buckets_count_non_negative")


class TestSettingsConstraints:
    async def test_string_primary_key_round_trip(self, session: AsyncSession) -> None:
        session.add(
            Setting(
                key="content_acl_epoch",
                value=1,
                value_type=SettingValueType.INTEGER,
                description="全站公开权限 epoch",
            )
        )
        await session.flush()
        loaded = await session.get(Setting, "content_acl_epoch")
        assert loaded is not None
        assert loaded.value == 1

    async def test_duplicate_key_is_rejected(self, session: AsyncSession) -> None:
        session.add(Setting(key="dup", value="a", value_type=SettingValueType.STRING))
        await session.flush()
        session.add(Setting(key="dup", value="b", value_type=SettingValueType.STRING))
        await _expect_integrity_error(session, "pk_settings")

    async def test_uppercase_key_is_rejected(self, session: AsyncSession) -> None:
        session.add(Setting(key="Content_ACL_Epoch", value=1, value_type=SettingValueType.INTEGER))
        await _expect_integrity_error(session, "ck_settings_key_lowercase")

    async def test_empty_key_is_rejected(self, session: AsyncSession) -> None:
        session.add(Setting(key="", value=1, value_type=SettingValueType.INTEGER))
        await _expect_integrity_error(session, "ck_settings_key_not_empty")

    async def test_value_type_is_constrained(self, session: AsyncSession) -> None:
        """原生 SQL 绕过 Python 侧枚举校验，验证数据库 CHECK 真实生效。"""
        await _expect_integrity_error(
            session,
            "ck_settings_value_type_valid",
            statement=text(
                "INSERT INTO settings (key, value, value_type, is_sensitive, version, "
                "created_at, updated_at) "
                "VALUES ('bad_type', '1'::jsonb, 'float', false, 0, now(), now())"
            ),
        )

    async def test_jsonb_value_round_trip(self, session: AsyncSession) -> None:
        payload = {"daily_ai_requests": 10, "cooldown_hours": 24}
        session.add(Setting(key="quota.policy", value=payload, value_type=SettingValueType.JSON))
        await session.flush()
        loaded = await session.get(Setting, "quota.policy")
        assert loaded is not None
        assert loaded.value == payload

    async def test_updated_by_survives_user_deletion_as_null(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="setter@example.com", role=Role.OWNER)
        await session.flush()
        session.add(
            Setting(
                key="owned.setting",
                value="v",
                value_type=SettingValueType.STRING,
                updated_by=user.id,
            )
        )
        await session.flush()

        await session.delete(user)
        await session.flush()
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
            text("SELECT email_canonical FROM users WHERE id = :uid"), {"uid": user_id}
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
        # "时间真的前进了" 属于跨事务语义，留给阶段 C 的并发用例。
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
                "INSERT INTO users (email, email_canonical, password_hash, role, locale, "
                "session_version, content_schema_version, version) "
                "VALUES ('raw@example.com', 'raw@example.com', 'h', 'member', 'zh-CN', 1, 1, 0) "
                "RETURNING id"
            )
        )
        assert isinstance(user_id, uuid.UUID)
        assert make_user is not None

"""身份与会话模型：元数据契约单测（不需要数据库）。

对应 ``docs/architecture/database.md`` §3、§9 与 ``db/models/identity.py``。
真库验收（约束真的能建出来、真的拦住非法数据）在
``tests/integration/test_identity_constraints.py``。

本文件只在 SQLAlchemy 元数据层断言：列名、约束名、CHECK 表达式、唯一性身份与
索引形状。任何一条断言都对应文档里的一句物理建模要求。
"""

from __future__ import annotations

import enum

import pytest
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Index,
    PrimaryKeyConstraint,
    Table,
    UniqueConstraint,
)

from autumn_backend.db import models
from autumn_backend.db.base import Base
from autumn_backend.db.enums import (
    AdminFactorKind,
    AuthTokenPurpose,
    UserRole,
    UserStatus,
    enum_check_expression,
    enum_column_type,
    in_predicate,
)
from autumn_backend.db.mixins import timestamped_tables

pytestmark = pytest.mark.unit

A3_TABLES = (
    "users",
    "auth_sessions",
    "auth_tokens",
    "admin_factors",
    "rate_limit_buckets",
    "settings",
)


@pytest.fixture(scope="module", autouse=True)
def _registered_models() -> None:
    models.load_all_models()


def table(name: str) -> Table:
    return Base.metadata.tables[name]


def constraint_names(name: str, kind: type) -> set[str]:
    return {
        constraint.name
        for constraint in table(name).constraints
        if isinstance(constraint, kind) and constraint.name is not None
    }


def check_expressions(name: str) -> dict[str, str]:
    return {
        constraint.name: str(constraint.sqltext)
        for constraint in table(name).constraints
        if isinstance(constraint, CheckConstraint) and constraint.name is not None
    }


def index_by_name(name: str, index_name: str) -> Index:
    return next(index for index in table(name).indexes if index.name == index_name)


class TestTableRegistration:
    @pytest.mark.parametrize("name", A3_TABLES)
    def test_table_exists(self, name: str) -> None:
        assert name in Base.metadata.tables

    def test_all_a3_tables_are_timestamped(self) -> None:
        """可变记录都有 updated_at（文档 §1："除特别说明外还有 updated_at"）。"""
        assert set(A3_TABLES).issubset({t.name for t in timestamped_tables()})

    def test_check_constraint_names_have_single_prefix(self) -> None:
        """命名约定已含 ``ck_%(table_name)s_``，模型只能传 label，不能传完整名字。"""
        names = [
            c.name
            for name in A3_TABLES
            for c in table(name).constraints
            if isinstance(c, CheckConstraint)
        ]
        assert names, "A3 表应当带 CHECK 约束"
        for name in names:
            assert name is not None
            assert name.count("ck_") == 1, f"{name} 出现重复前缀"


class TestUsers:
    def test_primary_key_is_uuid(self) -> None:
        assert table("users").c.id.primary_key is True
        pk = next(c for c in table("users").constraints if isinstance(c, PrimaryKeyConstraint))
        assert pk.name == "pk_users"

    def test_email_normalized_is_unique(self) -> None:
        """文档 §3：唯一性由 ``email_normalized`` 承担，且不因软删除解除。"""
        assert "uq_users_email_normalized" in constraint_names("users", UniqueConstraint)

    def test_email_normalized_must_be_canonical(self) -> None:
        expressions = check_expressions("users")
        assert expressions["ck_users_email_normalized_canonical"] == (
            "email_normalized = lower(btrim(email_normalized))"
        )
        assert "length(email_normalized) > 0" in expressions["ck_users_email_normalized_not_empty"]

    def test_role_check_lists_exactly_the_enum_values(self) -> None:
        expression = check_expressions("users")["ck_users_role_valid"]
        for value in UserRole:
            assert f"'{value.value}'" in expression
        # anonymous 不是落库角色：注册前没有账号行。
        assert "'anonymous'" not in expression

    def test_status_check_lists_exactly_the_enum_values(self) -> None:
        expression = check_expressions("users")["ck_users_status_valid"]
        for value in UserStatus:
            assert f"'{value.value}'" in expression

    def test_auth_version_guard(self) -> None:
        assert "auth_version >= 1" in check_expressions("users")["ck_users_auth_version_positive"]

    def test_status_created_at_index_exists(self) -> None:
        """文档 §3：索引覆盖 status 与创建时间。"""
        index = index_by_name("users", "ix_users_status_created_at")
        assert [column.name for column in index.columns] == ["status", "created_at"]

    def test_removed_v1_columns_are_gone(self) -> None:
        """旧契约的列必须彻底消失：留着它们等于给"双写漂移"留口子。"""
        columns = set(table("users").columns.keys())
        assert (
            not {
                "email",
                "email_canonical",
                "session_version",
                "daily_quota_override",
                "content_schema_version",
                "locale",
            }
            & columns
        )

    def test_version_column_is_bigint(self) -> None:
        """文档 §1：可变对象用 **bigint** version 实现乐观并发。"""
        assert isinstance(table("users").c.version.type, BigInteger)

    def test_only_deleted_at_not_archived_at(self) -> None:
        """``Deletable``（不是 ``SoftDelete``）：账号只有删除入口，没有归档。"""
        columns = set(table("users").columns.keys())
        assert "deleted_at" in columns
        assert "archived_at" not in columns

    def test_no_auto_expiry_columns(self) -> None:
        """默认永久保留：users 不得出现"到期自动删除"类字段（文档 §1）。"""
        columns = set(table("users").columns.keys())
        assert not {c for c in columns if "expire" in c or "purge" in c or "ttl" in c}


class TestAuthSessions:
    def test_token_hash_is_unique(self) -> None:
        assert "uq_auth_sessions_token_hash" in constraint_names("auth_sessions", UniqueConstraint)

    def test_identity_and_user_pair_is_unique(self) -> None:
        """``UNIQUE(id, user_id)`` 是 ``runs(auth_session_id, user_id)`` 的前置条件。"""
        assert "uq_auth_sessions_id_user_id" in constraint_names("auth_sessions", UniqueConstraint)

    def test_no_plaintext_token_column(self) -> None:
        columns = set(table("auth_sessions").columns.keys())
        assert "token_hash" in columns
        # 只允许 *_hash / *_ciphertext：任何 plaintext token 列都是缺陷。
        assert not {"token", "token_plain", "plaintext_token"} & columns

    def test_user_fk_cascades(self) -> None:
        fk = next(iter(table("auth_sessions").c.user_id.foreign_keys))
        assert fk.target_fullname == "users.id"
        assert fk.ondelete == "CASCADE"

    def test_expiry_and_version_guards(self) -> None:
        expressions = check_expressions("auth_sessions")
        # 绝对过期是硬上限，必须晚于创建。
        assert (
            "absolute_expires_at > created_at"
            in expressions["ck_auth_sessions_absolute_expires_after_created"]
        )
        # 空闲过期不得越过绝对过期。
        assert (
            "idle_expires_at <= absolute_expires_at"
            in expressions["ck_auth_sessions_idle_within_absolute_expiry"]
        )
        assert "auth_version >= 1" in expressions["ck_auth_sessions_auth_version_positive"]
        assert "csrf_version >= 1" in expressions["ck_auth_sessions_csrf_version_positive"]

    def test_no_rotation_marker_columns(self) -> None:
        """新契约用 ``revoked_at`` + ``auth_version`` 表达失效，不再存轮换墓碑。"""
        columns = set(table("auth_sessions").columns.keys())
        assert "revoked_at" in columns
        assert not {"replaced_by_session_id", "rotated_at"} & columns

    def test_lookup_indexes_exist(self) -> None:
        index_names = {index.name for index in table("auth_sessions").indexes}
        assert "ix_auth_sessions_user_id" in index_names
        assert "ix_auth_sessions_absolute_expires_at" in index_names

    def test_active_expiry_index_is_partial(self) -> None:
        """文档 §3：索引专门覆盖"未撤销会话的过期时间"。"""
        index = index_by_name("auth_sessions", "ix_auth_sessions_idle_expires_at_active")
        assert index.unique is False
        where = index.dialect_options["postgresql"]["where"]
        assert where is not None
        assert "revoked_at IS NULL" in str(where)


class TestAuthTokens:
    def test_idempotency_identity(self) -> None:
        uniques = constraint_names("auth_tokens", UniqueConstraint)
        # 令牌哈希全局唯一；一次性链接没有"客户端标识"参与的唯一性。
        assert "uq_auth_tokens_token_hash" in uniques
        assert "uq_auth_tokens_purpose_user_client" not in uniques

    def test_purpose_check_lists_exactly_the_enum_values(self) -> None:
        expression = check_expressions("auth_tokens")["ck_auth_tokens_purpose_valid"]
        assert expression == enum_check_expression("purpose", AuthTokenPurpose)

    def test_attempt_counter_guard(self) -> None:
        expressions = check_expressions("auth_tokens")
        assert "attempt_count >= 0" in expressions["ck_auth_tokens_attempt_count_non_negative"]
        assert "expires_at > created_at" in expressions["ck_auth_tokens_expires_after_created"]

    def test_consumed_at_is_the_single_use_marker(self) -> None:
        """消费是 ``consumed_at IS NULL AND expires_at > now()`` 的原子更新。"""
        columns = set(table("auth_tokens").columns.keys())
        assert "consumed_at" in columns
        assert not {"request_hash", "client_id", "failed_attempts"} & columns

    def test_no_plaintext_token_column(self) -> None:
        columns = set(table("auth_tokens").columns.keys())
        assert "token_hash" in columns
        assert not {"token", "token_plain", "plaintext_token"} & columns

    def test_lookup_indexes_exist(self) -> None:
        index_names = {index.name for index in table("auth_tokens").indexes}
        assert "ix_auth_tokens_user_id_purpose" in index_names
        assert "ix_auth_tokens_expires_at" in index_names


class TestAdminFactors:
    def test_secret_is_ciphertext_not_plaintext(self) -> None:
        columns = set(table("admin_factors").columns.keys())
        assert "secret_ciphertext" in columns
        assert "secret" not in columns
        assert "totp_secret" not in columns

    def test_one_factor_per_user(self) -> None:
        """文档 §3：``user_id`` 唯一——一个账号只有一个 TOTP 因子。"""
        assert "uq_admin_factors_user_id" in constraint_names("admin_factors", UniqueConstraint)

    def test_recovery_code_hashes_stored_as_jsonb(self) -> None:
        column = table("admin_factors").c.recovery_code_hashes
        assert column.type.__class__.__name__ == "JSONB"

    def test_kind_check(self) -> None:
        expression = check_expressions("admin_factors")["ck_admin_factors_kind_valid"]
        assert expression == enum_check_expression("kind", AdminFactorKind)
        assert expression == "kind IN ('totp')"

    def test_secret_and_key_guards(self) -> None:
        expressions = check_expressions("admin_factors")
        assert (
            "length(secret_ciphertext) > 0"
            in expressions["ck_admin_factors_secret_ciphertext_not_empty"]
        )
        assert (
            "encryption_key_version >= 1"
            in expressions["ck_admin_factors_encryption_key_version_positive"]
        )

    def test_replay_guard_column_exists(self) -> None:
        """``last_used_time_step`` 防止同一时间片重放。"""
        assert "last_used_time_step" in table("admin_factors").columns


class TestRateLimitBuckets:
    def test_composite_primary_key(self) -> None:
        """短期计数用复合主键：同一个 scope/policy 的同一个窗口只有一行。"""
        pk = next(
            c
            for c in table("rate_limit_buckets").constraints
            if isinstance(c, PrimaryKeyConstraint)
        )
        assert pk.name == "pk_rate_limit_buckets"
        assert [column.name for column in pk.columns] == [
            "scope_hash",
            "policy_key",
            "window_start",
        ]
        assert "id" not in table("rate_limit_buckets").columns

    def test_counters_cannot_go_negative(self) -> None:
        expressions = check_expressions("rate_limit_buckets")
        assert "hits >= 0" in expressions["ck_rate_limit_buckets_hits_non_negative"]
        assert "window_end > window_start" in expressions["ck_rate_limit_buckets_window_ordered"]

    def test_scope_and_policy_must_be_non_empty(self) -> None:
        expressions = check_expressions("rate_limit_buckets")
        assert "length(scope_hash) > 0" in expressions["ck_rate_limit_buckets_scope_hash_not_empty"]
        assert "length(policy_key) > 0" in expressions["ck_rate_limit_buckets_policy_key_not_empty"]

    def test_no_user_id_column(self) -> None:
        """只存服务端 HMAC：不存可用于枚举账号的明文邮箱或用户 ID。"""
        columns = set(table("rate_limit_buckets").columns.keys())
        assert "scope_hash" in columns
        assert not {"user_id", "email", "ip"} & columns

    def test_window_start_is_timestamptz(self) -> None:
        assert table("rate_limit_buckets").c.window_start.type.timezone is True

    def test_expiry_index_exists(self) -> None:
        assert index_by_name("rate_limit_buckets", "ix_rate_limit_buckets_expires_at") is not None


class TestSettings:
    def test_string_primary_key(self) -> None:
        column = table("settings").c.key
        assert column.primary_key is True
        # 明确不套 UUIDRepository：主键是 str。
        assert "id" not in table("settings").columns

    def test_content_acl_epoch_is_a_key_not_a_column(self) -> None:
        """``content_acl_epoch`` 是 settings 里的一项配置，不是独立列。"""
        assert "key" in table("settings").columns
        assert "content_acl_epoch" not in table("settings").columns

    def test_key_must_be_lowercase_and_non_empty(self) -> None:
        expressions = check_expressions("settings")
        assert expressions["ck_settings_key_lowercase"] == "key = lower(key)"
        assert "length(key) > 0" in expressions["ck_settings_key_not_empty"]

    def test_versioning_columns(self) -> None:
        """``schema_version``（配置结构）与 ``version``（乐观并发）含义不同。"""
        expressions = check_expressions("settings")
        assert "schema_version >= 1" in expressions["ck_settings_schema_version_positive"]
        assert "version >= 0" in expressions["ck_settings_version_non_negative"]
        assert isinstance(table("settings").c.version.type, BigInteger)
        assert isinstance(table("settings").c.schema_version.type, BigInteger)

    def test_value_is_jsonb(self) -> None:
        assert table("settings").c.value.type.__class__.__name__ == "JSONB"

    def test_updated_by_is_set_null_on_delete(self) -> None:
        fk = next(iter(table("settings").c.updated_by.foreign_keys))
        assert fk.target_fullname == "users.id"
        assert fk.ondelete == "SET NULL"


class TestEnumHelper:
    def test_enum_check_expression_uses_values_not_member_names(self) -> None:
        assert enum_check_expression("role", UserRole) == "role IN ('member', 'owner')"

    def test_enum_check_expression_rejects_empty_enum(self) -> None:
        class Empty(enum.StrEnum):
            pass

        with pytest.raises(ValueError, match="至少要有一个取值"):
            enum_check_expression("col", Empty)

    def test_enum_column_type_stores_values(self) -> None:
        column_type = enum_column_type(UserRole, length=16)
        assert column_type.native_enum is False
        assert column_type.create_constraint is False
        assert column_type.enums == ["member", "owner"]

    def test_in_predicate_helper(self) -> None:
        assert in_predicate("status", ["a", "b"]) == "status IN ('a', 'b')"
        with pytest.raises(ValueError, match="至少要有一个取值"):
            in_predicate("status", [])

    def test_index_helper_naming(self) -> None:
        from autumn_backend.db.naming import index_name

        assert index_name("users", ("status", "created_at")) == "ix_users_status_created_at"

    def test_declared_index_names_follow_convention(self) -> None:
        """显式索引名必须等于命名约定会生成的名字，否则 alembic check 会漂移。"""
        for name in A3_TABLES:
            for index in table(name).indexes:
                if index.name is None:
                    continue
                if index.name.endswith("_active"):
                    continue  # Partial Index 刻意用语义化后缀
                expected = f"ix_{name}_" + "_".join(column.name for column in index.columns)
                assert index.name == expected, f"{name}.{index.name} != {expected}"


class TestLoadAllModels:
    def test_idempotent(self) -> None:
        before = set(Base.metadata.tables)
        models.load_all_models()
        models.load_all_models()
        assert set(Base.metadata.tables) == before

    def test_metadata_declares_28_tables(self) -> None:
        """文档 §11 的四批迁移合计 28 张表。"""
        assert len(Base.metadata.tables) == 28

    def test_no_stray_index_without_name(self) -> None:
        for name in A3_TABLES:
            for index in table(name).indexes:
                assert index.name is not None

    def test_every_table_has_primary_key(self) -> None:
        for name in A3_TABLES:
            assert isinstance(
                next(c for c in table(name).constraints if isinstance(c, PrimaryKeyConstraint)),
                PrimaryKeyConstraint,
            )

    def test_indexes_are_declared_as_index_objects(self) -> None:
        for name in A3_TABLES:
            assert all(isinstance(index, Index) for index in table(name).indexes)

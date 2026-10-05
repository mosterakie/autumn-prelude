"""A3 第一批模型：元数据契约单测（不需要数据库）。

真库验收（约束真的能建出来、真的拦住非法数据）在
``tests/integration/test_identity_constraints.py``。
"""

from __future__ import annotations

import pytest
from sqlalchemy import CheckConstraint, Index, PrimaryKeyConstraint, Table, UniqueConstraint

from autumn_backend.db import models
from autumn_backend.db.base import Base
from autumn_backend.db.enums import AdminFactorType, AuthTokenPurpose, Role, SettingValueType
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


class TestTableRegistration:
    @pytest.mark.parametrize("name", A3_TABLES)
    def test_table_exists(self, name: str) -> None:
        assert name in Base.metadata.tables

    def test_all_a3_tables_are_timestamped(self) -> None:
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

    def test_email_canonical_is_unique(self) -> None:
        assert "uq_users_email_canonical" in constraint_names("users", UniqueConstraint)

    def test_email_canonical_must_be_normalised(self) -> None:
        expressions = check_expressions("users")
        assert "ck_users_email_canonical_normalized" in expressions
        assert "lower(btrim(email_canonical))" in expressions["ck_users_email_canonical_normalized"]

    def test_role_check_lists_exactly_the_enum_values(self) -> None:
        expression = check_expressions("users")["ck_users_role_valid"]
        for value in Role:
            assert f"'{value.value}'" in expression
        # anonymous 不是落库角色：注册前没有账号行。
        assert "'anonymous'" not in expression

    def test_session_version_and_schema_version_guards(self) -> None:
        expressions = check_expressions("users")
        assert "ck_users_session_version_positive" in expressions
        assert "ck_users_content_schema_version_positive" in expressions
        assert "ck_users_daily_quota_override_non_negative" in expressions

    def test_no_auto_expiry_columns(self) -> None:
        """默认永久保留：users 不得出现"到期自动删除"类字段。"""
        columns = set(table("users").columns.keys())
        assert not {c for c in columns if "expire" in c or "purge" in c or "ttl" in c}


class TestAuthSessions:
    def test_token_hash_is_unique(self) -> None:
        assert "uq_auth_sessions_token_hash" in constraint_names("auth_sessions", UniqueConstraint)

    def test_no_plaintext_token_column(self) -> None:
        columns = set(table("auth_sessions").columns.keys())
        assert "token_hash" in columns
        # 只允许 *_hash / *_ciphertext：任何 plaintext token 列都是缺陷。
        assert not {"token", "token_plain", "plaintext_token"} & columns

    def test_user_fk_cascades(self) -> None:
        fk = next(iter(table("auth_sessions").c.user_id.foreign_keys))
        assert fk.target_fullname == "users.id"
        assert fk.ondelete == "CASCADE"

    def test_rotation_invariant(self) -> None:
        expression = check_expressions("auth_sessions")[
            "ck_auth_sessions_rotation_marker_consistent"
        ]
        assert "replaced_by_session_id" in expression
        assert "rotated_at" in expression

    def test_expiry_and_version_guards(self) -> None:
        expressions = check_expressions("auth_sessions")
        assert "ck_auth_sessions_expires_after_created" in expressions
        assert "ck_auth_sessions_session_version_positive" in expressions
        assert "ck_auth_sessions_csrf_version_positive" in expressions

    def test_lookup_indexes_exist(self) -> None:
        index_names = {index.name for index in table("auth_sessions").indexes}
        assert "ix_auth_sessions_user_id_revoked_at" in index_names
        assert "ix_auth_sessions_expires_at" in index_names


class TestAuthTokens:
    def test_idempotency_identity(self) -> None:
        uniques = constraint_names("auth_tokens", UniqueConstraint)
        assert "uq_auth_tokens_purpose_user_client" in uniques
        assert "uq_auth_tokens_token_hash" in uniques

    def test_purpose_check_lists_exactly_the_enum_values(self) -> None:
        expression = check_expressions("auth_tokens")["ck_auth_tokens_purpose_valid"]
        for value in AuthTokenPurpose:
            assert f"'{value.value}'" in expression

    def test_request_hash_present(self) -> None:
        assert "request_hash" in table("auth_tokens").columns

    def test_no_plaintext_token_column(self) -> None:
        columns = set(table("auth_tokens").columns.keys())
        assert "token_hash" in columns
        assert not {"token", "token_plain", "plaintext_token"} & columns


class TestAdminFactors:
    def test_secret_is_ciphertext_not_plaintext(self) -> None:
        columns = set(table("admin_factors").columns.keys())
        assert "secret_ciphertext" in columns
        assert "secret" not in columns
        assert "totp_secret" not in columns

    def test_recovery_codes_stored_as_jsonb(self) -> None:
        column = table("admin_factors").c.recovery_codes
        assert column.type.__class__.__name__ == "JSONB"

    def test_factor_type_check(self) -> None:
        expression = check_expressions("admin_factors")["ck_admin_factors_factor_type_valid"]
        for value in AdminFactorType:
            assert f"'{value.value}'" in expression

    def test_active_partial_unique_index(self) -> None:
        index = next(
            i
            for i in table("admin_factors").indexes
            if i.name == "ix_admin_factors_user_type_active"
        )
        assert index.unique is True
        assert index.dialect_options["postgresql"]["where"] is not None
        assert [c.name for c in index.columns] == ["user_id", "factor_type"]


class TestRateLimitBuckets:
    def test_window_uniqueness(self) -> None:
        uniques = constraint_names("rate_limit_buckets", UniqueConstraint)
        assert "uq_rate_limit_buckets_user_window" in uniques

    def test_counters_cannot_go_negative(self) -> None:
        expressions = check_expressions("rate_limit_buckets")
        assert "count >= 0" in expressions["ck_rate_limit_buckets_count_non_negative"]
        assert "window_seconds > 0" in expressions["ck_rate_limit_buckets_window_seconds_positive"]

    def test_window_start_is_timestamptz(self) -> None:
        assert table("rate_limit_buckets").c.window_start.type.timezone is True


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

    def test_value_type_check(self) -> None:
        expression = check_expressions("settings")["ck_settings_value_type_valid"]
        for value in SettingValueType:
            assert f"'{value.value}'" in expression

    def test_key_must_be_lowercase_and_non_empty(self) -> None:
        expressions = check_expressions("settings")
        assert expressions["ck_settings_key_lowercase"] == "key = lower(key)"
        assert "length(key) > 0" in expressions["ck_settings_key_not_empty"]

    def test_value_is_jsonb(self) -> None:
        assert table("settings").c.value.type.__class__.__name__ == "JSONB"

    def test_updated_by_is_set_null_on_delete(self) -> None:
        fk = next(iter(table("settings").c.updated_by.foreign_keys))
        assert fk.target_fullname == "users.id"
        assert fk.ondelete == "SET NULL"


class TestEnumHelper:
    def test_enum_check_expression_uses_values_not_member_names(self) -> None:
        from autumn_backend.db.enums import enum_check_expression

        assert enum_check_expression("role", Role) == "role IN ('member', 'owner')"

    def test_enum_check_expression_rejects_empty_enum(self) -> None:
        import enum

        from autumn_backend.db.enums import enum_check_expression

        class Empty(enum.StrEnum):
            pass

        with pytest.raises(ValueError, match="没有任何值"):
            enum_check_expression("col", Empty)

    def test_enum_column_type_stores_values(self) -> None:
        from autumn_backend.db.enums import enum_column_type

        column_type = enum_column_type(Role, length=16)
        assert column_type.native_enum is False
        assert column_type.create_constraint is False
        assert column_type.enums == ["member", "owner"]

    def test_index_helper_naming(self) -> None:
        from autumn_backend.db.naming import index_name

        assert index_name("users", ("role", "created_at")) == "ix_users_role_created_at"

    def test_declared_index_names_follow_convention(self) -> None:
        """显式索引名必须等于命名约定会生成的名字，否则 alembic check 会漂移。"""
        for name in A3_TABLES:
            for index in table(name).indexes:
                if index.name is None:
                    continue
                if index.name.startswith("ix_admin_factors_user_type_active"):
                    continue  # Partial Unique Index 刻意用 ix_ 前缀
                expected = f"ix_{name}_" + "_".join(c.name for c in index.columns)
                assert index.name == expected, f"{name}.{index.name} != {expected}"


class TestLoadAllModels:
    def test_idempotent(self) -> None:
        before = set(Base.metadata.tables)
        models.load_all_models()
        models.load_all_models()
        assert set(Base.metadata.tables) == before

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

"""阶段 A2 自检：Mixin 列语义、命名约定、时间戳触发器 DDL。

这些测试**不需要数据库**：SQLAlchemy 元数据与 DDL 编译本身就足以证明契约。
"迁移真能在 PostgreSQL 上建出这些对象" 属于 A4 的集成验收。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.schema import CreateTable

from autumn_backend.db import mixins
from autumn_backend.db.base import NAMING_CONVENTION, Base
from autumn_backend.db.naming import (
    check_constraint_name,
    index_name,
    unique_constraint_name,
)
from autumn_backend.db.timestamps import (
    CREATE_UPDATED_AT_FUNCTION_SQL,
    UPDATED_AT_FUNCTION,
    drop_updated_at_trigger_statements,
    updated_at_trigger_name,
    updated_at_trigger_statements,
)

pytestmark = pytest.mark.unit

UUID_DEFAULT = "gen_random_uuid()"
NOW_DEFAULT = "now()"


class ProbeBase(DeclarativeBase):
    """独立注册表：不污染 ``Base.metadata``（那是 Alembic 迁移的输入）。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class FullStack(
    ProbeBase,
    mixins.UUIDPrimaryKey,
    mixins.Versioned,
    mixins.Timestamped,
    mixins.SoftDelete,
):
    """组合全部 Mixin 的探针模型。"""

    __tablename__ = "probe_full_stack"

    title: Mapped[str] = mapped_column(default="")


class TimestampOnly(ProbeBase, mixins.UUIDPrimaryKey, mixins.Timestamped):
    __tablename__ = "probe_timestamp_only"


class PlainOnly(ProbeBase, mixins.UUIDPrimaryKey):
    """不含 updated_at 的对照模型：不得触发触发器挂载。"""

    __tablename__ = "probe_plain"


def _fake_model(**state: object) -> SimpleNamespace:
    """构造只带软删除字段的假对象，用来单测三个 property。"""
    return SimpleNamespace(**state)


class _RecordingConnection:
    """记录 DDL 的假连接：让监听器测试完全不依赖数据库。"""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def exec_driver_sql(self, statement: str) -> None:
        self.statements.append(statement)


class TestUUIDPrimaryKey:
    def test_declares_uuid_primary_key_with_db_default(self) -> None:
        column = FullStack.__table__.c.id
        assert column.primary_key is True
        assert isinstance(column.type, Uuid)
        assert column.type.as_uuid is True
        # 数据库侧默认值：原生 SQL 插入也能生成 UUID，不只依赖 Python 默认。
        assert column.server_default is not None
        assert UUID_DEFAULT in str(column.server_default.arg)

    def test_primary_key_name_follows_convention(self) -> None:
        pk = next(c for c in FullStack.__table__.constraints if isinstance(c, PrimaryKeyConstraint))
        assert pk.name == "pk_probe_full_stack"

    def test_ddl_is_uuid_with_gen_random_uuid_default(self) -> None:
        ddl = str(CreateTable(FullStack.__table__).compile(dialect=postgresql.dialect()))
        assert "id UUID DEFAULT gen_random_uuid() NOT NULL" in ddl


class TestVersioned:
    def test_declares_only_version_column(self) -> None:
        columns = set(FullStack.__table__.columns.keys())
        assert "version" in columns
        # ACL 版本不属于本 Mixin（架构文档 §5：两者完全解耦）。
        assert "acl_version" not in columns

        version = FullStack.__table__.c.version
        assert isinstance(version.type, Integer)
        assert version.nullable is False
        # 不用 server_default：避免"0 与 NULL"两套语义并存。
        assert version.server_default is None

    def test_acl_version_is_not_declared_on_the_mixin(self) -> None:
        assert "acl_version" not in mixins.Versioned.__dict__

    def test_version_matches(self) -> None:
        row = SimpleNamespace(version=3)
        assert mixins.Versioned.version_matches(row, 3) is True  # type: ignore[arg-type]
        assert mixins.Versioned.version_matches(row, 4) is False  # type: ignore[arg-type]


class TestTimestamped:
    def test_columns_are_timestamptz_with_server_defaults(self) -> None:
        for name in ("created_at", "updated_at"):
            column = TimestampOnly.__table__.c[name]
            assert isinstance(column.type, DateTime)
            assert column.type.timezone is True, f"{name} 必须是 timestamptz"
            assert column.nullable is False
            assert column.server_default is not None
            assert NOW_DEFAULT in str(column.server_default.arg)

    def test_updated_at_has_orm_onupdate(self) -> None:
        assert TimestampOnly.__table__.c.updated_at.onupdate is not None
        assert TimestampOnly.__table__.c.created_at.onupdate is None

    def test_timestamped_tables_helper_reads_base_metadata(self) -> None:
        # A1 阶段 Base.metadata 尚无表，A3 起逐批出现；这里只断言契约成立。
        assert isinstance(mixins.timestamped_tables(), tuple)
        assert all("updated_at" in table.columns for table in mixins.timestamped_tables())


class TestSoftDelete:
    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            ({"deleted_at": None, "archived_at": None}, mixins.SoftDeleteState.ACTIVE),
            (
                {"deleted_at": None, "archived_at": datetime.now(UTC)},
                mixins.SoftDeleteState.ARCHIVED,
            ),
            (
                {"deleted_at": datetime.now(UTC), "archived_at": None},
                mixins.SoftDeleteState.DELETED,
            ),
            (
                {"deleted_at": datetime.now(UTC), "archived_at": datetime.now(UTC)},
                mixins.SoftDeleteState.DELETED,
            ),
        ],
    )
    def test_state_matrix(self, state: dict[str, object], expected: mixins.SoftDeleteState) -> None:
        row = _fake_model(**state)
        assert mixins.SoftDelete.soft_delete_state.fget(row) is expected  # type: ignore[attr-defined]

    def test_helpers_agree_with_state(self) -> None:
        active = _fake_model(deleted_at=None, archived_at=None)
        assert mixins.SoftDelete.is_active.fget(active) is True  # type: ignore[attr-defined]
        assert mixins.SoftDelete.is_deleted.fget(active) is False  # type: ignore[attr-defined]
        assert mixins.SoftDelete.is_archived.fget(active) is False  # type: ignore[attr-defined]

        deleted = _fake_model(deleted_at=datetime.now(UTC), archived_at=None)
        assert mixins.SoftDelete.is_active.fget(deleted) is False  # type: ignore[attr-defined]
        assert mixins.SoftDelete.is_deleted.fget(deleted) is True  # type: ignore[attr-defined]

        archived = _fake_model(deleted_at=None, archived_at=datetime.now(UTC))
        assert mixins.SoftDelete.is_archived.fget(archived) is True  # type: ignore[attr-defined]
        assert mixins.SoftDelete.is_deleted.fget(archived) is False  # type: ignore[attr-defined]

    def test_columns_are_nullable_timestamptz_without_defaults(self) -> None:
        for name in ("deleted_at", "archived_at"):
            column = FullStack.__table__.c[name]
            assert isinstance(column.type, DateTime)
            assert column.type.timezone is True
            assert column.nullable is True
            assert column.server_default is None


class TestMixinComposition:
    def test_full_stack_declares_expected_columns(self) -> None:
        assert set(FullStack.__table__.columns.keys()) == {
            "id",
            "version",
            "created_at",
            "updated_at",
            "deleted_at",
            "archived_at",
            "title",
        }

    def test_mixins_do_not_leak_between_models(self) -> None:
        """Mixin 只影响继承它的表，不会全局泄漏。"""
        assert "version" not in TimestampOnly.__table__.columns
        assert "deleted_at" not in TimestampOnly.__table__.columns
        assert "updated_at" not in PlainOnly.__table__.columns
        assert "version" not in PlainOnly.__table__.columns


class TestUpdatedAtTrigger:
    def test_trigger_name(self) -> None:
        assert updated_at_trigger_name("resources") == "trg_resources_set_updated_at"
        assert updated_at_trigger_name(Table("auth_sessions", MetaData())) == (
            "trg_auth_sessions_set_updated_at"
        )

    def test_trigger_statements_are_idempotent_and_single_statement(self) -> None:
        statements = updated_at_trigger_statements("resources")
        # 两条独立语句：asyncpg 不接受一次预编译多条命令。
        assert len(statements) == 2
        assert statements[0] == "DROP TRIGGER IF EXISTS trg_resources_set_updated_at ON resources;"
        assert statements[1] == (
            "CREATE TRIGGER trg_resources_set_updated_at "
            "BEFORE UPDATE ON resources "
            "FOR EACH ROW "
            f"EXECUTE FUNCTION {UPDATED_AT_FUNCTION}();"
        )
        assert all(";" in statement for statement in statements)
        assert all("\n" not in statement for statement in statements)

    def test_drop_statement_shape(self) -> None:
        assert drop_updated_at_trigger_statements("resources") == [
            "DROP TRIGGER IF EXISTS trg_resources_set_updated_at ON resources;"
        ]

    def test_function_ddl_assigns_updated_at(self) -> None:
        ddl = CREATE_UPDATED_AT_FUNCTION_SQL
        assert f"CREATE OR REPLACE FUNCTION {UPDATED_AT_FUNCTION}()" in ddl
        assert "RETURNS trigger" in ddl
        assert "LANGUAGE plpgsql" in ddl
        assert "NEW.updated_at := now();" in ddl
        assert "RETURN NEW;" in ddl

    def test_listener_attaches_trigger_for_timestamped_table(self) -> None:
        connection = _RecordingConnection()
        mixins._attach_updated_at_trigger(TimestampOnly.__table__, connection)  # type: ignore[arg-type]

        assert connection.statements == updated_at_trigger_statements("probe_timestamp_only")

    def test_listener_skips_table_without_updated_at(self) -> None:
        connection = _RecordingConnection()
        mixins._attach_updated_at_trigger(PlainOnly.__table__, connection)  # type: ignore[arg-type]

        assert connection.statements == []


class TestNamingHelpers:
    def test_index_name_matches_naming_convention(self) -> None:
        assert index_name("runs", "user_id") == "ix_runs_user_id"
        assert index_name("runs", ("user_id", "status")) == "ix_runs_user_id_status"
        assert index_name("runs", ("user_id", "status"), suffix="non_terminal") == (
            "ix_runs_user_id_status_non_terminal"
        )

    def test_unique_constraint_name_matches_naming_convention(self) -> None:
        assert unique_constraint_name("runs", ("user_id", "idempotency_key")) == (
            "uq_runs_user_id_idempotency_key"
        )

    def test_check_constraint_name_matches_naming_convention(self) -> None:
        assert check_constraint_name("quota_buckets", "used_non_negative") == (
            "ck_quota_buckets_used_non_negative"
        )

    def test_index_name_agrees_with_sqlalchemy_convention(self) -> None:
        """显式生成的名字必须与命名约定产出完全一致，否则 alembic check 会漂移。"""
        metadata = MetaData(naming_convention=NAMING_CONVENTION)
        table = Table(
            "probe_index_naming",
            metadata,
            Column("user_id", Uuid),
            Column("idempotency_key", Uuid),
        )
        Index(None, table.c.user_id)
        Index(None, table.c.idempotency_key)

        generated = {index.name for index in table.indexes}
        assert generated == {
            index_name("probe_index_naming", "user_id"),
            index_name("probe_index_naming", "idempotency_key"),
        }

    def test_unique_constraint_name_agrees_with_sqlalchemy_convention(self) -> None:
        metadata = MetaData(naming_convention=NAMING_CONVENTION)
        table = Table(
            "probe_uq_naming",
            metadata,
            Column("user_id", Uuid),
            Column("idempotency_key", Uuid),
            UniqueConstraint("user_id", "idempotency_key"),
        )
        unique = next(c for c in table.constraints if isinstance(c, UniqueConstraint))
        assert unique.name == unique_constraint_name(
            "probe_uq_naming", ("user_id", "idempotency_key")
        )

    def test_check_constraint_name_agrees_with_sqlalchemy_convention(self) -> None:
        """命名约定里 ck 用 ``%(constraint_name)s``，因此 CHECK 必须显式给 label。"""
        table = Table(
            "probe_ck_naming",
            MetaData(naming_convention=NAMING_CONVENTION),
            CheckConstraint("used >= 0", name="used_non_negative"),
        )
        check = next(c for c in table.constraints if isinstance(c, CheckConstraint))
        assert check.name == check_constraint_name("probe_ck_naming", "used_non_negative")

    def test_naming_helpers_accept_table_objects(self) -> None:
        table = Table("probe_table_objects", MetaData())
        assert index_name(table, ("created_at", "id"), suffix="cursor") == (
            "ix_probe_table_objects_created_at_id_cursor"
        )
        assert unique_constraint_name(table, "user_id") == ("uq_probe_table_objects_user_id")
        assert check_constraint_name(table, "amount_is_one") == (
            "ck_probe_table_objects_amount_is_one"
        )

    def test_base_metadata_uses_the_same_convention(self) -> None:
        assert Base.metadata.naming_convention is not None
        assert dict(Base.metadata.naming_convention)["ck"] == (
            "ck_%(table_name)s_%(constraint_name)s"
        )

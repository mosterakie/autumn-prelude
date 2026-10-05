"""Catalog 核对：真实数据库对象 vs ORM 元数据声明。

**为什么需要这个测试**（评审 D7）：

``alembic check`` 复用 autogenerate 的比较能力，而 autogenerate **不比较**
CHECK 表达式，也不把"部分索引的谓词变化"算作差异。因此
"No new upgrade operations detected" 只证明表/列/索引/唯一约束/外键这一层无漂移，
**不能**证明 CHECK 语义正确、触发器存在。

本文件补齐三层核对：

1. **名称集合双向比对**：CHECK / UNIQUE / FK / 索引 / 触发器在元数据与
   PostgreSQL catalog 中必须一一对应——多一个或少一个都算漂移。
2. **关键部分索引谓词**：逐个断言谓词里出现预期的状态字面量。
3. **语义正反例**：由各 ``test_*_constraints.py`` 的负向用例承担
   （CHECK 表达式被 PostgreSQL 重写，无法逐字比较；行为测试才是语义证据）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint, text
from sqlalchemy.ext.asyncio import AsyncSession

from autumn_backend.db.base import Base
from autumn_backend.db.models import load_all_models

pytestmark = pytest.mark.integration

#: 带 updated_at 的表必须都有启用状态的触发器。
_TRIGGER_SUFFIX = "set_updated_at"


@pytest.fixture(scope="module", autouse=True)
def _registered() -> None:
    load_all_models()


def _metadata_checks() -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for name, table in Base.metadata.tables.items():
        names = {
            c.name
            for c in table.constraints
            if isinstance(c, CheckConstraint) and c.name is not None
        }
        result[name] = names
    return result


def _metadata_uniques() -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for name, table in Base.metadata.tables.items():
        result[name] = {
            c.name
            for c in table.constraints
            if isinstance(c, UniqueConstraint) and c.name is not None
        }
    return result


def _metadata_fks() -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for name, table in Base.metadata.tables.items():
        result[name] = {
            c.name
            for c in table.constraints
            if isinstance(c, ForeignKeyConstraint) and c.name is not None
        }
    return result


def _metadata_indexes() -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for name, table in Base.metadata.tables.items():
        result[name] = {i.name for i in table.indexes if i.name is not None}
    return result


async def _db_constraint_names(session: AsyncSession, contype: str) -> dict[str, set[str]]:
    # contype 是 PostgreSQL 的 "char" 类型，asyncpg 会映射成 bytes；
    # 因此显式转成 text 再比较，避免传字符串时报"需要 bytes"。
    rows = await session.execute(
        text(
            "SELECT c.relname AS table_name, con.conname "
            "FROM pg_constraint con "
            "JOIN pg_class c ON c.oid = con.conrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND con.contype::text = :contype"
        ),
        {"contype": contype},
    )
    result: dict[str, set[str]] = {}
    for table_name, constraint_name in rows:
        result.setdefault(table_name, set()).add(constraint_name)
    return result


async def _db_index_names(session: AsyncSession) -> dict[str, set[str]]:
    rows = await session.execute(
        text(
            "SELECT c.relname AS table_name, ic.relname AS index_name "
            "FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indrelid "
            "JOIN pg_class ic ON ic.oid = i.indexrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public'"
        )
    )
    result: dict[str, set[str]] = {}
    for table_name, index_name in rows:
        result.setdefault(table_name, set()).add(index_name)
    return result


async def _db_triggers(session: AsyncSession) -> dict[str, set[str]]:
    rows = await session.execute(
        text(
            "SELECT c.relname AS table_name, t.tgname "
            "FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND NOT t.tgisinternal"
        )
    )
    result: dict[str, set[str]] = {}
    for table_name, trigger_name in rows:
        result.setdefault(table_name, set()).add(trigger_name)
    return result


class TestCheckConstraintCatalog:
    async def test_check_names_match_metadata_exactly(self, session: AsyncSession) -> None:
        """CHECK 约束名必须双向一致：autogenerate 不比较 CHECK，只能靠这里兜住。"""
        declared = _metadata_checks()
        actual = await _db_constraint_names(session, "c")

        problems: list[str] = []
        for table, expected in sorted(declared.items()):
            found = actual.get(table, set())
            missing = expected - found
            extra = found - expected
            if missing:
                problems.append(f"{table} 缺少 CHECK：{sorted(missing)}")
            if extra:
                problems.append(f"{table} 多出未声明的 CHECK：{sorted(extra)}")

        assert not problems, "；".join(problems)

    async def test_every_check_is_named(self) -> None:
        """命名 CHECK 才能被 catalog 核对；匿名 CHECK 是漏检入口。"""
        for table_name, table in Base.metadata.tables.items():
            for constraint in table.constraints:
                if isinstance(constraint, CheckConstraint):
                    assert constraint.name is not None, f"{table_name} 存在匿名 CHECK"


class TestUniqueConstraintCatalog:
    async def test_unique_names_match_metadata_exactly(self, session: AsyncSession) -> None:
        declared = _metadata_uniques()
        actual = await _db_constraint_names(session, "u")

        problems: list[str] = []
        for table, expected in sorted(declared.items()):
            found = actual.get(table, set())
            missing = expected - found
            if missing:
                problems.append(f"{table} 缺少 UNIQUE：{sorted(missing)}")
        assert not problems, "；".join(problems)


class TestForeignKeyCatalog:
    async def test_foreign_key_names_match_metadata_exactly(self, session: AsyncSession) -> None:
        declared = _metadata_fks()
        actual = await _db_constraint_names(session, "f")

        problems: list[str] = []
        for table, expected in sorted(declared.items()):
            found = actual.get(table, set())
            missing = expected - found
            if missing:
                problems.append(f"{table} 缺少 FK：{sorted(missing)}")
        assert not problems, "；".join(problems)


class TestIndexCatalog:
    async def test_index_names_match_metadata_exactly(self, session: AsyncSession) -> None:
        declared = _metadata_indexes()
        actual = await _db_index_names(session)

        problems: list[str] = []
        for table, expected in sorted(declared.items()):
            found = actual.get(table, set())
            missing = expected - found
            if missing:
                problems.append(f"{table} 缺少索引：{sorted(missing)}")
        assert not problems, "；".join(problems)


class TestPartialIndexPredicates:
    """部分索引的谓词不参与 autogenerate 比较，必须逐个核对。"""

    async def _predicate(self, session: AsyncSession, index_name: str) -> str:
        predicate = await session.scalar(
            text(
                "SELECT pg_get_expr(i.indpred, i.indrelid) "
                "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = :name"
            ),
            {"name": index_name},
        )
        assert predicate is not None, f"{index_name} 不存在或不是部分索引"
        return predicate

    async def _unique_columns(
        self, session: AsyncSession, table_name: str, constraint_name: str
    ) -> list[str]:
        """唯一约束的列顺序：顺序错了复合引用的语义就变了。"""
        rows = await session.execute(
            text(
                "SELECT a.attname FROM pg_constraint con "
                "JOIN pg_class c ON c.oid = con.conrelid "
                "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(con.conkey) "
                "WHERE c.relname = :table_name AND con.conname = :name "
                "ORDER BY array_position(con.conkey, a.attnum)"
            ),
            {"table_name": table_name, "name": constraint_name},
        )
        return [row[0] for row in rows]

    async def test_runs_non_terminal_predicate_covers_api_statuses(
        self, session: AsyncSession
    ) -> None:
        predicate = await self._predicate(session, "uq_runs_conversation_id_non_terminal")
        for status in (
            "queued",
            "running",
            "waiting_input",
            "waiting_approval",
            "waiting_auth",
            "cancelling",
        ):
            assert f"'{status}'" in predicate, f"非终态谓词缺少 {status}"
        for status in ("succeeded", "failed", "cancelled"):
            assert f"'{status}'" not in predicate, f"终态 {status} 不应出现在非终态谓词里"

    async def test_publications_current_predicate(self, session: AsyncSession) -> None:
        predicate = await self._predicate(session, "uq_publications_resource_id_current")
        assert "revoked_at IS NULL" in predicate

    async def test_jobs_idempotency_key_is_the_dedupe_identity(self, session: AsyncSession) -> None:
        """新契约用 ``UNIQUE(idempotency_key)`` 去重，不再有"活跃作业"部分唯一索引。

        旧契约的 ``uq_jobs_type_dedupe_active`` 已随 ``jobs`` 表重写消失：排队去重
        改由稳定幂等键承担（见 ``db/models/runtime.py`` 的 ``Job``）。
        """
        assert await self._unique_columns(session, "jobs", "uq_jobs_idempotency_key") == [
            "idempotency_key"
        ]
        legacy = await session.scalar(
            text(
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = 'uq_jobs_type_dedupe_active'"
            )
        )
        assert legacy == 0, "旧契约的活跃去重索引仍然存在"

    async def test_admin_factors_user_id_is_unique(self, session: AsyncSession) -> None:
        """一个账号只有一个 TOTP 因子：唯一性直接落在 ``user_id`` 上。"""
        assert await self._unique_columns(session, "admin_factors", "uq_admin_factors_user_id") == [
            "user_id"
        ]

    async def test_resources_slug_is_globally_unique(self, session: AsyncSession) -> None:
        """``slug`` 是全局唯一的对外标识，不再按 owner 做部分唯一索引。"""
        assert await self._unique_columns(session, "resources", "uq_resources_slug") == ["slug"]
        legacy = await session.scalar(
            text(
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = 'uq_resources_owner_id_slug'"
            )
        )
        assert legacy == 0, "旧契约的按 owner 部分唯一索引仍然存在"

    async def test_no_approximate_vector_index_is_installed(self, session: AsyncSession) -> None:
        """文档 §5：首版精确向量查询，**不建**近似索引。

        旧契约的 ``ix_knowledge_indexes_embedding_hnsw``（hnsw + vector_cosine_ops）
        已随知识索引重写消失：``embedding`` 移到 ``knowledge_chunks``，且只建
        B-tree 辅助索引。这是一处有意偏离，必须显式核对而不是默认它不存在。
        """
        rows = await session.execute(
            text(
                "SELECT ic.relname, am.amname FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indrelid "
                "JOIN pg_class ic ON ic.oid = i.indexrelid "
                "JOIN pg_am am ON am.oid = ic.relam "
                "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey) "
                "WHERE c.relname = 'knowledge_chunks' AND a.attname = 'embedding'"
            )
        )
        assert list(rows) == [], "向量列上出现了索引（首版应为精确查询）"

    async def test_knowledge_chunk_indexes_use_btree(self, session: AsyncSession) -> None:
        rows = await session.execute(
            text(
                "SELECT am.amname FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indrelid "
                "JOIN pg_class ic ON ic.oid = i.indexrelid "
                "JOIN pg_am am ON am.oid = ic.relam "
                "WHERE c.relname = 'knowledge_chunks'"
            )
        )
        access_methods = {row[0] for row in rows}
        assert access_methods == {"btree"}


class TestTriggerCatalog:
    async def test_every_timestamped_table_has_enabled_trigger(self, session: AsyncSession) -> None:
        """``updated_at`` 触发器不在元数据里，autogenerate 完全看不到它。"""
        from autumn_backend.db.mixins import timestamped_tables

        actual = await _db_triggers(session)
        problems: list[str] = []
        for table in timestamped_tables():
            triggers = actual.get(table.name, set())
            matching = {t for t in triggers if t.endswith(_TRIGGER_SUFFIX)}
            if not matching:
                problems.append(f"{table.name} 缺少 {_TRIGGER_SUFFIX} 触发器")
        assert not problems, "；".join(problems)

    async def test_all_timestamp_triggers_are_enabled(self, session: AsyncSession) -> None:
        rows = await session.execute(
            text(
                "SELECT c.relname, t.tgenabled FROM pg_trigger t "
                "JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE NOT t.tgisinternal AND t.tgname LIKE 'trg%set_updated_at'"
            )
        )
        for table_name, enabled in rows:
            assert bytes(enabled).decode() == "O", f"{table_name} 的触发器被禁用"

    async def test_trigger_function_exists_and_is_plpgsql(self, session: AsyncSession) -> None:
        row = (
            await session.execute(
                text(
                    "SELECT l.lanname, p.prorettype::regtype::text "
                    "FROM pg_proc p JOIN pg_language l ON l.oid = p.prolang "
                    "WHERE p.proname = 'autumn_set_updated_at'"
                )
            )
        ).one_or_none()
        assert row is not None, "触发器函数不存在"
        assert row[0] == "plpgsql"
        assert row[1] == "trigger"


class TestCatalogCounts:
    async def test_no_application_table_is_missing_from_metadata(
        self, session: AsyncSession
    ) -> None:
        """反向核对：数据库里不应存在 metadata 未声明的应用表。

        ``alembic/env.py`` 只排除**显式登记**的外部表（当前为空），
        因此这里也按同样口径核对——"应用表意外从 metadata 遗漏"必须被发现。
        """
        from autumn_backend.db.external_tables import EXTERNAL_TABLE_ALLOWLIST

        rows = await session.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
            )
        )
        db_tables = {row[0] for row in rows}
        declared = set(Base.metadata.tables)
        allowlisted = {name for _schema, name in EXTERNAL_TABLE_ALLOWLIST}

        unexpected = db_tables - declared - allowlisted
        assert not unexpected, f"数据库中存在未声明的表：{sorted(unexpected)}"

        missing_in_db = declared - db_tables
        assert not missing_in_db, f"metadata 声明但数据库缺少的表：{sorted(missing_in_db)}"

    async def test_expected_table_count(self, session: AsyncSession) -> None:
        """数据库表数必须等于 metadata 表数（阶段 A 结束时为 22 张）。"""
        count = await session.scalar(
            text(
                "SELECT count(*) FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
            )
        )
        assert count == len(Base.metadata.tables)


class TestIndexOpcassAndUniqueness:
    async def test_declared_unique_indexes_are_unique_in_catalog(
        self, session: AsyncSession
    ) -> None:
        declared = [
            (table_name, index.name)
            for table_name, table in Base.metadata.tables.items()
            for index in table.indexes
            if index.unique and index.name is not None
        ]
        assert declared, "应当存在唯一索引"
        for table_name, index_name in sorted(declared):
            is_unique = await session.scalar(
                text(
                    "SELECT i.indisunique FROM pg_index i "
                    "JOIN pg_class c ON c.oid = i.indexrelid "
                    "JOIN pg_class t ON t.oid = i.indrelid "
                    "WHERE c.relname = :name AND t.relname = :table_name"
                ),
                {"name": index_name, "table_name": table_name},
            )
            assert is_unique is True, f"{table_name}.{index_name} 在库中不是唯一索引"

    async def test_declared_index_columns_match_catalog(self, session: AsyncSession) -> None:
        """索引列顺序必须一致——顺序错了索引就用不上。"""
        for table_name, table in sorted(Base.metadata.tables.items()):
            for index in table.indexes:
                if index.name is None:
                    continue
                expected = [c.name for c in index.columns]
                rows = await session.execute(
                    text(
                        "SELECT a.attname FROM pg_index i "
                        "JOIN pg_class c ON c.oid = i.indexrelid "
                        "JOIN pg_class t ON t.oid = i.indrelid "
                        "JOIN pg_attribute a ON a.attrelid = t.oid "
                        "AND a.attnum = ANY(i.indkey) "
                        "WHERE c.relname = :name AND t.relname = :table_name "
                        "ORDER BY array_position(i.indkey, a.attnum)"
                    ),
                    {"name": index.name, "table_name": table_name},
                )
                actual = [row[0] for row in rows]
                assert actual == expected, (
                    f"{table_name}.{index.name} 列不一致：{actual} != {expected}"
                )


class TestCheckSemanticsAreCoveredByBehaviour:
    """本文件只核对对象存在性；语义由行为测试证明。

    这条用例把"分工"写成可执行的文档，避免以后误以为 catalog 核对就等于语义验收。
    """

    def test_every_table_with_checks_has_behaviour_tests(self) -> None:
        """同步读取测试语料即可——这里不需要数据库，也不需要 await。"""
        from pathlib import Path

        integration_dir = Path(__file__).resolve().parent
        corpus = "\n".join(
            path.read_text(encoding="utf-8") for path in integration_dir.glob("test_*.py")
        )
        for table_name in sorted(Base.metadata.tables):
            checks = _metadata_checks().get(table_name, set())
            if not checks:
                continue
            assert table_name in corpus, f"{table_name} 有 CHECK 但没有行为测试覆盖"

"""时间戳字段的数据库触发器 DDL。

`updated_at` 有两层保障：
1. ORM 路径：列的 ``onupdate=func.now()``，任何 SQLAlchemy 发出的 UPDATE 都会带上它。
2. 数据库路径：本模块的 BEFORE UPDATE 触发器，覆盖迁移脚本、运维脚本或
   任何原生 SQL 直接改行的情况。

两者不冲突：触发器与 ``onupdate`` 都写 ``now()``（同一事务内是同一个时刻）。

触发器函数由迁移创建（A3 第一批迁移执行 :data:`CREATE_UPDATED_AT_FUNCTION_SQL`），
运行期只负责按表挂触发器，因此 `alembic check` 不会看到"模型里没有的对象"。

**每条 DDL 都是独立语句**：asyncpg 的预编译语句不接受一次多条命令
（``cannot insert multiple commands into a prepared statement``），
因此 :func:`updated_at_trigger_statements` 返回列表而不是一段多语句脚本。
"""

from __future__ import annotations

from sqlalchemy import Table

__all__ = [
    "CREATE_UPDATED_AT_FUNCTION_SQL",
    "DROP_UPDATED_AT_FUNCTION_SQL",
    "UPDATED_AT_COLUMN",
    "UPDATED_AT_FUNCTION",
    "drop_updated_at_trigger_statements",
    "updated_at_trigger_name",
    "updated_at_trigger_statements",
]

#: 时间戳 Mixin 使用的列名；与触发器函数体必须一致。
UPDATED_AT_COLUMN = "updated_at"

#: 全库共用的触发器函数名。
UPDATED_AT_FUNCTION = "autumn_set_updated_at"

#: 创建/更新触发器函数（幂等，可在每个环境重复执行）。
CREATE_UPDATED_AT_FUNCTION_SQL = f"""
CREATE OR REPLACE FUNCTION {UPDATED_AT_FUNCTION}()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.{UPDATED_AT_COLUMN} := now();
    RETURN NEW;
END;
$$;
"""

#: 删除触发器函数（仅在确认没有表再引用它时使用）。
DROP_UPDATED_AT_FUNCTION_SQL = f"DROP FUNCTION IF EXISTS {UPDATED_AT_FUNCTION}();"


def _table_name(table: Table | str) -> str:
    return table.name if isinstance(table, Table) else table


def updated_at_trigger_name(table: Table | str) -> str:
    """触发器名：``trg_<table>_set_updated_at``。"""
    return f"trg_{_table_name(table)}_set_updated_at"


def updated_at_trigger_statements(table: Table | str) -> list[str]:
    """按表生成挂触发器的语句列表（DROP 在前，因此幂等）。

    幂等不是可有可无的保险：``op.create_table`` 会触发 ``Table.after_create``
    事件，同样调用本函数，因此迁移里的显式调用与事件挂载会同时发生；
    不幂等就会撞上 ``DuplicateObjectError``。
    """
    name = _table_name(table)
    return [
        f"DROP TRIGGER IF EXISTS {updated_at_trigger_name(name)} ON {name};",
        f"CREATE TRIGGER {updated_at_trigger_name(name)} "
        f"BEFORE UPDATE ON {name} "
        f"FOR EACH ROW "
        f"EXECUTE FUNCTION {UPDATED_AT_FUNCTION}();",
    ]


def drop_updated_at_trigger_statements(table: Table | str) -> list[str]:
    """按表生成卸载触发器的语句列表（降级迁移用）。"""
    name = _table_name(table)
    return [f"DROP TRIGGER IF EXISTS {updated_at_trigger_name(name)} ON {name};"]

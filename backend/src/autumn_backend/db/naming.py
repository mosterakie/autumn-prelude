"""数据库对象命名工具。

索引/约束的**默认名**由 ``db/base.py`` 的 ``NAMING_CONVENTION`` 决定，
但 Partial Unique Index 与命名 CHECK 必须显式给名字（PostgreSQL 不会替它们取名）。
为了让"模型里声明的名字"与"迁移里写的名字"永远一致，名字统一在这里生成，
而不是在两处各写一遍字符串。
"""

from __future__ import annotations

from sqlalchemy import Column, Table

__all__ = ["check_constraint_name", "index_name", "unique_constraint_name"]


def _column_names(columns: Column[object] | str | tuple[Column[object] | str, ...]) -> str:
    """把列对象/列名规范成命名约定里的 ``column_0_N_name`` 片段。"""
    if isinstance(columns, tuple):
        items: tuple[Column[object] | str, ...] = columns
    else:
        items = (columns,)
    names = [item.name if isinstance(item, Column) else item for item in items]
    return "_".join(names)


def _table_name(table: Table | str) -> str:
    return table.name if isinstance(table, Table) else table


def index_name(
    table: Table | str,
    columns: Column[object] | str | tuple[Column[object] | str, ...],
    *,
    suffix: str | None = None,
) -> str:
    """索引名：``ix_<table>_<cols>[_<suffix>]``，与命名约定一致。"""
    base = f"ix_{_table_name(table)}_{_column_names(columns)}"
    return f"{base}_{suffix}" if suffix else base


def unique_constraint_name(
    table: Table | str,
    columns: Column[object] | str | tuple[Column[object] | str, ...],
) -> str:
    """唯一约束名：``uq_<table>_<cols>``，与命名约定一致。"""
    return f"uq_{_table_name(table)}_{_column_names(columns)}"


def check_constraint_name(table: Table | str, label: str) -> str:
    """CHECK 约束名的完整形式：``ck_<table>_<label>``。

    **传给 ``CheckConstraint(name=...)`` 的应当是 ``label`` 本身，不是本函数的返回值。**
    命名约定 ``ck_%(table_name)s_%(constraint_name)s`` 会自己补上前缀；
    把完整名字再传进去会得到 ``ck_users_ck_users_role_valid`` 这种双重前缀。

    本函数用于**断言**与文档核对，以及需要完整名字的运维/测试场景。
    """
    return f"ck_{_table_name(table)}_{label}"

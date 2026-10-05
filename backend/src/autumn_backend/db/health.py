"""数据库连通性与契约自检。

A1 的验收工具：确认 DSN 可解析、服务器可达、版本满足要求，并报告
pgvector 是否可用（A5/E6 的向量检索依赖它，缺失时提前暴露而不是等到那时）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autumn_backend.db.session import create_engine, create_session_factory

# `knowledge_indexes` 的向量列需要 pgvector；A5 起成为硬依赖。
REQUIRED_EXTENSION = "vector"

_SERVER_INFO_SQL = text(
    """
    SELECT current_database() AS database,
           current_user       AS db_user,
           version()          AS version,
           current_setting('TimeZone') AS session_timezone
    """
)

_EXTENSION_SQL = text("SELECT extname FROM pg_extension WHERE extname = :name")

_AVAILABLE_EXTENSION_SQL = text("SELECT 1 FROM pg_available_extensions WHERE name = :name")


async def collect_database_info(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    """收集连接级事实；纯只读，不修改任何对象。"""
    async with session_factory() as session:
        row = (await session.execute(_SERVER_INFO_SQL)).mappings().one()
        installed = (
            await session.execute(_EXTENSION_SQL, {"name": REQUIRED_EXTENSION})
        ).scalar_one_or_none()
        available = (
            await session.execute(_AVAILABLE_EXTENSION_SQL, {"name": REQUIRED_EXTENSION})
        ).scalar_one_or_none()

    return {
        "database": row["database"],
        "db_user": row["db_user"],
        "version": row["version"],
        "session_timezone": row["session_timezone"],
        "pgvector_installed": installed is not None,
        "pgvector_available": available is not None,
    }


async def check_connection() -> dict[str, Any]:
    """一次性连通性自检入口。"""
    engine = create_engine()
    try:
        return await collect_database_info(create_session_factory(engine))
    finally:
        await engine.dispose()


__all__ = ["REQUIRED_EXTENSION", "check_connection", "collect_database_info"]

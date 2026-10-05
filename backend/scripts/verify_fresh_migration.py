"""开发期验收辅助：在**全新空库**上验证迁移链。

实施顺序的阶段 A 完成判据要求「空库 base→head 升级成功」，而不是只在
已经被反复升降级的开发库上通过。本脚本：

1. 创建（或重建）一个专用验收库；
2. 显式启用 ``vector`` 扩展（pgvector 是环境预置条件，不是迁移的职责）；
3. 从 ``base`` 升到 ``head``；
4. 打印每个 revision 与最终表数。

它只做迁移与只读统计，不写入业务数据。用完请自行决定是否保留该库。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg

from autumn_backend.config import get_settings

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _admin_dsn(url: str) -> str:
    parts = urlsplit(url.replace("+asyncpg", ""))
    return urlunsplit(("postgresql", parts.netloc, "/postgres", "", ""))


def _target_dsn(url: str, database: str) -> str:
    parts = urlsplit(url.replace("+asyncpg", ""))
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 5432
    return urlunsplit(
        ("postgresql", f"{parts.username}:{parts.password}@{host}:{port}", f"/{database}", "", "")
    )


async def _recreate_database(dsn_url: str, database: str) -> None:
    admin = await asyncpg.connect(_admin_dsn(dsn_url))
    try:
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            database,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
        await admin.execute(f'CREATE DATABASE "{database}"')
    finally:
        await admin.close()

    connection = await asyncpg.connect(_target_dsn(dsn_url, database))
    try:
        available = await connection.fetchval(
            "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"
        )
        if not available:
            print("错误：服务器上没有 vector 扩展（pgvector 是环境预置条件）。", file=sys.stderr)
            raise SystemExit(1)
        await connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
    finally:
        await connection.close()


def _alembic(args: list[str], database_url: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_ROOT,
        env={**os.environ, "AUTUMN_DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        raise SystemExit(f"alembic {' '.join(args)} 失败（退出码 {result.returncode}）")
    for line in result.stderr.splitlines():
        if "Running " in line:
            print("   ", line.strip())


async def _summary(database_url: str, database: str) -> None:
    connection = await asyncpg.connect(_target_dsn(database_url, database))
    try:
        tables = await connection.fetchval(
            "SELECT count(*) FROM pg_tables WHERE schemaname='public' "
            "AND tablename <> 'alembic_version'"
        )
        checks = await connection.fetchval(
            "SELECT count(*) FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname='public' AND con.contype::text = 'c'"
        )
        indexes = await connection.fetchval(
            "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname='public'"
        )
        triggers = await connection.fetchval(
            "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname='public' AND NOT t.tgisinternal"
        )
        epoch = await connection.fetchval(
            "SELECT value FROM settings WHERE key = 'content_acl_epoch'"
        )
        print(f"应用表={tables} CHECK={checks} 索引={indexes} 触发器={triggers}")
        print(f"content_acl_epoch={epoch!r}")
    finally:
        await connection.close()


def main() -> int:
    settings = get_settings()
    database = "autumn_fresh"
    url = settings.async_database_url
    fresh_url = url.rsplit("/", 1)[0] + f"/{database}"

    print(f"在 {database} 上空库重建并执行 base -> head")
    asyncio.run(_recreate_database(url, database))
    _alembic(["upgrade", "head"], fresh_url)
    _alembic(["check"], fresh_url)
    asyncio.run(_summary(url, database))
    print(f"完成。如不再需要：DROP DATABASE {database};")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

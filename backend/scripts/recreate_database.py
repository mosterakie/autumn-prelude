"""重建一个空数据库并启用 pgvector。

用途：

- 阶段闸门（``scripts/ci.ps1``）在跑迁移前重建**专用 CI 库**；
- 本地开发需要干净库时手工执行。

**危险操作**：会 DROP 目标数据库。因此默认**只允许**重建带 ``_ci`` 后缀、
或显式用 ``--force`` 确认的名字，避免误伤 ``autumn`` 里的开发数据。
连接参数从 ``AUTUMN_DATABASE_URL`` 推导（只换库名，不动主机与凭据）。

用法::

    python scripts/recreate_database.py autumn_ci
    python scripts/recreate_database.py autumn_fresh
    python scripts/recreate_database.py autumn --force
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg

_DEFAULT_URL = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5442/autumn"

#: 不需要 ``--force`` 就允许重建的库名后缀。
_SAFE_SUFFIXES = ("_ci", "_fresh", "_test")


def _split_url() -> tuple[str, str]:
    """把 SQLAlchemy 风格 URL 拆成 (服务器前缀, 当前库名)。"""
    url = os.environ.get("AUTUMN_DATABASE_URL", _DEFAULT_URL)
    plain = url.replace("+asyncpg", "")
    prefix, _, database = plain.rpartition("/")
    if not prefix or not database:
        raise SystemExit(f"无法从 AUTUMN_DATABASE_URL 解析库名：{url}")
    return prefix, database


async def _recreate(prefix: str, database: str) -> None:
    netloc = prefix.split("://", 1)[1]
    admin = await asyncpg.connect(f"postgresql://{netloc}/postgres")
    try:
        # 先踢掉连接，否则 DROP DATABASE 会失败。
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            database,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
        await admin.execute(f'CREATE DATABASE "{database}"')
    finally:
        await admin.close()

    connection = await asyncpg.connect(f"postgresql://{netloc}/{database}")
    try:
        available = await connection.fetchval(
            "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"
        )
        if not available:
            raise SystemExit("服务器上没有 pgvector：请使用 pgvector 官方镜像")
        await connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
    finally:
        await connection.close()
    print(f"数据库 {database} 已重建并启用 vector 扩展")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重建空数据库并启用 pgvector")
    parser.add_argument("database", nargs="?", help="目标库名；默认沿用 AUTUMN_DATABASE_URL")
    parser.add_argument(
        "--force",
        action="store_true",
        help=f"允许重建不在安全后缀 {_SAFE_SUFFIXES} 之内的库名",
    )
    args = parser.parse_args(argv)

    prefix, current = _split_url()
    database = args.database or current
    if not args.force and not database.endswith(_SAFE_SUFFIXES):
        print(
            f"拒绝重建 {database}：它看起来是开发/生产库。若确实要重建，请显式加 --force。",
            file=sys.stderr,
        )
        return 2

    asyncio.run(_recreate(prefix, database))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

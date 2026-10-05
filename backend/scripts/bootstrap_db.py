"""开发期数据库引导脚本：创建应用库与扩展。

只做幂等操作，可在任意环境重复执行::

    python scripts/bootstrap_db.py

它会：
1. 连接 ``AUTUMN_DATABASE_URL`` 指向的服务器（默认库 ``postgres``）；
2. 若应用库不存在则创建；
3. 在应用库内创建 ``vector`` 扩展（A5/E6 的向量检索依赖）。

不写任何业务数据，不做迁移——迁移一律由 ``alembic`` 负责。
"""

from __future__ import annotations

import asyncio
import sys
from urllib.parse import urlsplit, urlunsplit

import asyncpg

from autumn_backend.config import get_settings


def _split(url: str) -> tuple[str, str, str, str]:
    """把 SQLAlchemy 异步 URL 拆成 asyncpg 可直接使用的主机/库信息。"""
    parts = urlsplit(url.replace("+asyncpg", ""))
    return (
        parts.hostname or "127.0.0.1",
        str(parts.port or 5432),
        (parts.path or "/postgres").lstrip("/"),
        urlunsplit(("postgresql", parts.netloc, "/postgres", "", "")),
    )


async def bootstrap() -> int:
    settings = get_settings()
    host, port, database, admin_dsn = _split(settings.async_database_url)
    print(f"目标服务器：{host}:{port}；应用库：{database}")

    admin = await asyncpg.connect(admin_dsn)
    try:
        exists = await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", database)
        if exists:
            print(f"数据库 {database} 已存在，跳过创建。")
        else:
            # 库名来自本地配置，不是用户输入；仍做标识符转义以防特殊字符。
            quoted = '"' + database.replace('"', '""') + '"'
            await admin.execute(f"CREATE DATABASE {quoted}")
            print(f"已创建数据库 {database}。")
    finally:
        await admin.close()

    target_dsn = urlunsplit(
        ("postgresql", f"postgres:postgres@{host}:{port}", f"/{database}", "", "")
    )
    connection = await asyncpg.connect(target_dsn)
    try:
        extension = await connection.fetchval(
            "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"
        )
        if not extension:
            print("错误：服务器上不存在 vector 扩展，请使用 pgvector 镜像。", file=sys.stderr)
            return 1
        await connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
        installed = await connection.fetchval(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        )
        print(f"vector 扩展已就绪：{installed}")

        server = await connection.fetchrow(
            "SELECT current_database() AS db, current_user AS usr, "
            "current_setting('TimeZone') AS tz, version() AS ver"
        )
        print(f"库={server['db']} 用户={server['usr']} 时区={server['tz']}")
        print(server["ver"].split(",")[0])
    finally:
        await connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(bootstrap()))

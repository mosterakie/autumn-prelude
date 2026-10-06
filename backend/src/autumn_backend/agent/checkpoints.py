"""官方 PostgreSQL saver，显式初始化独立 schema，只保存图调度元数据。"""

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from sqlalchemy.engine import make_url

from autumn_backend.io_boundary import require_outside_uow


@asynccontextmanager
async def postgres_saver(
    database_url: str, *, schema: str = "autumn_checkpoints", initialize: bool = False
) -> AsyncIterator[AsyncPostgresSaver]:
    require_outside_uow()
    if not re.fullmatch(r"autumn_checkpoints(?:_[a-z0-9_]+)?", schema):
        raise ValueError("检查点必须位于专用 schema")
    connection_url = make_url(database_url).set(drivername="postgresql")
    async with await AsyncConnection.connect(
        connection_url.render_as_string(hide_password=False),
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
    ) as connection:
        if initialize:
            # setup 包含并发索引创建，不能包在业务 UoW / 普通事务内。
            await connection.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema))
            )
        await connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        saver = AsyncPostgresSaver(connection)
        if initialize:
            await saver.setup()
        else:
            row = await (
                await connection.execute("SELECT max(v) AS v FROM checkpoint_migrations")
            ).fetchone()
            if row is None or row["v"] != len(saver.MIGRATIONS) - 1:
                raise RuntimeError("请先显式初始化当前版本的 PostgreSQL 检查点")
        yield saver

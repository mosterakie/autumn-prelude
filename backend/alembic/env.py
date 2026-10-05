"""Alembic 运行环境。

约定：
- URL 只来自 ``AUTUMN_DATABASE_URL``（``alembic/env.py`` 不读仓库里的凭据）。
- 生成迁移时导入全部模型，保证 ``alembic revision --autogenerate`` 与
  ``alembic check`` 能看到完整元数据。
- 数据库时间统一 UTC：迁移连接同样设置 ``timezone=UTC``。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from autumn_backend.config import get_settings
from autumn_backend.db import external_tables
from autumn_backend.db.base import Base
from autumn_backend.db.models import load_all_models

# Alembic 配置对象与日志配置。
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# 目标元数据：全部模型的统一 Metadata。
# 注册入口是 ``autumn_backend.db.models`` 包的导入副作用，见该包的说明。
load_all_models()
target_metadata = Base.metadata

# URL 只从应用配置注入（alembic.ini 中的 sqlalchemy.url 保持为空）。
_settings = get_settings()
config.set_main_option("sqlalchemy.url", _settings.async_database_url)


#: 允许存在于数据库中但不属于应用模型的外部表，按 ``(schema, table)`` 登记。
#: 定义放在应用包里，迁移运行器与 catalog 核对测试引用同一份政策。
EXTERNAL_TABLE_ALLOWLIST = external_tables.EXTERNAL_TABLE_ALLOWLIST


def _include_object(
    obj: object,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: object | None,
) -> bool:
    """只排除**显式登记**的外部表；其余全部纳入比较。

    注意：``CHECK`` 约束表达式与触发器**不在** autogenerate 的比较范围内
    （见 Alembic 文档 "What does autogenerate detect"）。因此本函数只保证
    "表/列/索引/唯一约束/外键"这一层不漂移；CHECK 语义与触发器是否生效，
    必须靠集成测试与 catalog 核对另行证明。
    """
    if type_ == "table" and reflected and compare_to is None:
        schema = getattr(obj, "schema", None) or "public"
        return (schema, name or "") in EXTERNAL_TABLE_ALLOWLIST
    return True


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连接数据库。"""
    context.configure(
        url=_settings.sync_database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
        include_schemas=False,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
        include_schemas=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """在线模式：异步引擎连接真实 PostgreSQL。"""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={"server_settings": {"timezone": "UTC"}},
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

"""后端运维 CLI。

用法::

    python -m autumn_backend.cli check-db     # 数据库连通性与 pgvector 可用性自检
    python -m autumn_backend.cli config       # 打印生效配置（自动脱敏）

只做只读检查；迁移一律通过 ``alembic`` 命令执行。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from autumn_backend.config import Environment, Settings, get_settings
from autumn_backend.db.health import check_connection


def _config_report(settings: Settings) -> dict[str, Any]:
    """输出可安全打印的配置快照：不含任何密钥或完整 DSN。"""
    return {
        "environment": settings.environment.value,
        "debug": settings.debug,
        "quota_timezone": settings.quota_timezone,
        "cookie_name": settings.cookie_name,
        "cookie_secure": settings.cookie_secure,
        "log_level": settings.log_level,
        "log_json": settings.log_json,
        "database_url_configured": bool(settings.async_database_url),
        "storage_root": str(settings.storage_root),
    }


async def _check_db() -> int:
    try:
        info = await check_connection()
    except Exception as error:
        print(f"数据库连接失败：{type(error).__name__}: {error}", file=sys.stderr)
        return 1

    print(json.dumps(info, ensure_ascii=False, indent=2))
    if not info["pgvector_installed"]:
        hint = "可用但未安装" if info["pgvector_available"] else "在服务器上不可用"
        print(f"警告：pgvector 扩展{hint}；阶段 A5/E6 的向量检索需要它。", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autumn_backend.cli", description="秋序后端运维 CLI")
    parser.add_argument(
        "command",
        choices=["check-db", "config"],
        help="check-db：数据库自检；config：打印生效配置",
    )
    args = parser.parse_args(argv)

    settings = get_settings()

    if args.command == "config":
        print(json.dumps(_config_report(settings), ensure_ascii=False, indent=2))
        return 0

    if settings.environment is Environment.LOCAL and not settings.async_database_url:
        print("未配置 AUTUMN_DATABASE_URL。", file=sys.stderr)
        return 1
    return asyncio.run(_check_db())


if __name__ == "__main__":
    raise SystemExit(main())

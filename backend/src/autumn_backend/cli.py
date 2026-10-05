"""后端运维 CLI。

用法::

    python -m autumn_backend.cli check-db     # 数据库连通性与 pgvector 可用性自检
    python -m autumn_backend.cli config       # 打印生效配置（自动脱敏）

check-db/config 只读；bootstrap-owner 交互式创建首位站长并绑定 TOTP。
迁移一律通过 ``alembic`` 命令执行。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from getpass import getpass
from typing import Any
from urllib.parse import quote

from autumn_backend.auth.service import AuthService
from autumn_backend.config import Environment, Settings, get_settings
from autumn_backend.db.health import check_connection
from autumn_backend.db.session import UnitOfWorkFactory, create_engine, create_session_factory


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


async def _bootstrap_owner(settings: Settings) -> int:
    """受控本地交互引导；秘密只显示在发起引导的终端，不写日志/文件。"""
    email = (await asyncio.to_thread(input, "站长邮箱：")).strip()
    display_name = (await asyncio.to_thread(input, "显示名称：")).strip()
    password = await asyncio.to_thread(getpass, "密码（至少 12 字符）：")
    if password != await asyncio.to_thread(getpass, "再次输入密码："):
        print("两次密码不同，未创建账号。", file=sys.stderr)
        return 1
    secret = AuthService.new_totp_secret()
    print(
        f"请在验证器中导入：otpauth://totp/{quote('Autumn Prelude:' + email)}?secret={secret}&issuer=Autumn%20Prelude"
    )
    code = await asyncio.to_thread(getpass, "验证器当前 6 位代码：")
    engine = create_engine(settings)
    try:
        service = AuthService(UnitOfWorkFactory(create_session_factory(engine)), settings)
        codes = await service.bootstrap_owner(
            email=email, password=password, display_name=display_name, totp_secret=secret, code=code
        )
    except Exception as error:
        print(f"引导失败：{type(error).__name__}。未完成的创建已回滚。", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    print("站长已创建。请单独保存下列一次性恢复码（仅显示一次）：")
    for value in codes:
        print(value)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autumn_backend.cli", description="秋序后端运维 CLI")
    parser.add_argument(
        "command",
        choices=["check-db", "config", "bootstrap-owner"],
        help="check-db：数据库自检；config：打印生效配置",
    )
    args = parser.parse_args(argv)

    settings = get_settings()

    if args.command == "config":
        print(json.dumps(_config_report(settings), ensure_ascii=False, indent=2))
        return 0
    if args.command == "bootstrap-owner":
        return asyncio.run(_bootstrap_owner(settings))

    if settings.environment is Environment.LOCAL and not settings.async_database_url:
        print("未配置 AUTUMN_DATABASE_URL。", file=sys.stderr)
        return 1
    return asyncio.run(_check_db())


if __name__ == "__main__":
    raise SystemExit(main())

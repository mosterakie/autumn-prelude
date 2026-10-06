"""独立 Worker 入口；工厂只由可信本机启动参数指定。"""

import argparse
import asyncio
import importlib
import selectors
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import cast

from autumn_backend.agent.checkpoints import postgres_saver
from autumn_backend.config import get_settings
from autumn_backend.workers.core import Worker

Factory = Callable[[], AbstractAsyncContextManager[Worker]]


def loop_factory() -> asyncio.AbstractEventLoop:
    # psycopg 异步连接在 Windows 需要 Selector；只影响独立 Worker 进程。
    return asyncio.SelectorEventLoop(selectors.DefaultSelector())


async def start(factory: Factory, *, once: bool) -> None:
    async with factory() as worker:
        if once:
            await worker.run_once()
        else:
            await worker.run(asyncio.Event())


async def setup_checkpoints() -> None:
    async with postgres_saver(get_settings().async_database_url, initialize=True):
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="秋序后台任务执行器")
    parser.add_argument("--once", action="store_true", help="最多执行一个任务后退出")
    parser.add_argument("--setup-checkpoints", action="store_true", help="显式初始化框架专用表")
    parser.add_argument("--factory", help="可信本地装配入口 module:factory")
    args = parser.parse_args()
    if args.setup_checkpoints:
        asyncio.run(setup_checkpoints(), loop_factory=loop_factory)
        return
    if not args.factory:
        parser.error("执行任务需要配置可信 --factory")
    module, name = args.factory.split(":", 1)
    factory = cast(Factory, getattr(importlib.import_module(module), name))
    try:
        asyncio.run(start(factory, once=args.once), loop_factory=loop_factory)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

"""独立 Worker 入口；工厂只由可信本机启动参数指定。"""

import argparse
import asyncio
import importlib
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import cast

from autumn_backend.workers.core import Worker

Factory = Callable[[], AbstractAsyncContextManager[Worker]]


async def start(factory: Factory, *, once: bool) -> None:
    async with factory() as worker:
        if once:
            await worker.run_once()
        else:
            await worker.run(asyncio.Event())


def main() -> None:
    parser = argparse.ArgumentParser(description="秋序后台任务执行器")
    parser.add_argument("--once", action="store_true", help="最多执行一个任务后退出")
    parser.add_argument("--factory", required=True, help="可信本地装配入口 module:factory")
    args = parser.parse_args()
    module, name = args.factory.split(":", 1)
    factory = cast(Factory, getattr(importlib.import_module(module), name))
    try:
        asyncio.run(start(factory, once=args.once))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

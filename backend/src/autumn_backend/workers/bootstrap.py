"""默认运行已配置的任务；真实模型/Embedding 由可信工厂注入。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from autumn_backend.config import get_settings
from autumn_backend.db.session import UnitOfWorkFactory, create_engine, create_session_factory
from autumn_backend.jobs.queue import Queue
from autumn_backend.services.storage import StorageService
from autumn_backend.services.tasks import TaskService
from autumn_backend.storage.local import LocalObjectStore
from autumn_backend.workers.core import Worker
from autumn_backend.workers.handlers import storage_handlers
from autumn_backend.workers.registry import Registry


@asynccontextmanager
async def worker() -> AsyncIterator[Worker]:
    settings = get_settings()
    engine = create_engine(settings)
    try:
        uows = UnitOfWorkFactory(create_session_factory(engine))
        tasks = TaskService(uows)
        storage = StorageService(uows, LocalObjectStore(settings.storage_root))
        yield Worker(Queue(uows), Registry(storage_handlers(storage, tasks)), tasks.failure)
    finally:
        await engine.dispose()

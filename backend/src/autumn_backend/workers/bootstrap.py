"""默认运行已配置的任务；真实模型/Embedding 由可信工厂注入。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from autumn_backend.agent.contracts import ModelDriver
from autumn_backend.config import get_settings
from autumn_backend.db.session import UnitOfWorkFactory, create_engine, create_session_factory
from autumn_backend.jobs.queue import Queue
from autumn_backend.services.action_execution import ActionExecutionService
from autumn_backend.services.knowledge import KnowledgeService
from autumn_backend.services.storage import StorageService
from autumn_backend.services.tasks import TaskService
from autumn_backend.storage.local import LocalObjectStore
from autumn_backend.workers.core import Worker
from autumn_backend.workers.handlers import (
    agent_runtime,
    cleanup_handlers,
    knowledge_handlers,
    run_handlers,
    storage_handlers,
)
from autumn_backend.workers.registry import Registry


def configured_worker(
    uows: UnitOfWorkFactory,
    storage: StorageService,
    *,
    knowledge: KnowledgeService | None = None,
    model: ModelDriver | None = None,
    saver: BaseCheckpointSaver[Any] | None = None,
) -> Worker:
    tasks = TaskService(uows)
    handlers = {**storage_handlers(storage, tasks), **cleanup_handlers(uows)}
    handlers["action.execute"] = ActionExecutionService(uows).execute
    if knowledge is not None:
        handlers.update(knowledge_handlers(knowledge, tasks))
    if model is not None:
        if knowledge is None or saver is None:
            raise ValueError("运行任务必须配置知识服务和持久检查点")
        handlers.update(run_handlers(agent_runtime(uows, knowledge, model, saver)))
    return Worker(Queue(uows), Registry(handlers), tasks.failure)


@asynccontextmanager
async def worker() -> AsyncIterator[Worker]:
    settings = get_settings()
    engine = create_engine(settings)
    try:
        uows = UnitOfWorkFactory(create_session_factory(engine))
        storage = StorageService(uows, LocalObjectStore(settings.storage_root))
        yield configured_worker(uows, storage)
    finally:
        await engine.dispose()

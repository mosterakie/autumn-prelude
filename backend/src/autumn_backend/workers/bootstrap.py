"""默认运行已配置的任务；真实模型/Embedding 由可信工厂注入。"""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from autumn_backend.agent.checkpoints import postgres_saver
from autumn_backend.agent.contracts import ModelDriver
from autumn_backend.config import get_settings
from autumn_backend.db.session import UnitOfWorkFactory, create_engine, create_session_factory
from autumn_backend.jobs.queue import LeasedJob, Queue
from autumn_backend.providers.configured import providers
from autumn_backend.services.action_execution import ActionExecutionService
from autumn_backend.services.auth_email import AuthEmailService
from autumn_backend.services.conversation_cleanup import ConversationCleanupService
from autumn_backend.services.knowledge import KnowledgeService
from autumn_backend.services.knowledge_imports import KnowledgeImportService
from autumn_backend.services.maintenance import MaintenanceService
from autumn_backend.services.run_cancellation import RunCancellationService
from autumn_backend.services.storage import StorageService
from autumn_backend.services.tasks import TaskService
from autumn_backend.services.web_search import WebSearchService
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
    web_search: WebSearchService | None = None,
    email: AuthEmailService | None = None,
) -> Worker:
    tasks = TaskService(uows)
    handlers = {**storage_handlers(storage, tasks), **cleanup_handlers(uows)}
    imports = KnowledgeImportService(uows, storage, knowledge)

    async def bookmark(job: LeasedJob) -> None:
        await imports.execute(await tasks.actor(job), job.id, job.token)

    handlers["knowledge.bookmark"] = bookmark
    handlers["action.execute"] = ActionExecutionService(uows).execute
    handlers["conversation.cleanup"] = ConversationCleanupService(uows).execute
    if email is not None:
        handlers["auth.email"] = email.execute

    async def failure(job: LeasedJob, error: Exception) -> None:
        if job.kind == "auth.email" and email is not None:
            await email.failure(job, error)
        else:
            await tasks.failure(job, error)

    async def cancel(provider: str, name: str, key: str) -> None:
        if model is not None and (provider, name) == (model.provider, model.model):
            await model.cancel(external_idempotency_key=key)

    handlers["run.cancel"] = RunCancellationService(
        uows, cancel if model is not None else None
    ).execute
    if knowledge is not None:
        handlers.update(knowledge_handlers(knowledge, tasks))
    if model is not None:
        if knowledge is None or saver is None:
            raise ValueError("运行任务必须配置知识服务和持久检查点")
        handlers.update(
            run_handlers(agent_runtime(uows, knowledge, model, saver, web_search=web_search))
        )
    return Worker(
        Queue(uows), Registry(handlers), failure, maintenance=MaintenanceService(uows).tick
    )


@asynccontextmanager
async def worker() -> AsyncIterator[Worker]:
    settings = get_settings()
    engine = create_engine(settings)
    try:
        uows = UnitOfWorkFactory(create_session_factory(engine))
        storage = StorageService(uows, LocalObjectStore(settings.storage_root))
        async with AsyncExitStack() as stack:
            configured = await stack.enter_async_context(providers(settings))
            knowledge = (
                KnowledgeService(uows, storage, configured.embedder)
                if configured.embedder
                else None
            )
            web_search = WebSearchService(uows, configured.search) if configured.search else None
            saver = None
            if configured.model is not None:
                if knowledge is None:
                    raise ValueError("启用模型前必须配置嵌入服务")
                saver = await stack.enter_async_context(
                    postgres_saver(
                        settings.async_database_url,
                        schema=settings.checkpoint_schema,
                    )
                )
            yield configured_worker(
                uows,
                storage,
                knowledge=knowledge,
                model=configured.model,
                saver=saver,
                web_search=web_search,
                email=AuthEmailService(uows, settings, configured.mailer)
                if configured.mailer
                else None,
            )
    finally:
        await engine.dispose()

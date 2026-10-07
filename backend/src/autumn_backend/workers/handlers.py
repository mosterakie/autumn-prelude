"""允许 workers → agent/services；不向队列层反向注册业务依赖。"""

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from autumn_backend.agent.context import ContextLoader
from autumn_backend.agent.contracts import ModelDriver
from autumn_backend.agent.runtime import AgentRuntime
from autumn_backend.agent.tools import Tools
from autumn_backend.db.session import UnitOfWorkFactory
from autumn_backend.jobs.queue import LeasedJob
from autumn_backend.services.actions import ActionService
from autumn_backend.services.execution import ExecutionService
from autumn_backend.services.index_cleanup import IndexCleanupService
from autumn_backend.services.input_waits import InputWaitService
from autumn_backend.services.knowledge import KnowledgeService
from autumn_backend.services.knowledge_imports import KnowledgeImportService
from autumn_backend.services.runtime import RuntimeService
from autumn_backend.services.storage import StorageService
from autumn_backend.services.tasks import TaskService
from autumn_backend.services.web_search import WebSearchService
from autumn_backend.workers.registry import Handler


def run_handlers(runtime: AgentRuntime) -> dict[str, Handler]:
    async def dispatch(job: LeasedJob) -> None:
        await runtime.execute(job.id, job.token)

    return {"run.dispatch": dispatch, "run.resume": dispatch}


def storage_handlers(storage: StorageService, tasks: TaskService) -> dict[str, Handler]:
    async def finalize(job: LeasedJob) -> None:
        await storage.finalize(await tasks.actor(job), job.id, job.token)

    async def delete(job: LeasedJob) -> None:
        await storage.run_delete(await tasks.actor(job), job.id, job.token)

    return {"storage.finalize": finalize, "storage.delete": delete}


def knowledge_handlers(knowledge: KnowledgeService, tasks: TaskService) -> dict[str, Handler]:
    imports = KnowledgeImportService(knowledge._uows, knowledge.storage, knowledge)

    async def index(job: LeasedJob) -> None:
        await imports.execute(await tasks.actor(job), job.id, job.token)

    async def publication(job: LeasedJob) -> None:
        await knowledge.sync_publication(await tasks.actor(job), job.id, job.token)

    return {"knowledge.ingest": index, "knowledge.publication_sync": publication}


def cleanup_handlers(uows: UnitOfWorkFactory) -> dict[str, Handler]:
    return {"knowledge.cleanup": IndexCleanupService(uows).execute}


def agent_runtime(
    uows: UnitOfWorkFactory,
    knowledge: KnowledgeService,
    model: ModelDriver,
    saver: BaseCheckpointSaver[Any],
    *,
    web_search: WebSearchService | None = None,
) -> AgentRuntime:
    service = RuntimeService(uows)
    return AgentRuntime(
        service,
        ContextLoader(service, knowledge),
        Tools(service, knowledge, ActionService(uows), web_search=web_search),
        ExecutionService(uows),
        InputWaitService(uows),
        model,
        checkpointer=saver,
    )

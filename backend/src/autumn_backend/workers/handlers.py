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
from autumn_backend.services.input_waits import InputWaitService
from autumn_backend.services.knowledge import KnowledgeService
from autumn_backend.services.runtime import RuntimeService
from autumn_backend.workers.registry import Handler


def run_handlers(runtime: AgentRuntime) -> dict[str, Handler]:
    async def dispatch(job: LeasedJob) -> None:
        await runtime.execute(job.id, job.token)

    return {"run.dispatch": dispatch, "run.resume": dispatch}


def agent_runtime(
    uows: UnitOfWorkFactory,
    knowledge: KnowledgeService,
    model: ModelDriver,
    saver: BaseCheckpointSaver[Any],
) -> AgentRuntime:
    service = RuntimeService(uows)
    return AgentRuntime(
        service,
        ContextLoader(service, knowledge),
        Tools(service, knowledge, ActionService(uows)),
        ExecutionService(uows),
        InputWaitService(uows),
        model,
        checkpointer=saver,
    )

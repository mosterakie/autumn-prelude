"""每次进入图从当前业务数据库装配文本，不能从框架 checkpoint 提取正文。"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from autumn_backend.errors import ConflictError, LeaseLostError, NotFoundError, OptimisticLockError
from autumn_backend.services.context import TaskFence
from autumn_backend.services.knowledge import Citation, KnowledgeService
from autumn_backend.services.runtime import RuntimeService, RuntimeTicket


@dataclass(frozen=True, slots=True)
class Context:
    history: tuple[str, ...]
    sources: tuple[Citation, ...]
    actions: tuple[dict[str, Any], ...] = ()


class ContextLoader:
    def __init__(self, runtime: RuntimeService, knowledge: KnowledgeService) -> None:
        self.runtime, self.knowledge = runtime, knowledge

    async def load(self, ticket: RuntimeTicket) -> Context:
        await self.runtime.guard(ticket, context=False)
        fence = TaskFence(ticket.job_id, ticket.token, ticket.generation)
        valid: list[UUID] = []
        for identifier in ticket.history_ids:
            try:
                await self.knowledge.capture_dependencies(
                    ticket.actor, ticket.run_id, message_ids=(identifier,), fence=fence
                )
            except (LeaseLostError, OptimisticLockError):
                raise
            except (ConflictError, NotFoundError):
                # 丢弃无权或缺少完整来源的历史，不先读取正文再让模型自行判断权限。
                continue
            valid.append(identifier)
        history = await self.knowledge.capture_dependencies(
            ticket.actor, ticket.run_id, message_ids=tuple(valid), fence=fence
        )
        sources = await self.knowledge.current_sources(ticket.actor, ticket.run_id, fence=fence)
        await self.runtime.guard(ticket)
        return Context(history, sources, await self.runtime.completed_actions(ticket))

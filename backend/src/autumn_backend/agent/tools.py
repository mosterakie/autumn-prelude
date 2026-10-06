"""两个业务参数工具；身份、Run、授权消息与任务资格只由服务端绑定。"""

import json
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from autumn_backend.errors import InvalidInputError
from autumn_backend.policies import ActorRole
from autumn_backend.services.actions import ActionService, Command
from autumn_backend.services.context import TaskFence
from autumn_backend.services.knowledge import Citation, KnowledgeService
from autumn_backend.services.runtime import RuntimeService, RuntimeTicket


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Search(Input):
    query: str = Field(min_length=1, max_length=8000)
    limit: int = Field(default=5, ge=1, le=10)


class Proposal(Input):
    command: Command = Field(discriminator="target_type")


class ToolCall(Input):
    name: Literal["search_knowledge", "propose_action"]
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResult:
    sources: tuple[Citation, ...] = ()
    action_id: UUID | None = None


class Tools:
    def __init__(
        self, runtime: RuntimeService, knowledge: KnowledgeService, actions: ActionService
    ) -> None:
        self.runtime, self.knowledge, self.actions = runtime, knowledge, actions

    def schemas(self, ticket: RuntimeTicket) -> tuple[dict[str, Any], ...]:
        result = [
            {
                "name": "search_knowledge",
                "description": "检索当前模式与资源范围允许的站内资料。",
                "parameters": Search.model_json_schema(),
            }
        ]
        if ticket.actor.role is ActorRole.OWNER and ticket.actor.step_up_expires_at is not None:
            result.append(
                {
                    "name": "propose_action",
                    "description": "生成待用户确认的固定操作预览，不直接执行。",
                    "parameters": Proposal.model_json_schema(),
                }
            )
        return tuple(result)

    async def execute(self, ticket: RuntimeTicket, call: ToolCall, *, step: int) -> ToolResult:
        if step < 1:
            raise InvalidInputError("工具步骤无效")
        await self.runtime.guard(ticket)
        await self.runtime.reserve(ticket, kind="tool")
        fence = TaskFence(ticket.job_id, ticket.token, ticket.generation)
        try:
            if call.name == "search_knowledge":
                search = Search.model_validate_json(json.dumps(call.arguments))
                result = await self.knowledge.retrieve(
                    ticket.actor, ticket.run_id, search.query, limit=search.limit, fence=fence
                )
                await self.runtime.guard(ticket)
                return ToolResult(sources=result)
            proposal = Proposal.model_validate_json(json.dumps(call.arguments))
        except ValidationError as error:
            raise InvalidInputError("工具参数不属于允许的业务 schema") from error
        action = await self.actions.preview(
            ticket.actor,
            proposal.command,
            idempotency_key=f"agent:{ticket.run_id}:{ticket.generation}:tool:{step}",
            run_id=ticket.run_id,
            authorization_message_id=ticket.request_id,
            fence=fence,
        )
        return ToolResult(action_id=action.id)

"""薄业务工具；身份、Run、授权消息与任务资格只由服务端绑定。"""

import json
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from autumn_backend.errors import InvalidInputError
from autumn_backend.policies import ActorRole
from autumn_backend.services.actions import ActionService, Command, ResourceCreatePreview
from autumn_backend.services.context import TaskFence
from autumn_backend.services.knowledge import Citation, KnowledgeService
from autumn_backend.services.resources import CreateResource
from autumn_backend.services.runtime import RuntimeService, RuntimeTicket
from autumn_backend.services.web_search import WebSearchService


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Search(Input):
    query: str = Field(min_length=1, max_length=8000)
    limit: int = Field(default=5, ge=1, le=10)


class Proposal(Input):
    command: Command = Field(discriminator="target_type")


class WebSearch(Input):
    query: str = Field(min_length=1, max_length=8000)
    limit: int = Field(default=3, ge=1, le=5)


class Bookmark(Input):
    url: str = Field(min_length=1, max_length=2048)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    private_note: str | None = Field(default=None, max_length=64000)
    tags: tuple[str, ...] = Field(default=(), max_length=20)


class Note(Input):
    title: str = Field(min_length=1, max_length=500)
    body_text: str = Field(min_length=1, max_length=200000)
    tags: tuple[str, ...] = Field(default=(), max_length=20)


class ToolCall(Input):
    name: Literal[
        "search_knowledge", "search_web", "propose_bookmark", "propose_note", "propose_action"
    ]
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResult:
    sources: tuple[Citation, ...] = ()
    action_id: UUID | None = None


class Tools:
    def __init__(
        self,
        runtime: RuntimeService,
        knowledge: KnowledgeService,
        actions: ActionService,
        *,
        web_search: WebSearchService | None = None,
    ) -> None:
        self.runtime, self.knowledge, self.actions = runtime, knowledge, actions
        self.web_search = web_search

    def schemas(self, ticket: RuntimeTicket) -> tuple[dict[str, Any], ...]:
        result = [
            {
                "name": "search_knowledge",
                "description": "检索当前模式与资源范围允许的站内资料。",
                "parameters": Search.model_json_schema(),
            }
        ]
        if (
            ticket.actor.role is ActorRole.OWNER
            and ticket.actor.step_up_expires_at is not None
            and ticket.mode == "owner"
        ):
            if (
                self.web_search is not None
                and ticket.mode == "owner"
                and ticket.search_mode in ("web", "auto")
            ):
                result.append(
                    {
                        "name": "search_web",
                        "description": "联网搜索并登记引用，只用于已验证站长的联网运行。",
                        "parameters": WebSearch.model_json_schema(),
                    }
                )
            result.append(
                {
                    "name": "propose_action",
                    "description": "生成待用户确认的固定操作预览，不直接执行。",
                    "parameters": Proposal.model_json_schema(),
                }
            )
            result.extend(
                [
                    {
                        "name": "propose_bookmark",
                        "description": "预览新增私人网址收藏，用户确认后保存。仅保存链接，不读取或下载网页；标题可省略。",
                        "parameters": Bookmark.model_json_schema(),
                    },
                    {
                        "name": "propose_note",
                        "description": "预览新建私人手记，用户确认后保存草稿；不会自动公开。",
                        "parameters": Note.model_json_schema(),
                    },
                ]
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
            if call.name == "search_web":
                search_web = WebSearch.model_validate_json(json.dumps(call.arguments))
                if self.web_search is None:
                    raise InvalidInputError("联网工具未配置")
                result = await self.web_search.search(
                    ticket.actor,
                    ticket.run_id,
                    search_web.query,
                    limit=search_web.limit,
                    fence=fence,
                    step=step,
                )
                await self.runtime.guard(ticket)
                return ToolResult(sources=result)
            command: Command
            if call.name == "propose_bookmark":
                bookmark = Bookmark.model_validate_json(json.dumps(call.arguments))
                command = ResourceCreatePreview(
                    resource=CreateResource(
                        kind="bookmark",
                        title=bookmark.title or urlsplit(bookmark.url).hostname or bookmark.url,
                        url=bookmark.url,
                        private_note=bookmark.private_note,
                        tags=bookmark.tags,
                    )
                )
            elif call.name == "propose_note":
                note = Note.model_validate_json(json.dumps(call.arguments))
                command = ResourceCreatePreview(
                    resource=CreateResource(
                        kind="article",
                        title=note.title,
                        body_text=note.body_text,
                        tags=note.tags,
                    )
                )
            else:
                command = Proposal.model_validate_json(json.dumps(call.arguments)).command
        except ValidationError as error:
            raise InvalidInputError("工具参数不属于允许的业务 schema") from error
        action = await self.actions.preview(
            ticket.actor,
            command,
            idempotency_key=f"agent:{ticket.run_id}:{ticket.generation}:tool:{step}",
            run_id=ticket.run_id,
            authorization_message_id=ticket.request_id,
            fence=fence,
        )
        return ToolResult(action_id=action.id)

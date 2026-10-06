"""本人会话与 Run 接口，受理与权限判断均在 services。"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse, Response

from autumn_backend.api.deps import authenticated_actor
from autumn_backend.api.responses import success
from autumn_backend.errors import ConfigurationError
from autumn_backend.policies.facts import SearchMode
from autumn_backend.services.chats import ChatService
from autumn_backend.services.runs import AcceptRunCommand, RunService

router = APIRouter(prefix="/api", tags=["chats"])
Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(max_length=512)]
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]


def service(request: Request) -> ChatService:
    value = getattr(request.app.state, "chats", None)
    if not isinstance(value, ChatService):
        raise ConfigurationError("会话服务未初始化")
    return value


class ChatInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateInput(ChatInput):
    mode: Literal["public", "owner"] = "public"
    title: str | None = Field(default=None, max_length=200)


class VersionInput(ChatInput):
    expected_version: int = Field(ge=0)


class RenameInput(VersionInput):
    title: str = Field(min_length=1, max_length=200)


class AskInput(ChatInput):
    conversation_id: UUID
    client_message_id: UUID
    message: str = Field(min_length=1, max_length=32000)
    resource_ids: list[UUID] = Field(default_factory=list, max_length=100)
    search_mode: Literal["auto", "site", "web"] = "site"


class ResumeInput(ChatInput):
    input_request_id: UUID | None = None
    answer: str | None = Field(default=None, min_length=1, max_length=8000)
    resume: bool = False


@router.post("/conversations", status_code=201)
async def create(request: Request, body: CreateInput) -> JSONResponse:
    return success(
        request,
        await service(request).create(
            authenticated_actor(request), mode=body.mode, title=body.title
        ),
        status=201,
    )


@router.get("/conversations")
async def conversations(
    request: Request,
    mode: Literal["public", "owner"] = "public",
    limit: Limit = 20,
    cursor: Cursor = None,
) -> JSONResponse:
    return success(
        request,
        await service(request).list(
            authenticated_actor(request), mode=mode, limit=limit, cursor=cursor
        ),
    )


@router.get("/conversations/{conversation_id}")
async def conversation(request: Request, conversation_id: UUID) -> JSONResponse:
    return success(
        request, await service(request).read(authenticated_actor(request), conversation_id)
    )


@router.get("/conversations/{conversation_id}/messages")
async def messages(
    request: Request, conversation_id: UUID, limit: Limit = 20, cursor: Cursor = None
) -> JSONResponse:
    return success(
        request,
        await service(request).messages(
            authenticated_actor(request), conversation_id, limit=limit, cursor=cursor
        ),
    )


@router.patch("/conversations/{conversation_id}")
async def rename(request: Request, conversation_id: UUID, body: RenameInput) -> JSONResponse:
    return success(
        request,
        await service(request).rename(
            authenticated_actor(request),
            conversation_id,
            version=body.expected_version,
            title=body.title,
        ),
    )


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete(request: Request, conversation_id: UUID, body: VersionInput) -> Response:
    await service(request).delete(
        authenticated_actor(request), conversation_id, version=body.expected_version
    )
    return Response(status_code=204)


@router.post("/ask", status_code=202)
async def ask(request: Request, body: AskInput, key: Key) -> JSONResponse:
    actor = authenticated_actor(request)
    runs = getattr(request.app.state, "runs", None)
    if not isinstance(runs, RunService):
        raise ConfigurationError("运行服务未初始化")
    command = AcceptRunCommand(
        conversation_id=body.conversation_id,
        client_message_id=body.client_message_id,
        idempotency_key=key,
        message=body.message,
        resource_ids=tuple(body.resource_ids),
        search_mode=SearchMode(body.search_mode),
    )
    return success(request, await runs.accept_run(actor, command), status=202)


@router.get("/runs/{run_id}")
async def run(request: Request, run_id: UUID) -> JSONResponse:
    return success(request, await service(request).run(authenticated_actor(request), run_id))


@router.post("/runs/{run_id}/cancel", status_code=202)
async def cancel(request: Request, run_id: UUID) -> JSONResponse:
    return success(
        request, await service(request).cancel(authenticated_actor(request), run_id), status=202
    )


@router.post("/runs/{run_id}/resume", status_code=202)
async def resume(request: Request, run_id: UUID, body: ResumeInput) -> JSONResponse:
    return success(
        request,
        await service(request).resume(
            authenticated_actor(request),
            run_id,
            wait_id=body.input_request_id,
            answer=body.answer,
            resume=body.resume,
        ),
        status=202,
    )


@router.get("/citations/{citation_id}")
async def citation(request: Request, citation_id: UUID) -> JSONResponse:
    return success(
        request, await service(request).citation(authenticated_actor(request), citation_id)
    )

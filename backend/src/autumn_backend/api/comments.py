"""留言、举报和审核 HTTP 入口；数据库操作由应用服务承担。"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse, Response

from autumn_backend.api.deps import authenticated_actor
from autumn_backend.api.responses import success
from autumn_backend.errors import ConfigurationError
from autumn_backend.services.comments import CommentService, CreateCommentCommand

router = APIRouter(prefix="/api", tags=["comments"])
PageLimit = Annotated[int, Query(ge=1, le=100)]
PageCursor = Annotated[str | None, Query(max_length=512)]


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateComment(Payload):
    client_id: UUID
    body: str = Field(min_length=1, max_length=2000)
    resource_id: UUID | None = None
    parent_id: UUID | None = None


class Version(Payload):
    expected_version: int = Field(ge=0)


class EditComment(Version):
    body: str = Field(min_length=1, max_length=2000)


class ReportComment(Payload):
    comment_id: UUID
    reason: str = Field(min_length=1, max_length=2000)


class Decision(Version):
    decision: Literal["approve", "reject", "hide"]
    reason: str = Field(min_length=1, max_length=2000)


class Resolution(Version):
    resolution: str = Field(min_length=1, max_length=2000)
    dismissed: bool = False


def service(request: Request) -> CommentService:
    value = getattr(request.app.state, "comments", None)
    if not isinstance(value, CommentService):
        raise ConfigurationError("留言服务未初始化")
    return value


@router.get("/public/comments")
async def public_comments(
    request: Request,
    resource_id: UUID | None = None,
    limit: PageLimit = 20,
    cursor: PageCursor = None,
) -> JSONResponse:
    return success(
        request,
        await service(request).public_page(resource_id=resource_id, limit=limit, cursor=cursor),
    )


@router.post("/comments", status_code=201)
async def create(request: Request, body: CreateComment) -> JSONResponse:
    return success(
        request,
        await service(request).create_comment(
            authenticated_actor(request),
            CreateCommentCommand(**body.model_dump()),
            request_id=request.state.request_id,
        ),
        status=201,
    )


@router.patch("/comments/{comment_id}")
async def edit(request: Request, comment_id: UUID, body: EditComment) -> JSONResponse:
    return success(
        request,
        await service(request).edit(authenticated_actor(request), comment_id, **body.model_dump()),
    )


@router.delete("/comments/{comment_id}", status_code=204)
async def delete(request: Request, comment_id: UUID, body: Version) -> Response:
    await service(request).delete(
        authenticated_actor(request), comment_id, expected_version=body.expected_version
    )
    return Response(status_code=204)


@router.post("/reports", status_code=201)
async def report(request: Request, body: ReportComment) -> JSONResponse:
    return success(
        request,
        await service(request).report(
            authenticated_actor(request), body.comment_id, reason=body.reason
        ),
        status=201,
    )


@router.get("/moderation/comments")
async def moderation(
    request: Request,
    status: Literal["pending", "approved", "rejected", "hidden"] | None = "pending",
    limit: PageLimit = 20,
    cursor: PageCursor = None,
) -> JSONResponse:
    return success(
        request,
        await service(request).moderation_page(
            authenticated_actor(request), status=status, limit=limit, cursor=cursor
        ),
    )


@router.post("/moderation/comments/{comment_id}/decision")
async def decide(request: Request, comment_id: UUID, body: Decision) -> JSONResponse:
    return success(
        request,
        await service(request).decide(
            authenticated_actor(request), comment_id, **body.model_dump()
        ),
    )


@router.get("/moderation/reports")
async def reports(
    request: Request,
    status: Literal["open", "resolved", "dismissed"] = "open",
    limit: PageLimit = 20,
    cursor: PageCursor = None,
) -> JSONResponse:
    return success(
        request,
        await service(request).reports_page(
            authenticated_actor(request), status=status, limit=limit, cursor=cursor
        ),
    )


@router.post("/moderation/reports/{report_id}/resolve")
async def resolve(request: Request, report_id: UUID, body: Resolution) -> JSONResponse:
    return success(
        request,
        await service(request).resolve_report(
            authenticated_actor(request), report_id, **body.model_dump()
        ),
    )

"""公开读取与发布预览入口，所有数据由应用服务产生。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.responses import JSONResponse, Response

from autumn_backend.api.deps import authenticated_actor
from autumn_backend.api.responses import success
from autumn_backend.errors import ConfigurationError
from autumn_backend.services.actions import ActionService, ResourcePreview
from autumn_backend.services.public import PublicService
from autumn_backend.services.publication import (
    PUBLIC_FIELD_ALIASES,
    PublicationService,
    RevokeCommand,
)
from autumn_backend.services.resources import DeleteResource

router = APIRouter(prefix="/api", tags=["public"])
Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(max_length=512)]
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]


def service(request: Request) -> PublicService:
    value = getattr(request.app.state, "public", None)
    if not isinstance(value, PublicService):
        raise ConfigurationError("公开读取服务未初始化")
    return value


class PreviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision_id: UUID
    public_fields: list[str] = Field(min_length=1, max_length=5)
    expected_version: int = Field(ge=0)
    expected_acl_version: int = Field(ge=0)
    ai_enabled: bool = False
    raw_download_enabled: bool = False

    @field_validator("public_fields")
    @classmethod
    def normalize_fields(cls, value: list[str]) -> list[str]:
        fields = {PUBLIC_FIELD_ALIASES.get(item, item) for item in value}
        if not fields <= {"title", "body", "note", "url", "tags"}:
            raise ValueError("公开字段无效")
        return sorted(fields)


@router.get("/public/site")
async def site(request: Request) -> JSONResponse:
    return success(
        request,
        {
            "name": "秋序",
            "english_name": "Autumn Prelude",
            "navigation": ["手记", "收藏", "AI 助手", "留言"],
            "links": [],
            "character": "tingyun",
        },
    )


@router.get("/public/notes")
async def notes(
    request: Request,
    limit: Limit = 20,
    cursor: Cursor = None,
    tag: Annotated[str | None, Query(max_length=40)] = None,
) -> JSONResponse:
    return success(request, await service(request).notes(tag=tag, limit=limit, cursor=cursor))


@router.get("/public/bookmarks")
async def bookmarks(
    request: Request,
    limit: Limit = 20,
    cursor: Cursor = None,
    tag: Annotated[str | None, Query(max_length=40)] = None,
) -> JSONResponse:
    return success(request, await service(request).bookmarks(tag=tag, limit=limit, cursor=cursor))


@router.get("/public/notes/{slug}")
async def note(request: Request, slug: str) -> JSONResponse:
    return success(request, await service(request).note(slug))


@router.get("/public/sources/{publication_id}")
async def source(request: Request, publication_id: UUID) -> JSONResponse:
    return success(request, await service(request).by_publication(publication_id))


@router.get("/public/sources/{publication_id}/file")
async def file(request: Request, publication_id: UUID) -> Response:
    content, mime = await service(request).file(publication_id)
    return Response(
        content,
        media_type=mime,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "attachment",
        },
    )


@router.post("/resources/{resource_id}/publication/preview", status_code=201)
async def preview(
    request: Request, resource_id: UUID, body: PreviewInput, key: Key
) -> JSONResponse:
    actor = authenticated_actor(request)
    actions = getattr(request.app.state, "actions", None)
    if not isinstance(actions, ActionService):
        raise ConfigurationError("动作服务未初始化")
    command = ResourcePreview(
        kind="publish",
        target_id=resource_id,
        revision_id=body.revision_id,
        expected_version=body.expected_version,
        expected_acl_version=body.expected_acl_version,
        public_fields=tuple(body.public_fields),
        ai_enabled=body.ai_enabled,
        raw_download_enabled=body.raw_download_enabled,
    )
    return success(request, await actions.preview(actor, command, idempotency_key=key), status=201)


@router.post("/resources/{resource_id}/publication/revoke")
async def revoke(
    request: Request, resource_id: UUID, body: DeleteResource, key: Key
) -> JSONResponse:
    actor = authenticated_actor(request)
    publications = getattr(request.app.state, "publications", None)
    if not isinstance(publications, PublicationService):
        raise ConfigurationError("发布服务未初始化")
    return success(
        request,
        await publications.revoke_explicit(
            actor,
            RevokeCommand(
                resource_id=resource_id,
                expected_version=body.expected_version,
                expected_acl_version=body.expected_acl_version,
                idempotency_key=key,
            ),
            request_id=request.state.request_id,
        ),
    )

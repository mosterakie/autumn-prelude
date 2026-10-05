"""私人资源路由；绝不直接读取 ORM 或存储对象。"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from starlette.responses import JSONResponse, Response

from autumn_backend.api.deps import authenticated_actor
from autumn_backend.api.responses import success
from autumn_backend.errors import ConfigurationError
from autumn_backend.services.resources import (
    CreateResource,
    DeleteResource,
    EditResource,
    ResourceService,
)

router = APIRouter(prefix="/api/resources", tags=["resources"])
RequestKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]
PageLimit = Annotated[int, Query(ge=1, le=100)]
PageCursor = Annotated[str | None, Query(max_length=512)]


def service(request: Request) -> ResourceService:
    value = getattr(request.app.state, "resources", None)
    if not isinstance(value, ResourceService):
        raise ConfigurationError("资源服务未初始化")
    return value


@router.get("")
async def list_resources(
    request: Request,
    kind: Literal["article", "bookmark", "document", "webpage"] | None = None,
    limit: PageLimit = 20,
    cursor: PageCursor = None,
) -> JSONResponse:
    return success(
        request,
        await service(request).list(
            authenticated_actor(request), kind=kind, limit=limit, cursor=cursor
        ),
    )


@router.get("/{resource_id}")
async def read(request: Request, resource_id: UUID) -> JSONResponse:
    return success(request, await service(request).read(authenticated_actor(request), resource_id))


@router.get("/{resource_id}/versions")
async def versions(
    request: Request, resource_id: UUID, limit: PageLimit = 20, cursor: PageCursor = None
) -> JSONResponse:
    return success(
        request,
        await service(request).versions(
            authenticated_actor(request), resource_id, limit=limit, cursor=cursor
        ),
    )


@router.get("/{resource_id}/versions/{revision_id}/file")
async def file(request: Request, resource_id: UUID, revision_id: UUID) -> Response:
    content, mime = await service(request).file(
        authenticated_actor(request), resource_id, revision_id
    )
    return Response(
        content,
        media_type=mime,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "attachment",
        },
    )


@router.post("", status_code=201)
async def create(request: Request, body: CreateResource, key: RequestKey) -> JSONResponse:
    return success(
        request,
        await service(request).mutate(authenticated_actor(request), key=key, command=body),
        status=201,
    )


@router.patch("/{resource_id}")
async def edit(
    request: Request, resource_id: UUID, body: EditResource, key: RequestKey
) -> JSONResponse:
    return success(
        request,
        await service(request).mutate(
            authenticated_actor(request), key=key, command=body, resource_id=resource_id
        ),
    )


@router.delete("/{resource_id}", status_code=204)
async def delete(
    request: Request, resource_id: UUID, body: DeleteResource, key: RequestKey
) -> Response:
    await service(request).mutate(
        authenticated_actor(request), key=key, command=body, resource_id=resource_id
    )
    return Response(status_code=204)

"""站长知识导入入口：请求解析、可信身份、应用服务与统一响应。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import Response

from autumn_backend.api.deps import authenticated_actor
from autumn_backend.api.resources import RequestKey
from autumn_backend.api.responses import success
from autumn_backend.api.tasks import ResumeTask
from autumn_backend.errors import ConfigurationError, InvalidInputError
from autumn_backend.services.knowledge_imports import ImportURL, KnowledgeImportService
from autumn_backend.services.storage import MAX_FILE_BYTES
from autumn_backend.services.tasks import TaskService

router = APIRouter(prefix="/api", tags=["knowledge"])


def tasks(request: Request) -> TaskService:
    value = getattr(request.app.state, "tasks", None)
    if not isinstance(value, TaskService):
        raise ConfigurationError("后台任务服务未配置")
    return value


def service(request: Request) -> KnowledgeImportService:
    value = getattr(request.app.state, "knowledge_imports", None)
    if not isinstance(value, KnowledgeImportService):
        raise ConfigurationError("知识导入服务未配置")
    return value


@router.post("/knowledge/files", status_code=202)
async def upload_file(
    request: Request,
    key: RequestKey,
    file: Annotated[UploadFile, File()],
    title: Annotated[str | None, Form(max_length=300)] = None,
) -> Response:
    actor = authenticated_actor(request)
    imports = service(request)
    try:
        await imports.authorize(actor)
        data = await file.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise InvalidInputError("文件不得超过 20 MiB")
        filename = file.filename or ""
        result = await imports.file(
            actor,
            key=key,
            filename=filename,
            title=(title or "").strip() or filename,
            data=data,
            mime=file.content_type or "application/octet-stream",
        )
        return success(request, result, status=202)
    finally:
        await file.close()


@router.post("/knowledge/urls", status_code=202)
async def import_url(request: Request, command: ImportURL, key: RequestKey) -> Response:
    return success(
        request,
        await service(request).url(
            authenticated_actor(request),
            command,
            key=key,
        ),
        status=202,
    )


class RefreshResource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)


@router.post("/resources/{resource_id}/refresh", status_code=202)
async def refresh(
    request: Request, resource_id: UUID, command: RefreshResource, key: RequestKey
) -> Response:
    return success(
        request,
        await service(request).refresh(
            authenticated_actor(request),
            resource_id,
            key=key,
            expected_version=command.expected_version,
        ),
        status=202,
    )


@router.get("/jobs/{job_id}")
async def read_job(request: Request, job_id: UUID) -> Response:
    return success(request, await tasks(request).read(authenticated_actor(request), job_id))


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: UUID) -> Response:
    return success(request, await tasks(request).cancel(authenticated_actor(request), job_id))


@router.post("/jobs/{job_id}/retry", status_code=202)
async def resume_job(request: Request, job_id: UUID, command: ResumeTask) -> Response:
    actor = authenticated_actor(request)
    # 通用入口仅允许知识任务；其他恢复入口继续使用 /owner/jobs。
    await tasks(request).read(actor, job_id)
    return success(
        request,
        await tasks(request).resume(
            actor,
            job_id,
            expected_version=command.expected_version,
        ),
        status=202,
    )

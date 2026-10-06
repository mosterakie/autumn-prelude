"""已批准文件/索引任务在站长重新验证后受控换绑会话。"""

from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from autumn_backend.api.deps import authenticated_actor
from autumn_backend.api.responses import success
from autumn_backend.errors import ConfigurationError
from autumn_backend.services.tasks import TaskService

router = APIRouter(prefix="/api/owner/jobs", tags=["owner-jobs"])


class ResumeTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)


@router.post("/{job_id}/resume", status_code=202)
async def resume(request: Request, job_id: UUID, body: ResumeTask) -> JSONResponse:
    service = getattr(request.app.state, "tasks", None)
    if not isinstance(service, TaskService):
        raise ConfigurationError("后台任务服务未初始化")
    return success(
        request,
        await service.resume(
            authenticated_actor(request),
            job_id,
            expected_version=body.expected_version,
        ),
        status=202,
    )

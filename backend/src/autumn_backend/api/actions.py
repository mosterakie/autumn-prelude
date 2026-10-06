"""用户确认只能消费持久动作；不能替换已展示的参数。"""

from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from autumn_backend.api.deps import authenticated_actor
from autumn_backend.api.responses import success
from autumn_backend.errors import ConfigurationError
from autumn_backend.services.actions import ActionService

router = APIRouter(prefix="/api/actions", tags=["actions"])


class ActionVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_action_version: int = Field(ge=0)


class ConfirmAction(ActionVersion):
    parameters_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def service(request: Request) -> ActionService:
    value = getattr(request.app.state, "actions", None)
    if not isinstance(value, ActionService):
        raise ConfigurationError("动作服务未初始化")
    return value


@router.get("/{action_id}")
async def read(request: Request, action_id: UUID) -> JSONResponse:
    return success(request, await service(request).read(authenticated_actor(request), action_id))


@router.post("/{action_id}/execute", status_code=202)
async def execute(request: Request, action_id: UUID, body: ConfirmAction) -> JSONResponse:
    return success(
        request,
        await service(request).confirm(
            authenticated_actor(request),
            action_id,
            expected_action_version=body.expected_action_version,
            parameters_hash=body.parameters_hash,
        ),
        status=202,
    )


@router.post("/{action_id}/cancel")
async def cancel(request: Request, action_id: UUID, body: ActionVersion) -> JSONResponse:
    return success(
        request,
        await service(request).cancel(
            authenticated_actor(request),
            action_id,
            expected_action_version=body.expected_action_version,
        ),
    )

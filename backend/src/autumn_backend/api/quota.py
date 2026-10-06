from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from autumn_backend.api.deps import authenticated_actor
from autumn_backend.api.responses import success
from autumn_backend.errors import ConfigurationError
from autumn_backend.services.quota import QuotaService

router = APIRouter(prefix="/api/me", tags=["quota"])


@router.get("/quota")
async def quota(request: Request) -> JSONResponse:
    service = getattr(request.app.state, "quota", None)
    if not isinstance(service, QuotaService):
        raise ConfigurationError("额度服务未初始化")
    return success(request, await service.current(authenticated_actor(request)))

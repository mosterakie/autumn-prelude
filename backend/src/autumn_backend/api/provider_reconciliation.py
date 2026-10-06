"""站长基于供应商账单/回执人工确认；不注册为 Agent 工具。"""

from uuid import UUID

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from autumn_backend.api.deps import authenticated_actor
from autumn_backend.api.responses import success
from autumn_backend.errors import ConfigurationError
from autumn_backend.services.provider_reconciliation import ProviderReconciliationService, Receipt

router = APIRouter(prefix="/api/owner/provider-calls", tags=["provider-reconciliation"])


def service(request: Request) -> ProviderReconciliationService:
    value = getattr(request.app.state, "provider_reconciliation", None)
    if not isinstance(value, ProviderReconciliationService):
        raise ConfigurationError("供应商对账服务未初始化")
    return value


@router.get("/unknown")
async def unknown(request: Request) -> JSONResponse:
    return success(
        request, {"items": await service(request).candidates(authenticated_actor(request))}
    )


@router.post("/{call_id}/reconcile")
async def confirm(request: Request, call_id: UUID, body: Receipt) -> JSONResponse:
    return success(
        request, await service(request).confirm(authenticated_actor(request), call_id, body)
    )

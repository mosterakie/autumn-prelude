"""领域错误映射；不回显正文、凭据、SQL 或校验失败的输入。"""

from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from autumn_backend.errors import DomainError
from autumn_backend.services.access import AuthorizationError

_STATUS = {
    "NOT_FOUND": 404,
    "INVALID_INPUT": 422,
    "INPUT_CHOICE_REQUIRED": 422,
    "CONFLICT": 409,
    "CONVERSATION_BUSY": 409,
    "VERSION_CONFLICT": 409,
    "IDEMPOTENCY_CONFLICT": 409,
    "LEASE_LOST": 409,
    "QUOTA_EXCEEDED": 429,
    "RATE_LIMITED": 429,
    "CONCURRENCY_LIMIT": 429,
    "CONFIGURATION_UNAVAILABLE": 503,
    "AUTH_REQUIRED": 401,
    "INVALID_CREDENTIALS": 401,
    "INVALID_TOKEN": 422,
    "CSRF_INVALID": 403,
    "ORIGIN_FORBIDDEN": 403,
    "STEP_UP_REQUIRED": 403,
}
_MESSAGES = {
    401: "请登录或重新验证身份",
    403: "当前身份或请求验证未满足要求",
    404: "对象不存在",
    409: "当前状态或版本已变化，请刷新后重试",
    413: "上传请求过大；文件上限为 20 MiB",
    422: "请求参数无效",
    429: "请求超过当前限制，请稍后重试",
    503: "服务暂时不可用",
}
_CODE_MESSAGES = {
    "INPUT_CHOICE_REQUIRED": "请从列出的选项中选择并提交",
}


def error_response(
    request: Request,
    *,
    status: int,
    code: str,
    details: dict[str, Any] | None = None,
    retry_at: datetime | None = None,
) -> JSONResponse:
    error: dict[str, Any] = {
        "code": code,
        "message": _CODE_MESSAGES.get(code, _MESSAGES.get(status, "请求处理失败")),
        "details": details or {},
    }
    headers = {}
    if retry_at is not None:
        seconds = max(0, int((retry_at - datetime.now(UTC)).total_seconds()) + 1)
        error["retry_after_seconds"] = seconds
        headers["Retry-After"] = str(seconds)
    return JSONResponse(
        {"error": error, "request_id": getattr(request.state, "request_id", "unavailable")},
        status_code=status,
        headers=headers,
    )


def domain_response(request: Request, error: DomainError) -> JSONResponse:
    if isinstance(error, AuthorizationError):
        return error_response(
            request,
            status=error.decision.http_status,
            code=str(error.decision.code),
            retry_at=error.decision.retry_at,
        )
    code = error.code.upper()
    return error_response(
        request,
        status=_STATUS.get(code, 400),
        code=code,
        retry_at=getattr(error, "retry_at", None),
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain(request: Request, error: DomainError) -> JSONResponse:
        return domain_response(request, error)

    @app.exception_handler(RequestValidationError)
    async def validation(request: Request, error: RequestValidationError) -> JSONResponse:
        fields = [{"field": list(item["loc"]), "type": item["type"]} for item in error.errors()]
        return error_response(
            request, status=422, code="VALIDATION_ERROR", details={"fields": fields}
        )

    @app.exception_handler(HTTPException)
    async def http(request: Request, error: HTTPException) -> JSONResponse:
        return error_response(
            request,
            status=error.status_code,
            code="NOT_FOUND" if error.status_code == 404 else "HTTP_ERROR",
        )

"""统一入口闸门，覆盖所有路由的已认证写请求。"""

from collections.abc import Awaitable, Callable
from uuid import uuid4

from fastapi import FastAPI, Request
from starlette.responses import Response

from autumn_backend.api.errors import domain_response
from autumn_backend.auth.contracts import AuthenticationPort
from autumn_backend.errors import ConfigurationError, DomainError

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def install_request_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.request_id = f"req-{uuid4().hex}"
        request.state.session = None
        try:
            token = request.cookies.get(request.app.state.settings.cookie_name)
            if token:
                auth: AuthenticationPort | None = getattr(request.app.state, "auth", None)
                if auth is None:
                    raise ConfigurationError("认证服务尚未配置")
                request.state.session = await auth.authenticate(token)
                if request.state.session is not None and request.method not in SAFE_METHODS:
                    auth.verify_write(
                        request.state.session,
                        origin=request.headers.get("origin"),
                        csrf_token=request.headers.get("x-csrf-token"),
                    )
            response = await call_next(request)
        except DomainError as error:
            response = domain_response(request, error)
        response.headers["X-Request-ID"] = request.state.request_id
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

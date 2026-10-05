"""邮箱登录入口：解析与 Cookie 输出，数据库操作全部交给 auth service。"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.responses import JSONResponse, Response

from autumn_backend.api.deps import authenticated_actor, current_session
from autumn_backend.api.responses import success
from autumn_backend.auth.contracts import SessionView
from autumn_backend.auth.service import AuthService, LoginResult
from autumn_backend.errors import ConfigurationError


def service(request: Request) -> AuthService:
    value = getattr(request.app.state, "auth", None)
    if not isinstance(value, AuthService):
        raise ConfigurationError("认证服务未初始化")
    return value


def trusted_origin(request: Request) -> None:
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        service(request).verify_origin(request.headers.get("origin"))


router = APIRouter(prefix="/api/auth", tags=["auth"], dependencies=[Depends(trusted_origin)])


class AuthInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmailInput(AuthInput):
    email: str = Field(min_length=3, max_length=254)


class RegisterInput(EmailInput):
    password: SecretStr = Field(min_length=12, max_length=256)
    display_name: str | None = Field(default=None, min_length=1, max_length=80)


class LoginInput(EmailInput):
    password: SecretStr = Field(min_length=1, max_length=256)


class TokenInput(AuthInput):
    token: SecretStr = Field(min_length=40, max_length=128)


class ResetInput(TokenInput):
    new_password: SecretStr = Field(min_length=12, max_length=256)


class StepUpInput(AuthInput):
    totp_code: str | None = Field(default=None, min_length=6, max_length=6)
    recovery_code: SecretStr | None = Field(default=None, min_length=40, max_length=128)


def remote_address(request: Request) -> str:
    return request.client.host if request.client else "unavailable"


def identity(session: SessionView | None) -> dict[str, object]:
    return {
        "user": session.user if session else None,
        "csrf_token": session.csrf_token if session else None,
        "server_time": datetime.now(UTC),
    }


def set_session_cookie(request: Request, result: LoginResult) -> JSONResponse:
    response = success(request, identity(result.session))
    settings = request.app.state.settings
    response.set_cookie(
        settings.cookie_name,
        result.token,
        max_age=settings.session_ttl_hours * 3600,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
        domain=settings.cookie_domain,
    )
    return response


def clear_cookie(request: Request, response: Response) -> None:
    settings = request.app.state.settings
    response.delete_cookie(
        settings.cookie_name,
        path="/",
        domain=settings.cookie_domain,
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


@router.get("/me")
async def me(request: Request) -> JSONResponse:
    session = current_session(request)
    response = success(request, identity(session))
    if session is None and request.cookies.get(request.app.state.settings.cookie_name):
        clear_cookie(request, response)
    return response


@router.post("/register", status_code=202)
async def register(request: Request, body: RegisterInput) -> JSONResponse:
    await service(request).register(
        email=body.email,
        password=body.password.get_secret_value(),
        display_name=body.display_name,
        remote_address=remote_address(request),
    )
    return success(request, {"accepted": True}, status=202)


@router.post("/verify-email")
async def verify_email(request: Request, body: TokenInput) -> JSONResponse:
    await service(request).verify_email(
        raw=body.token.get_secret_value(), remote_address=remote_address(request)
    )
    return success(request, {"verified": True})


@router.post("/resend-verification", status_code=202)
async def resend(request: Request, body: EmailInput) -> JSONResponse:
    await service(request).resend_verification(
        email=body.email, remote_address=remote_address(request)
    )
    return success(request, {"accepted": True}, status=202)


@router.post("/login")
async def login(request: Request, body: LoginInput) -> JSONResponse:
    previous = current_session(request)
    result = await service(request).login(
        email=body.email,
        password=body.password.get_secret_value(),
        remote_address=remote_address(request),
        previous=previous.actor.auth_session_id if previous else None,
    )
    return set_session_cookie(request, result)


@router.post("/logout", status_code=204)
async def logout(request: Request) -> Response:
    await service(request).logout(authenticated_actor(request))
    response = Response(status_code=204)
    clear_cookie(request, response)
    return response


@router.post("/forgot-password", status_code=202)
async def forgot(request: Request, body: EmailInput) -> JSONResponse:
    await service(request).forgot_password(email=body.email, remote_address=remote_address(request))
    return success(request, {"accepted": True}, status=202)


@router.post("/reset-password")
async def reset(request: Request, body: ResetInput) -> JSONResponse:
    await service(request).reset_password(
        raw=body.token.get_secret_value(),
        new_password=body.new_password.get_secret_value(),
        remote_address=remote_address(request),
    )
    response = success(request, {"reset": True, "login_required": True})
    clear_cookie(request, response)
    return response


@router.post("/step-up")
async def step_up(request: Request, body: StepUpInput) -> JSONResponse:
    result = await service(request).step_up(
        authenticated_actor(request),
        code=body.totp_code,
        recovery_code=body.recovery_code.get_secret_value() if body.recovery_code else None,
        remote_address=remote_address(request),
    )
    return set_session_cookie(request, result)

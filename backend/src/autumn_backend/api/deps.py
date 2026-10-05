"""依赖只返回认证快照与应用服务，不把数据库会话注入路由。"""

from typing import cast

from fastapi import Request

from autumn_backend.auth.contracts import AuthenticationPort, SessionView, anonymous_actor
from autumn_backend.errors import ConfigurationError, DomainError
from autumn_backend.policies import ActorContext


class AuthenticationRequired(DomainError):
    code = "auth_required"


def current_session(request: Request) -> SessionView | None:
    return cast(SessionView | None, request.state.session)


def actor(request: Request) -> ActorContext:
    session = current_session(request)
    return session.actor if session is not None else anonymous_actor()


def authenticated_actor(request: Request) -> ActorContext:
    session = current_session(request)
    if session is None:
        raise AuthenticationRequired()
    return session.actor


def authentication(request: Request) -> AuthenticationPort:
    service: AuthenticationPort | None = getattr(request.app.state, "auth", None)
    if service is None:
        raise ConfigurationError("认证服务尚未配置")
    return service

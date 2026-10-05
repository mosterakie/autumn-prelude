"""HTTP 入口使用的认证快照；不得包含 ORM 或原始凭据。"""

from dataclasses import dataclass
from typing import Any, Protocol

from autumn_backend.policies import ActorContext, ActorRole, Capability


@dataclass(frozen=True, slots=True)
class SessionView:
    actor: ActorContext
    user: dict[str, Any]
    csrf_token: str
    csrf_version: int


def anonymous_actor() -> ActorContext:
    return ActorContext(
        user_id=None,
        role=ActorRole.ANONYMOUS,
        auth_session_id=None,
        step_up_expires_at=None,
        capabilities=frozenset({Capability.READ_PUBLIC}),
        scope_epoch=0,
    )


class AuthenticationPort(Protocol):
    async def authenticate(self, token: str) -> SessionView | None: ...

    def verify_write(
        self, session: SessionView, *, origin: str | None, csrf_token: str | None
    ) -> None: ...

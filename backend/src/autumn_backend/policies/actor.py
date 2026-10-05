"""仅由服务端认证入口装配的身份快照；不是请求模型或授权凭据。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from autumn_backend.policies._validation import (
    nonnegative_integer,
    optional_datetime,
    uuid_value,
)


class ActorRole(StrEnum):
    ANONYMOUS = "anonymous"
    MEMBER = "member"
    OWNER = "owner"


class Capability(StrEnum):
    READ_PUBLIC = "read_public"
    OWN_CHAT = "own_chat"
    PUBLIC_AI = "public_ai"
    WRITE_COMMENT = "write_comment"
    PRIVATE_KNOWLEDGE = "private_knowledge"
    SEARCH_WEB = "search_web"
    MANAGE_CONTENT = "manage_content"
    MANAGE_SITE = "manage_site"
    OWN_ACTION = "own_action"
    OWN_MEMORY = "own_memory"


@dataclass(frozen=True, slots=True, kw_only=True)
class ActorContext:
    """当前身份快照。capabilities 只用于 UI 提示，不能独立授予权限。

    API 从 Cookie 对应的服务端记录、Agent 从 Run.auth_session_id、Worker 从
    Job 的关联记录重建。工具业务参数和 checkpoint 均不得反序列化为本类型。
    frozen 防止意外修改；真正的信任边界是入口装配和判定时的当前记录校验。
    """

    user_id: UUID | None
    role: ActorRole
    auth_session_id: UUID | None
    step_up_expires_at: datetime | None
    capabilities: frozenset[Capability]
    scope_epoch: int

    def __post_init__(self) -> None:
        if not isinstance(self.role, ActorRole):
            raise ValueError("role must be an ActorRole")
        if not isinstance(self.capabilities, frozenset) or any(
            not isinstance(value, Capability) for value in self.capabilities
        ):
            raise ValueError("capabilities must be a frozenset of Capability")
        nonnegative_integer(self.scope_epoch, "scope_epoch")
        optional_datetime(self.step_up_expires_at, "step_up_expires_at")
        if self.role is ActorRole.ANONYMOUS:
            if any(
                value is not None
                for value in (self.user_id, self.auth_session_id, self.step_up_expires_at)
            ):
                raise ValueError("anonymous actors cannot carry a user or session")
        else:
            if self.user_id is None or self.auth_session_id is None:
                raise ValueError("authenticated actors require a user and session")
            uuid_value(self.user_id, "user_id")
            uuid_value(self.auth_session_id, "auth_session_id")
            if self.role is not ActorRole.OWNER and self.step_up_expires_at is not None:
                raise ValueError("only owner actors can carry step-up")

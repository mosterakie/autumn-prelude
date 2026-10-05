"""可由 API 或执行器映射的判定结果；不含被拒对象的敏感细节。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from autumn_backend.policies._validation import optional_datetime


class DenialCode(StrEnum):
    AUTH_REQUIRED = "AUTH_REQUIRED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    EMAIL_UNVERIFIED = "EMAIL_UNVERIFIED"
    STEP_UP_REQUIRED = "STEP_UP_REQUIRED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    AI_COOLDOWN = "AI_COOLDOWN"
    ACL_CONTEXT_INVALIDATED = "ACL_CONTEXT_INVALIDATED"


@dataclass(frozen=True, slots=True, kw_only=True)
class Decision:
    code: DenialCode | None = None
    retry_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.code is not None and not isinstance(self.code, DenialCode):
            raise ValueError("code must be a DenialCode")
        optional_datetime(self.retry_at, "retry_at")
        if self.retry_at is not None and self.code is not DenialCode.AI_COOLDOWN:
            raise ValueError("retry_at is only valid for AI_COOLDOWN")

    @property
    def allowed(self) -> bool:
        return self.code is None

    @property
    def http_status(self) -> int:
        match self.code:
            case None:
                return 200
            case DenialCode.AUTH_REQUIRED | DenialCode.SESSION_EXPIRED:
                return 401
            case DenialCode.NOT_FOUND:
                return 404
            case DenialCode.AI_COOLDOWN:
                return 429
            case DenialCode.ACL_CONTEXT_INVALIDATED:
                return 409
            case _:
                return 403

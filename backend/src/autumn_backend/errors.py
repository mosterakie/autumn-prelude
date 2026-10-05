"""领域错误携带稳定错误码，不包含数据库细节或私人内容。"""

from datetime import datetime


class DomainError(Exception):
    code = "domain_error"


class InvalidInputError(DomainError):
    code = "invalid_input"


class NotFoundError(DomainError):
    code = "not_found"


class ConflictError(DomainError):
    code = "conflict"


class IdempotencyConflictError(ConflictError):
    code = "idempotency_conflict"


class OptimisticLockError(ConflictError):
    code = "version_conflict"


class QuotaExceededError(DomainError):
    code = "quota_exceeded"


class RateLimitedError(DomainError):
    code = "rate_limited"

    def __init__(self, message: str, *, retry_at: datetime) -> None:
        self.retry_at = retry_at
        super().__init__(message)


class ConcurrencyLimitError(DomainError):
    code = "concurrency_limit"


class LeaseLostError(ConflictError):
    code = "lease_lost"


class ConfigurationError(DomainError):
    code = "configuration_unavailable"

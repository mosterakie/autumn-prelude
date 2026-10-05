"""领域错误携带稳定错误码，不包含数据库细节或私人内容。"""


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


class LeaseLostError(ConflictError):
    code = "lease_lost"


class ConfigurationError(DomainError):
    code = "configuration_unavailable"

"""ORM 模型注册表。

Alembic 的 autogenerate 与 check 只看得见"已经被导入过"的模型，
因此所有模型模块都必须在这里登记一次。

- 阶段 A3：``identity`` —— users / auth_sessions / auth_tokens /
  admin_factors / rate_limit_buckets / settings
- 阶段 A5：``content`` —— resources / resource_versions / publications /
  comments / knowledge_indexes / run_sources
- 阶段 A6：``runtime`` —— conversations / messages / runs / run_events /
  quota_buckets / quota_reservations / jobs / provider_calls /
  audit_events / actions
"""

from __future__ import annotations

from autumn_backend.db.models.content import (
    CHUNK_TEXT_COLUMN,
    EMBEDDING_DIMENSIONS,
    KNOWLEDGE_INDEX_VECTOR_INDEX,
    Comment,
    KnowledgeIndex,
    Publication,
    Resource,
    ResourceVersion,
    RunSource,
)
from autumn_backend.db.models.identity import (
    AdminFactor,
    AuthSession,
    AuthToken,
    RateLimitBucket,
    Setting,
    User,
)
from autumn_backend.db.models.runtime import (
    Action,
    AuditEvent,
    Conversation,
    Job,
    Message,
    ProviderCall,
    QuotaBucket,
    QuotaReservation,
    Run,
    RunEvent,
)

__all__ = [
    "CHUNK_TEXT_COLUMN",
    "EMBEDDING_DIMENSIONS",
    "KNOWLEDGE_INDEX_VECTOR_INDEX",
    "Action",
    "AdminFactor",
    "AuditEvent",
    "AuthSession",
    "AuthToken",
    "Comment",
    "Conversation",
    "Job",
    "KnowledgeIndex",
    "Message",
    "ProviderCall",
    "Publication",
    "QuotaBucket",
    "QuotaReservation",
    "RateLimitBucket",
    "Resource",
    "ResourceVersion",
    "Run",
    "RunEvent",
    "RunSource",
    "Setting",
    "User",
    "load_all_models",
]


def load_all_models() -> None:
    """导入全部 ORM 模型，使 ``Base.metadata`` 完整。

    幂等：重复调用只会命中 Python 的模块缓存。
    新增模型模块时**必须**在这里补一次导入。
    """
    return None

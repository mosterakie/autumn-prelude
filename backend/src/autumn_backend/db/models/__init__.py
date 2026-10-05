"""ORM 模型注册表。

``Base.metadata`` 的完整性由**本模块的导入副作用**保证：Alembic 的
autogenerate 与 check 只看得见"已经被导入过"的模型，因此所有模型模块
都必须在这里登记一次。``load_all_models()`` 只是显式入口，真正的注册
发生在下面的模块级导入。

模块划分与**迁移批次**一一对应，序号即建表顺序：

- ``identity``（批一，A3/A4）：users / auth_sessions / auth_tokens /
  admin_factors / rate_limit_buckets / settings
- ``content``（批二，A5）：retention_policies / resources / file_objects /
  resource_versions / publications / comments / reports
- ``runtime``（批三，A6）：conversations / runs / messages / actions /
  quota_buckets / quota_reservations / jobs / provider_calls /
  audit_events / run_events
- ``knowledge``（批四，A7）：knowledge_indexes / knowledge_chunks /
  run_sources / conversation_summaries / memories

共 28 张表，按 ``docs/architecture/database.md`` 的物理建模说明实现。
LangGraph 自带状态（``agent_state`` 等）由固定版本的持久化适配器维护，
**不在本元数据中**，应用只持有 conversation → ``runs.checkpoint_thread_id`` 映射。
"""

from __future__ import annotations

from autumn_backend.db.models.content import (
    PUBLIC_FIELD_NAMES,
    Comment,
    FileObject,
    Publication,
    Report,
    Resource,
    ResourceVersion,
    RetentionPolicy,
)
from autumn_backend.db.models.identity import (
    AdminFactor,
    AuthSession,
    AuthToken,
    RateLimitBucket,
    Setting,
    User,
)
from autumn_backend.db.models.knowledge import (
    EMBEDDING_DIMENSIONS,
    ConversationSummary,
    KnowledgeChunk,
    KnowledgeIndex,
    Memory,
    RunSource,
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
    "EMBEDDING_DIMENSIONS",
    "PUBLIC_FIELD_NAMES",
    "Action",
    "AdminFactor",
    "AuditEvent",
    "AuthSession",
    "AuthToken",
    "Comment",
    "Conversation",
    "ConversationSummary",
    "FileObject",
    "Job",
    "KnowledgeChunk",
    "KnowledgeIndex",
    "Memory",
    "Message",
    "ProviderCall",
    "Publication",
    "QuotaBucket",
    "QuotaReservation",
    "RateLimitBucket",
    "Report",
    "Resource",
    "ResourceVersion",
    "RetentionPolicy",
    "Run",
    "RunEvent",
    "RunSource",
    "Setting",
    "User",
    "load_all_models",
]


def load_all_models() -> None:
    """显式注册入口；真正的注册是本模块导入的副作用。幂等。"""
    return None

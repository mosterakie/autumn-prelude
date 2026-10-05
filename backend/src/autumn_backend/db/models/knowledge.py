"""第四批模型：知识索引、分块、运行来源、会话摘要与记忆。

对应 ``docs/architecture/database.md`` §5、§7，以及迁移批次四：
``knowledge_indexes`` / ``knowledge_chunks`` /
``run_sources`` / ``conversation_summaries`` / ``memories``。

关键结构约束：

- 索引**元数据**（``knowledge_indexes``）与**向量分块**（``knowledge_chunks``）分开：
  文档明确要求两层，查询按 ``index_id`` 先过滤权限，而不是先全库召回再在应用层删掉越权结果。
- ``scope='public'`` 必须绑定当时的 publication，并用**复合外键**
  ``(resource_id, revision_id, publication_id)`` 证明三者同属一个资源与版本。
- 每个私人 revision 最多一个活动索引；每个 publication 最多一个活动索引，
  分别用部分唯一索引约束；**活动索引必须 ready**。
- ``run_sources`` 记录**所有**实际进入模型的来源（含经历史消息、摘要或记忆
  间接带入的依赖），不只最终展示引用；``UNIQUE(run_id, source_key)`` 保证同一 run 内不重号。

**有意偏离文档**：首版**不建**近似向量索引。文档 §5 明确"首版精确向量查询，
不建近似索引"，因此 ``knowledge_chunks`` 只建 ``(index_id, chunk_no)`` 唯一约束
与 ``index_id`` B-tree。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from autumn_backend.db.base import Base
from autumn_backend.db.enums import (
    IndexScope,
    IndexStatus,
    MemoryKind,
    RunSourceType,
    SummaryStatus,
    enum_check_expression,
    enum_column_type,
)
from autumn_backend.db.mixins import (
    CreatedAt,
    Deletable,
    Timestamped,
    UUIDPrimaryKey,
    Versioned,
)

#: 向量列维度。由配置选定的向量模型决定；首版固定 1024。
#: 更换维度必须走显式迁移或新表，**不把不同模型、不同维度的向量混查**。
EMBEDDING_DIMENSIONS = 1024


class KnowledgeIndex(UUIDPrimaryKey, Timestamped, Versioned, Base):
    """知识索引元数据。

    设计要点：

    - ``scope='owner'`` 时 ``publication_id`` 必须为空；``scope='public'`` 时必须绑定
      publication，且复合外键保证 publication 与本行的资源、版本一致。
    - ``UNIQUE(id, embedding_dimension)`` 供向量维度检查。
    - 重建完成后**事务性切换**活动版本；查询始终过滤已授权、``ready`` 且活动的索引。
    - ``generation`` 支持"重建为新 generation，再原子切换"。
    """

    __tablename__ = "knowledge_indexes"

    resource_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("resources.id", ondelete="CASCADE"), nullable=False
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    # 只有 public scope 才非空；复合外键会校验它绑定同一资源与版本。
    publication_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    scope: Mapped[IndexScope] = mapped_column(
        enum_column_type(IndexScope, length=16), nullable=False
    )
    embedding_provider: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_model: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    status: Mapped[IndexStatus] = mapped_column(
        enum_column_type(IndexStatus, length=16), nullable=False, default=IndexStatus.QUEUED
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        # 供向量维度检查与 chunk 侧引用。
        UniqueConstraint(
            "id", "embedding_dimension", name="uq_knowledge_indexes_id_embedding_dimension"
        ),
        # 每个私人 revision 最多一个活动索引。
        Index(
            "uq_knowledge_indexes_owner_active",
            "revision_id",
            unique=True,
            postgresql_where=text("is_active AND scope = 'owner'"),
        ),
        # 每个 publication 最多一个活动索引。
        Index(
            "uq_knowledge_indexes_public_active",
            "publication_id",
            unique=True,
            postgresql_where=text("is_active AND scope = 'public'"),
        ),
        CheckConstraint(enum_check_expression("scope", IndexScope), name="scope_valid"),
        CheckConstraint(enum_check_expression("status", IndexStatus), name="status_valid"),
        # scope 与 publication 必须一致。
        CheckConstraint(
            "(scope = 'public') = (publication_id IS NOT NULL)",
            name="publication_id_matches_scope",
        ),
        # 活动索引必须 ready：不允许把构建中的索引暴露给查询。
        CheckConstraint("NOT is_active OR status = 'ready'", name="active_must_be_ready"),
        # 当前列维度固定为 1024。
        CheckConstraint(
            f"embedding_dimension = {EMBEDDING_DIMENSIONS}",
            name="embedding_dimension_matches_column",
        ),
        CheckConstraint("generation >= 1", name="generation_positive"),
        CheckConstraint("length(content_hash) > 0", name="content_hash_not_empty"),
        CheckConstraint("length(embedding_provider) > 0", name="embedding_provider_not_empty"),
        CheckConstraint("length(embedding_model) > 0", name="embedding_model_not_empty"),
        # revision 必须属于同一资源。
        ForeignKeyConstraint(
            ["resource_id", "revision_id"],
            ["resource_versions.resource_id", "resource_versions.id"],
            name="fk_knowledge_indexes_resource_id_revision_id",
            ondelete="CASCADE",
        ),
        # public scope 的 publication 必须绑定同一资源与版本。
        # MATCH SIMPLE：publication_id 为空时不校验，正好对应 owner scope。
        ForeignKeyConstraint(
            ["resource_id", "revision_id", "publication_id"],
            [
                "publications.resource_id",
                "publications.revision_id",
                "publications.id",
            ],
            name="fk_knowledge_indexes_resource_revision_publication",
            ondelete="CASCADE",
        ),
        Index("ix_knowledge_indexes_resource_id_scope", "resource_id", "scope"),
        Index("ix_knowledge_indexes_revision_id", "revision_id"),
    )


class KnowledgeChunk(UUIDPrimaryKey, CreatedAt, Base):
    """知识分块（承载向量）。

    设计要点：

    - ``locator`` 记录 PDF 页码区间、DOCX 章节段落或网页投影内偏移。
    - 首版**精确向量查询，不建近似索引**（文档 §5）。
    - 查询必须先按 ``index_id`` 过滤权限，不能先全库召回再在应用层删掉越权结果。
    """

    __tablename__ = "knowledge_chunks"

    index_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("knowledge_indexes.id", ondelete="CASCADE"), nullable=False
    )
    chunk_no: Mapped[int] = mapped_column(Integer, nullable=False)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    locator: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=False)

    __table_args__ = (
        UniqueConstraint("index_id", "chunk_no", name="uq_knowledge_chunks_index_chunk_no"),
        CheckConstraint("chunk_no >= 0", name="chunk_no_non_negative"),
        CheckConstraint("token_count IS NULL OR token_count >= 0", name="token_count_non_negative"),
        CheckConstraint("length(content_text) > 0", name="content_text_not_empty"),
        CheckConstraint(
            "locator IS NULL OR jsonb_typeof(locator) = 'object'", name="locator_is_object"
        ),
        # 唯一约束已为 (index_id, chunk_no) 建了索引，按 index_id 的前缀查询直接可用，
        # 因此**不再**重复建一个同列的普通 B-tree 索引。
        # 文档要求的 "B-tree(index_id)" 由 uq_knowledge_chunks_index_chunk_no 满足。
    )


class RunSource(UUIDPrimaryKey, CreatedAt, Base):
    """实际进入模型的来源依赖（只追加）。

    设计要点：

    - 记录**所有**实际输入模型的来源，包括通过历史生成消息、摘要或记忆
      间接带入的依赖，不只记录最终展示引用。
    - ``resource`` 类型必须有资源与版本的复合外键；``web`` 类型禁止填入
      无意义的资源外键，且只有站长运行可创建（service 规则）。
    - "public 模式还必须指向当时的 publication"依赖 run 的 scope，
      跨表条件无法用 CHECK 表达，由 service 校验并记录在 ``publication_id``。
    - API 的 ``citation_id`` 就是本表 ``id``；返回前检查 run 归属与来源的当前权限。
    """

    __tablename__ = "run_sources"

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    source_type: Mapped[RunSourceType] = mapped_column(
        enum_column_type(RunSourceType, length=16), nullable=False
    )
    # 同一 run 内的稳定来源键：来源不重号。
    source_key: Mapped[str] = mapped_column(Text, nullable=False)

    resource_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    revision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    publication_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("publications.id", ondelete="SET NULL"), nullable=True
    )
    index_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("knowledge_indexes.id", ondelete="SET NULL"), nullable=True
    )
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("knowledge_chunks.id", ondelete="SET NULL"), nullable=True
    )

    # 记录来源被读取时的可见性版本，便于判断 ACL 是否已变化。
    observed_acl_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    locator: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    web_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    web_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("run_id", "source_key", name="uq_run_sources_run_id_source_key"),
        CheckConstraint(
            enum_check_expression("source_type", RunSourceType), name="source_type_valid"
        ),
        CheckConstraint("length(source_key) > 0", name="source_key_not_empty"),
        CheckConstraint("observed_acl_version >= 0", name="observed_acl_version_non_negative"),
        CheckConstraint(
            "locator IS NULL OR jsonb_typeof(locator) = 'object'", name="locator_is_object"
        ),
        # resource 类型必须有资源与版本；web 类型必须没有资源引用且有 URL。
        CheckConstraint(
            "(source_type = 'resource' AND resource_id IS NOT NULL AND revision_id IS NOT NULL "
            "AND web_url IS NULL AND web_title IS NULL) "
            "OR (source_type = 'web' AND resource_id IS NULL AND revision_id IS NULL "
            "AND publication_id IS NULL AND index_id IS NULL AND chunk_id IS NULL "
            "AND web_url IS NOT NULL)",
            name="source_type_consistent",
        ),
        # index 与 chunk 必须成对出现。
        CheckConstraint("(index_id IS NULL) = (chunk_id IS NULL)", name="index_chunk_paired"),
        # revision 必须属于同一资源。
        ForeignKeyConstraint(
            ["resource_id", "revision_id"],
            ["resource_versions.resource_id", "resource_versions.id"],
            name="fk_run_sources_resource_id_revision_id",
            ondelete="SET NULL",
        ),
        Index("ix_run_sources_run_id", "run_id"),
        Index("ix_run_sources_resource_id", "resource_id"),
    )


class ConversationSummary(UUIDPrimaryKey, Timestamped, Versioned, Base):
    """会话摘要。

    设计要点：

    - 同会话最多一个 ``active`` 摘要（部分唯一索引）。
    - 摘要依赖为该会话截至 ``upto_message_seq`` 的消息关联 runs 及其 run_sources；
      恢复或 ``scope_epoch`` 变化时必须重新检查这些依赖，
      **不能只用摘要文本继续对话而跳过来源检查**。
    """

    __tablename__ = "conversation_summaries"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    upto_message_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    scope_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[SummaryStatus] = mapped_column(
        enum_column_type(SummaryStatus, length=16), nullable=False, default=SummaryStatus.ACTIVE
    )

    __table_args__ = (
        Index(
            "uq_conversation_summaries_active",
            "conversation_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        CheckConstraint(enum_check_expression("status", SummaryStatus), name="status_valid"),
        CheckConstraint("upto_message_seq >= 1", name="upto_message_seq_positive"),
        CheckConstraint("scope_epoch >= 0", name="scope_epoch_non_negative"),
        CheckConstraint("length(body_text) > 0", name="body_text_not_empty"),
        Index("ix_conversation_summaries_conversation_id", "conversation_id", "upto_message_seq"),
    )


class Memory(UUIDPrimaryKey, Timestamped, Versioned, Deletable, Base):
    """站长记忆（偏好与事实）。

    设计要点：

    - 首版**仅站长明确要求记住时**创建；普通用户的自动性格画像不建表、不生成。
    - ``origin_run_id`` 用**单列**外键 + ``ON DELETE SET NULL``：记忆是来源的旁证，
      不该因为"来源 run 还在"而阻止删除会话。原先写成 ``(origin_run_id, user_id)``
      复合外键并声明 ``ON DELETE SET NULL`` 是错的——SET NULL 会把列组里每一列都
      置空，而 ``user_id`` 是 NOT NULL，动作永远无法完成。
      "来源 run 属于同一用户"因此由 service 校验。
    - 手工输入的记忆可以没有来源，但要记录授权 action（``actions``）。
    - 删除记忆时同时处理派生状态（service 规则）。
    """

    __tablename__ = "memories"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[MemoryKind] = mapped_column(
        enum_column_type(MemoryKind, length=16), nullable=False
    )
    content_text: Mapped[str] = mapped_column(Text, nullable=False)

    origin_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True
    )
    origin_message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(enum_check_expression("kind", MemoryKind), name="kind_valid"),
        CheckConstraint("length(content_text) > 0", name="content_text_not_empty"),
        Index("ix_memories_user_id_created_at", "user_id", "created_at"),
        Index(
            "ix_memories_active_user_id",
            "user_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_memories_origin_run_id", "origin_run_id"),
    )


__all__ = [
    "EMBEDDING_DIMENSIONS",
    "ConversationSummary",
    "KnowledgeChunk",
    "KnowledgeIndex",
    "Memory",
    "RunSource",
]

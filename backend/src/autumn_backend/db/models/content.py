"""第二批模型：内容、发布与检索。

覆盖实施顺序 A5 的六张表：

- :class:`Resource` —— 资源主体，**``version`` 与 ``acl_version`` 分离**
- :class:`ResourceVersion` —— 不可变的内容版本
- :class:`Publication` —— 公开投影，单资源只有一个现行版本
- :class:`Comment` —— 评论，幂等身份 ``(author_id, client_id)``
- :class:`KnowledgeIndex` —— 私有/公开分开的检索索引（pgvector，1024 维）
- :class:`RunSource` —— 实际进入模型的全部来源（AppendOnly 语义）

版本语义（架构文档 §5）：

| 字段 | 语义 | 递增时机 |
|---|---|---|
| ``resources.version`` | 私人内容 / metadata 的乐观并发版本 | 编辑原稿、标题、标签、私密备注、归档 |
| ``resources.acl_version`` | 可见性 / 发布状态版本 | publish、revoke、软删除、恢复 |
| ``settings.content_acl_epoch`` | 全站公开权限 epoch | 任何影响公开可见范围的事务 |

**publish / revoke 不递增 ``resources.version``**：可见性变化不是内容变化。
因此 ``acl_version`` 是这里显式声明的列，不属于 ``Versioned`` Mixin。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from autumn_backend.db.base import Base
from autumn_backend.db.enums import (
    CommentStatus,
    ContentFormat,
    IndexKind,
    IndexSourceKind,
    PublicationRevokeReason,
    ResourceType,
    RunSourceKind,
    enum_check_expression,
    enum_column_type,
)
from autumn_backend.db.mixins import SoftDelete, Timestamped, UUIDPrimaryKey, Versioned

# 嵌入向量维度。由配置选定的向量模型决定；A5 定稿为 1024。
# 注意：pgvector 的列维度不可参数化到运行期——换维度必须写迁移。
EMBEDDING_DIMENSIONS = 1024

# 文本片段上限（用于 rerank 与展示），不限制正文长度本身。
_SNIPPET_LENGTH = 4000
_URL_LENGTH = 2048
_HASH_LENGTH = 64

#: HNSW 向量索引名（显式命名，避免 alembic check 漂移）。
KNOWLEDGE_INDEX_VECTOR_INDEX = "ix_knowledge_indexes_embedding_hnsw"

#: 片段文本的数据库列名。
CHUNK_TEXT_COLUMN = "text"


class Resource(UUIDPrimaryKey, Timestamped, Versioned, SoftDelete, Base):
    """资源主体（文章 / 网页 / 文件）。

    设计要点：

    - ``version`` 与 ``acl_version`` **完全解耦**：前者管私人内容并发，
      后者管可见性；publish / revoke 只动后者。
    - ``private_note`` 是私密备注：**绝不进入公开 API 或公开索引**
      （v1 验收标准），发布投影按类型白名单构造，天然不含它。
    - ``title`` / ``tags`` 属于可公开 metadata；``source_url`` 是抓取来源。
    - 软删除与归档由 ``SoftDelete`` 提供，公开读取必须同时过滤这两个时刻。
    """

    __tablename__ = "resources"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[ResourceType] = mapped_column(
        enum_column_type(ResourceType, length=16), nullable=False
    )

    title: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    slug: Mapped[str | None] = mapped_column(String(200), nullable=True)
    tags: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    source_url: Mapped[str | None] = mapped_column(String(_URL_LENGTH), nullable=True)

    # 私密备注：只在私人上下文可见，公开投影必须排除。
    private_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 可见性版本：publish / revoke / 软删除 / 恢复 时递增；与 version 无关。
    acl_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    versions: Mapped[list[ResourceVersion]] = relationship(
        back_populates="resource", cascade="all, delete-orphan", lazy="raise"
    )
    publications: Mapped[list[Publication]] = relationship(
        back_populates="resource", cascade="all, delete-orphan", lazy="raise"
    )
    comments: Mapped[list[Comment]] = relationship(
        back_populates="resource", cascade="all, delete-orphan", lazy="raise"
    )

    __table_args__ = (
        CheckConstraint(enum_check_expression("type", ResourceType), name="type_valid"),
        CheckConstraint("acl_version >= 0", name="acl_version_non_negative"),
        Index("ix_resources_owner_id_type_created_at", "owner_id", "type", "created_at"),
        # 公开读取与私人列表都要按"未删除且未归档"过滤。
        Index("ix_resources_deleted_at_archived_at", "deleted_at", "archived_at"),
        # slug 在同一作者范围内唯一（非全局唯一：不同作者可以取同名 slug）。
        Index(
            "uq_resources_owner_id_slug",
            "owner_id",
            "slug",
            unique=True,
            postgresql_where=text("slug IS NOT NULL"),
        ),
    )


class ResourceVersion(UUIDPrimaryKey, Timestamped, Versioned, Base):
    """不可变的内容版本。

    设计要点：

    - 编辑原稿**新增一行**，而不是改旧行——已发布版本必须保持不变
      （v1 验收：更新私人原稿不改变已发布版本）。
    - ``version_no`` 在同一资源内递增且唯一。
    - ``content_hash`` 支持"内容没变就不新增版本"的判等（由 service 决定策略）。
    - ``storage_key`` 指向对象存储；物理 I/O 永远不在数据库事务里。
    """

    __tablename__ = "resource_versions"

    resource_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("resources.id", ondelete="CASCADE"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    content_format: Mapped[ContentFormat] = mapped_column(
        enum_column_type(ContentFormat, length=16), nullable=False
    )

    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body_hash: Mapped[str] = mapped_column(String(_HASH_LENGTH), nullable=False)

    # 文件类资源的对象存储位置与元数据；网页/文章为 NULL。
    storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    resource: Mapped[Resource] = relationship(back_populates="versions", lazy="raise")

    __table_args__ = (
        UniqueConstraint(
            "resource_id", "version_no", name="uq_resource_versions_resource_version_no"
        ),
        CheckConstraint(
            enum_check_expression("content_format", ContentFormat), name="content_format_valid"
        ),
        CheckConstraint("version_no >= 1", name="version_no_positive"),
        CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="size_bytes_non_negative"),
        # 有对象存储位置就必须有 MIME 与大小：避免"半截上传"记录。
        CheckConstraint(
            "(storage_key IS NULL) = (mime_type IS NULL) AND (storage_key IS NULL) = (size_bytes IS NULL)",
            name="storage_metadata_consistent",
        ),
        Index("ix_resource_versions_resource_id_created_at", "resource_id", "created_at"),
    )


class Publication(UUIDPrimaryKey, Timestamped, Versioned, Base):
    """公开投影。

    设计要点：

    - **一个资源只能有一个现行公开版本**：Partial Unique Index
      ``resource_id WHERE revoked_at IS NULL``。重新发布必须先撤销旧行，
      两步都在同一个事务里完成。
    - ``public_no`` 是资源内递增的发布序号；由 Repository 在锁定 Resource 行后
      计算 ``MAX()+1``（``publish_under_resource_lock``），行锁串行化它。
    - ``payload`` 只保存**按类型白名单构造的公开字段**；
      ``acl_version_at_publish`` 记录发布时的可见性版本，便于判定陈旧。
    - ``revoked_at`` 一旦写入，公开读取立即阻断；索引与缓存清理交给异步 job。
    """

    __tablename__ = "publications"

    resource_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("resources.id", ondelete="CASCADE"), nullable=False
    )
    resource_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("resource_versions.id", ondelete="RESTRICT"), nullable=False
    )
    # 发布时的资源内容版本：用于判断"公开内容对应哪一版原稿"。
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 发布时的可见性版本：与 resources.acl_version 对齐。
    acl_version_at_publish: Mapped[int] = mapped_column(Integer, nullable=False)

    public_no: Mapped[int] = mapped_column(Integer, nullable=False)
    public_url: Mapped[str] = mapped_column(String(_URL_LENGTH), nullable=False)
    # 公开投影：只含白名单字段，**不含** private_note。
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    published_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[PublicationRevokeReason | None] = mapped_column(
        enum_column_type(PublicationRevokeReason, length=32), nullable=True
    )

    resource: Mapped[Resource] = relationship(back_populates="publications", lazy="raise")

    __table_args__ = (
        # 单资源仅一个现行公开版本（Partial Unique Index）。
        Index(
            "uq_publications_resource_id_current",
            "resource_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        UniqueConstraint("public_no", name="uq_publications_public_no"),
        UniqueConstraint("public_url", name="uq_publications_public_url"),
        CheckConstraint("public_no >= 1", name="public_no_positive"),
        CheckConstraint("resource_version >= 1", name="resource_version_positive"),
        CheckConstraint("acl_version_at_publish >= 0", name="acl_version_at_publish_non_negative"),
        # 撤销原因只在已撤销时才有意义，反之亦然。
        CheckConstraint(
            "(revoked_at IS NULL) = (revoked_reason IS NULL)",
            name="revocation_marker_consistent",
        ),
        CheckConstraint(
            enum_check_expression("revoked_reason", PublicationRevokeReason),
            name="revoked_reason_valid",
        ),
        Index("ix_publications_resource_id_published_at", "resource_id", "created_at"),
    )


class Comment(UUIDPrimaryKey, Timestamped, Versioned, SoftDelete, Base):
    """评论。

    设计要点：

    - 幂等身份 ``(author_id, client_id)`` 唯一：同一客户端重复提交只产生一条。
    - ``request_hash`` 是语义判等键：稳定 JSON 序列化的 SHA-256。
      正文**只**统一 CRLF/CR 为 LF，不 strip、不折叠空白、不做 NFKC。
    - 父评论约束是**跨行不变量**（父存在、父无 parent、resource_id 一致、父未删除）：
      CHECK 不能跨行，普通 FK 只能保证父存在，因此由 Repository 在事务内锁定父行校验。
      DB 侧只能靠 trigger，见 ``tests/integration`` 中的说明与 ``trg_comments_*`` 注释。
    - ``status`` 与 ``deleted_at`` 分工：``hidden`` 是运营撤下，``deleted_at`` 是作者删除；
      公开读取两者都要过滤。
    """

    __tablename__ = "comments"

    resource_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("resources.id", ondelete="CASCADE"), nullable=False
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # 父评论：NULL 表示一级评论；子评论的父必须是同一资源下的一级评论。
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("comments.id", ondelete="CASCADE"), nullable=True
    )

    # 客户端生成的幂等标识（同一作者内唯一）。
    client_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(_HASH_LENGTH), nullable=False)

    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[CommentStatus] = mapped_column(
        enum_column_type(CommentStatus, length=16), nullable=False, default=CommentStatus.VISIBLE
    )
    # 运营撤下原因：主状态不因原因扩张。
    moderation_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    resource: Mapped[Resource] = relationship(back_populates="comments", lazy="raise")

    __table_args__ = (
        UniqueConstraint("author_id", "client_id", name="uq_comments_author_client"),
        CheckConstraint("length(body) > 0", name="body_not_empty"),
        CheckConstraint(enum_check_expression("status", CommentStatus), name="status_valid"),
        # 自己不能是自己的父评论。
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="parent_not_self"),
        Index("ix_comments_resource_id_created_at", "resource_id", "created_at"),
        Index("ix_comments_parent_id_created_at", "parent_id", "created_at"),
        Index("ix_comments_author_id_created_at", "author_id", "created_at"),
    )


class KnowledgeIndex(UUIDPrimaryKey, Timestamped, Base):
    """检索索引片段（pgvector，1024 维）。

    设计要点：

    - **私人版本与公开 publication 分别建索引**，且用 CHECK 把
      ``corpus_kind`` / ``source_kind`` 与具体外键列绑死：

      * ``private`` 必须 ``source_kind='resource_version'`` 且 ``publication_id IS NULL``
      * ``public``  必须 ``source_kind='publication'``   且 ``resource_version_id IS NULL``

      于是"复用私人 chunk 再加 public 标记"在结构上**无法表达**
      （架构文档 §9.2：公开索引只能从公开投影生成）。
    - ``superseded_at`` 标记被新索引取代；检索只使用 ``superseded_at IS NULL`` 的片段。
    - 向量索引用 HNSW + cosine，且带同样的可见性谓词，避免 ANN 越过权限边界。
    """

    __tablename__ = "knowledge_indexes"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("resources.id", ondelete="CASCADE"), nullable=False
    )

    corpus_kind: Mapped[IndexKind] = mapped_column(
        enum_column_type(IndexKind, length=16), nullable=False
    )
    source_kind: Mapped[IndexSourceKind] = mapped_column(
        enum_column_type(IndexSourceKind, length=32), nullable=False
    )
    # 私人来源：指向内容版本。
    resource_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("resource_versions.id", ondelete="CASCADE"), nullable=True
    )
    # 公开发布来源：指向公开投影。
    publication_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("publications.id", ondelete="CASCADE"), nullable=True
    )

    chunk_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    fragment_start: Mapped[int] = mapped_column(Integer, nullable=False)
    fragment_end: Mapped[int] = mapped_column(Integer, nullable=False)

    # 列名就叫 text；类体内的这个属性会遮蔽 sqlalchemy.text，
    # 因此本类的索引谓词一律用 sa.text(...) 构造（见 __table_args__）。
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_hash: Mapped[str] = mapped_column(String(_HASH_LENGTH), nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(128), nullable=False)
    embedding_dimensions: Mapped[int] = mapped_column(Integer, nullable=False)

    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(enum_check_expression("corpus_kind", IndexKind), name="corpus_kind_valid"),
        CheckConstraint(
            enum_check_expression("source_kind", IndexSourceKind), name="source_kind_valid"
        ),
        # 公开索引只能来自公开投影，私人索引只能来自私人版本。
        CheckConstraint(
            "(corpus_kind = 'private' AND source_kind = 'resource_version' "
            "AND resource_version_id IS NOT NULL AND publication_id IS NULL) "
            "OR (corpus_kind = 'public' AND source_kind = 'publication' "
            "AND publication_id IS NOT NULL AND resource_version_id IS NULL)",
            name="corpus_source_consistent",
        ),
        CheckConstraint(
            f"embedding_dimensions = {EMBEDDING_DIMENSIONS}",
            name="embedding_dimensions_matches_column",
        ),
        CheckConstraint("chunk_index >= 0", name="chunk_index_non_negative"),
        CheckConstraint("fragment_start >= 0", name="fragment_start_non_negative"),
        CheckConstraint("fragment_end > fragment_start", name="fragment_range_ordered"),
        UniqueConstraint(
            "corpus_kind",
            "resource_id",
            "chunk_id",
            name="uq_knowledge_indexes_corpus_resource_chunk",
        ),
        Index("ix_knowledge_indexes_resource_id_corpus_kind", "resource_id", "corpus_kind"),
        Index("ix_knowledge_indexes_chunk_id", "chunk_id"),
        Index("ix_knowledge_indexes_owner_id_corpus_kind", "owner_id", "corpus_kind"),
        # ANN 索引刻意带可见性谓词：检索永远不会越过 superseded_at 边界。
        # 这里必须用 sa.text(...)：本类体已用名为 text 的列属性遮蔽了 sqlalchemy.text。
        Index(
            KNOWLEDGE_INDEX_VECTOR_INDEX,
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_where=sa.text("superseded_at IS NULL"),
        ),
    )


class RunSource(UUIDPrimaryKey, Timestamped, Base):
    """实际进入模型的来源记录。

    设计要点：

    - **AppendOnly 语义**：Repository 只暴露 ``record()``，不提供通用 update/delete。
    - 绑定 revision/publication/acl_version/片段位置，使"这次回答依据了哪些资料"
      可被审计复现（架构文档 §9.2）。
    - ``cited`` 标记是否在回答中被引用；``grounding`` 标记是私人语料还是公开语料。
    - ``run_id`` 指向 ``runs``（A6 建立该表）。同一次 run 的同一片段只记录一次。
    """

    __tablename__ = "run_sources"

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )

    grounding: Mapped[RunSourceKind] = mapped_column(
        enum_column_type(RunSourceKind, length=16), nullable=False
    )
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("resources.id", ondelete="SET NULL"), nullable=True
    )
    resource_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("resource_versions.id", ondelete="SET NULL"), nullable=True
    )
    publication_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("publications.id", ondelete="SET NULL"), nullable=True
    )

    # 来源记录必须绑定当时的可见性版本，便于判断 ACL 是否已变化。
    acl_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 引用的索引片段：与 knowledge_indexes.chunk_id 对应。
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    fragment_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fragment_end: Mapped[int | None] = mapped_column(Integer, nullable=True)

    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    cited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 检索/重排的模型与参数版本，便于复现。
    retrieval_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 名为 metadata_json：ORM 的 ``metadata`` 属性已被 SQLAlchemy 占用。
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        CheckConstraint(enum_check_expression("grounding", RunSourceKind), name="grounding_valid"),
        # 来源必须且只能指向一类语料：私人版本或公开投影。
        CheckConstraint(
            "(grounding = 'private' AND publication_id IS NULL) "
            "OR (grounding = 'public' AND resource_version_id IS NULL)",
            name="grounding_target_consistent",
        ),
        CheckConstraint("acl_version >= 0", name="acl_version_non_negative"),
        CheckConstraint(
            "(fragment_start IS NULL) = (fragment_end IS NULL)",
            name="fragment_markers_consistent",
        ),
        CheckConstraint(
            "fragment_end IS NULL OR fragment_end > fragment_start",
            name="fragment_range_ordered",
        ),
        # 同一 run 内同一切片只记录一次。
        UniqueConstraint("run_id", "chunk_id", name="uq_run_sources_run_chunk"),
        Index("ix_run_sources_run_id", "run_id"),
        Index("ix_run_sources_resource_id", "resource_id"),
    )

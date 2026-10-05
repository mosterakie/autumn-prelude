"""第二批模型：保留策略、内容、版本、发布、文件对象、留言与举报。

对应 ``docs/architecture/database.md`` §4、§6、§9，以及迁移批次二：
``retention_policies`` / ``resources`` / ``file_objects`` / ``resource_versions`` /
``publications`` / ``comments`` / ``reports``。

关键结构约束（文档 §4）：

- ``resources.current_revision_id`` 用**可延迟复合外键**指向
  ``resource_versions(resource_id, id)``，确保它指向本资源的版本。
- ``publications`` 用 ``(resource_id, revision_id)`` 复合外键绑定确切原稿版本，
  并以 ``UNIQUE(resource_id, revision_id, id)`` 供索引表严格引用。
- ``comments`` 的 ``resource_id`` **可空**（独立留言板）；父子同资源用
  自引用复合外键保证。

``file_objects`` 是文档批次清单之外、由实施顺序评审要求的补充实体：文件
``staged`` / ``ready`` / ``pending_delete`` 生命周期必须落在**可变**记录上，
而不是反复修改声明不可变的 ``resource_versions``。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from autumn_backend.db.base import Base
from autumn_backend.db.enums import (
    CommentStatus,
    ContentFormat,
    FileObjectStatus,
    ReportStatus,
    ResourceKind,
    RetentionAnchor,
    RetentionMode,
    RetentionScope,
    enum_check_expression,
    enum_column_type,
)
from autumn_backend.db.mixins import (
    Deletable,
    SoftDelete,
    Timestamped,
    UUIDPrimaryKey,
    Versioned,
)

#: 公开投影允许的字段名。``public_fields`` 只能取这个集合的子集。
PUBLIC_FIELD_NAMES: tuple[str, ...] = ("title", "body", "note", "url", "tags")

_PUBLIC_FIELDS_LITERAL = ", ".join(f"'{name}'" for name in PUBLIC_FIELD_NAMES)


class RetentionPolicy(UUIDPrimaryKey, Timestamped, Versioned, Base):
    """保留策略。

    设计要点：

    - ``forever`` 时 ``ttl_days`` 必须为空；``ttl`` 时必须为正。
    - 同一 ``scope`` 与 ``resource_kind`` 只允许一个 ``is_active`` 策略。
      ``resource_kind`` 可空，按文档要求"NULL 值按同一个分类处理"——
      因此拆成两个部分唯一索引：一个管有具体 kind 的策略，一个管通用策略，
      比依赖 ``NULLS NOT DISTINCT`` 更直观且不依赖方言开关。
    - 给历史数据设置 ``expires_at`` 是**可预览的独立作业**，
      不能只修改策略行就默默开始删除。
    """

    __tablename__ = "retention_policies"

    scope: Mapped[RetentionScope] = mapped_column(
        enum_column_type(RetentionScope, length=32), nullable=False
    )
    resource_kind: Mapped[ResourceKind | None] = mapped_column(
        enum_column_type(ResourceKind, length=16), nullable=True
    )
    mode: Mapped[RetentionMode] = mapped_column(
        enum_column_type(RetentionMode, length=16), nullable=False, default=RetentionMode.FOREVER
    )
    ttl_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    anchor: Mapped[RetentionAnchor] = mapped_column(
        enum_column_type(RetentionAnchor, length=16),
        nullable=False,
        default=RetentionAnchor.CREATED_AT,
    )
    applies_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    include_existing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    __table_args__ = (
        CheckConstraint(enum_check_expression("scope", RetentionScope), name="scope_valid"),
        CheckConstraint(
            enum_check_expression("resource_kind", ResourceKind), name="resource_kind_valid"
        ),
        CheckConstraint(enum_check_expression("mode", RetentionMode), name="mode_valid"),
        CheckConstraint(enum_check_expression("anchor", RetentionAnchor), name="anchor_valid"),
        # forever 时 ttl_days 为空；ttl 时**必须给出正值**。
        # 注意必须写成 NULL 安全的形式：若只写 ``(mode='ttl' AND ttl_days > 0)``，
        # 当 ``ttl_days`` 为 NULL 时整个 OR 分支求值为 NULL，CHECK **不会**拦下它，
        # "ttl 模式却没有天数"就会漏过去。
        CheckConstraint(
            "(mode = 'forever' AND ttl_days IS NULL) "
            "OR (mode = 'ttl' AND ttl_days IS NOT NULL AND ttl_days > 0)",
            name="ttl_matches_mode",
        ),
        # 同一 scope + 具体 kind 只允许一个活动策略。
        Index(
            "uq_retention_policies_active_kind",
            "scope",
            "resource_kind",
            unique=True,
            postgresql_where=text("is_active AND resource_kind IS NOT NULL"),
        ),
        # 同一 scope 的通用策略（resource_kind IS NULL）只允许一个活动策略。
        Index(
            "uq_retention_policies_active_global",
            "scope",
            unique=True,
            postgresql_where=text("is_active AND resource_kind IS NULL"),
        ),
    )


class Resource(UUIDPrimaryKey, Timestamped, Versioned, SoftDelete, Base):
    """资源主体。

    设计要点：

    - ``kind`` 创建后不可改（由 service 保证；数据库无历史可查）。
    - ``version`` 是内容/元数据乐观并发版本；``acl_version`` 只在公开范围变化时递增。
      编辑内容**不**递增 ``acl_version``，发布/撤回**不**递增 ``version``。
    - ``slug`` 是对外稳定标识且**全局唯一**；私密状态仍对外返回 404。
    - ``current_revision_id`` 是当前私密版本指针，用可延迟复合外键绑定本资源版本。
    - ``retention_policy_id`` 为空表示继承该类型的默认策略。
    """

    __tablename__ = "resources"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[ResourceKind] = mapped_column(
        enum_column_type(ResourceKind, length=16), nullable=False
    )
    slug: Mapped[str] = mapped_column(String(160), nullable=False)
    current_revision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    # 公开范围版本：publish / revoke / 软删除 / 恢复时递增；与 version 无关。
    acl_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    retention_policy_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("retention_policies.id", ondelete="SET NULL"), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("slug", name="uq_resources_slug"),
        # 支持其他表的复合归属约束（文档 §4）。
        UniqueConstraint("id", "owner_id", name="uq_resources_id_owner_id"),
        CheckConstraint(enum_check_expression("kind", ResourceKind), name="kind_valid"),
        CheckConstraint("length(slug) > 0", name="slug_not_empty"),
        CheckConstraint("acl_version >= 0", name="acl_version_non_negative"),
        # 可延迟复合外键：current_revision 必须属于本资源。
        # use_alter：resource_versions 反向引用 resources，建表顺序上必须后补。
        #
        # 删除动作刻意用 ``NO ACTION`` 而不是 ``SET NULL``：SET NULL 会把**列组里的
        # 每一列**都置空，而 ``id`` 是 NOT NULL 主键，因此 SET NULL 永远无法完成。
        # 语义上"删掉当前版本"本来就该先由 service 重新指向新版本；
        # 删除整个资源时版本随之级联删除，提交时无悬挂引用，约束不会拦。
        ForeignKeyConstraint(
            ["current_revision_id", "id"],
            ["resource_versions.id", "resource_versions.resource_id"],
            name="fk_resources_current_revision_id_resource_versions",
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        Index("ix_resources_owner_id_kind_created_at", "owner_id", "kind", "created_at"),
        # 未删除且未归档资源的可用索引。
        Index(
            "ix_resources_active_owner_id",
            "owner_id",
            postgresql_where=text("deleted_at IS NULL AND archived_at IS NULL"),
        ),
        # 到期清理索引只覆盖 expires_at IS NOT NULL。
        Index(
            "ix_resources_expires_at",
            "expires_at",
            postgresql_where=text("expires_at IS NOT NULL"),
        ),
    )


class FileObject(UUIDPrimaryKey, Timestamped, Versioned, Deletable, Base):
    """对象存储中的文件对象（可变生命周期记录）。

    生命周期：``staged`` → ``ready``；删除时先转 ``pending_delete``，
    物理删除成功后置 ``deleted_at``；失败保留 ``pending_delete`` 以待重试。

    物理 I/O **不参与**数据库事务，因此状态推进由 ``storage.finalize`` /
    ``storage.delete`` job 收敛（阶段 E5/H4）。
    """

    __tablename__ = "file_objects"

    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[FileObjectStatus] = mapped_column(
        enum_column_type(FileObjectStatus, length=16),
        nullable=False,
        default=FileObjectStatus.STAGED,
    )
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(Text, nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(32), nullable=False, default="local")

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("resources.id", ondelete="SET NULL"), nullable=True
    )

    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("object_key", name="uq_file_objects_object_key"),
        CheckConstraint(enum_check_expression("status", FileObjectStatus), name="status_valid"),
        CheckConstraint("length(object_key) > 0", name="object_key_not_empty"),
        CheckConstraint("length(sha256) > 0", name="sha256_not_empty"),
        CheckConstraint("byte_size >= 0", name="byte_size_non_negative"),
        # ready 必须有定稿时刻；未 ready 不得有。
        CheckConstraint(
            "(status = 'ready') = (finalized_at IS NOT NULL)", name="finalized_at_matches_status"
        ),
        Index("ix_file_objects_status_created_at", "status", "created_at"),
        Index("ix_file_objects_resource_id", "resource_id"),
    )


class ResourceVersion(UUIDPrimaryKey, Timestamped, Versioned, Base):
    """内容版本。**创建后不可编辑**，修订产生新版本。

    标题、正文、URL、私密备注、标签都在**版本**上（接口契约的 ``RevisionDTO``
    与 ``POST /api/resources`` 同样把它们作为原稿内容），而不是挂在资源主体上：
    这样已发布版本不会被后续编辑影响。

    "版本创建后不可修改"是 Repository/Service 的规则；数据库侧通过
    "只暴露领域转换方法、不提供通用 update"来保证（阶段 B2）。
    """

    __tablename__ = "resource_versions"

    resource_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("resources.id", ondelete="CASCADE"), nullable=False
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_format: Mapped[ContentFormat] = mapped_column(
        enum_column_type(ContentFormat, length=16),
        nullable=False,
        default=ContentFormat.MARKDOWN,
    )
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    private_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]")
    )
    # 收藏关联网页资料；不因关联而继承公开权限。
    linked_source_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("resources.id", ondelete="SET NULL"), nullable=True
    )
    # 网页元数据（original_url / final_url / fetched_at / content_hash / parser_version）。
    source_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # 文件类版本的可选字段。
    file_object_key: Mapped[str | None] = mapped_column(
        String(512), ForeignKey("file_objects.object_key", ondelete="RESTRICT"), nullable=True
    )
    file_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    byte_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "resource_id", "revision_no", name="uq_resource_versions_resource_revision"
        ),
        # 供 resources.current_revision_id 与 publications 的复合引用。
        UniqueConstraint("resource_id", "id", name="uq_resource_versions_resource_id_id"),
        CheckConstraint(
            enum_check_expression("content_format", ContentFormat), name="content_format_valid"
        ),
        CheckConstraint("revision_no >= 1", name="revision_no_positive"),
        CheckConstraint("byte_size IS NULL OR byte_size >= 0", name="byte_size_non_negative"),
        # 网页元数据必须是对象，不允许塞入数组或标量。
        CheckConstraint(
            "source_metadata IS NULL OR jsonb_typeof(source_metadata) = 'object'",
            name="source_metadata_is_object",
        ),
        # 文件字段全有或全无：避免"半截上传"版本。
        CheckConstraint(
            "(file_object_key IS NULL) = (file_sha256 IS NULL) "
            "AND (file_object_key IS NULL) = (media_type IS NULL) "
            "AND (file_object_key IS NULL) = (byte_size IS NULL)",
            name="file_metadata_consistent",
        ),
        Index("ix_resource_versions_resource_id_created_at", "resource_id", "created_at"),
    )


class Publication(UUIDPrimaryKey, Timestamped, Versioned, Base):
    """公开投影。

    设计要点：

    - **一个资源只能有一个现行公开版本**：``UNIQUE(resource_id) WHERE revoked_at IS NULL``。
    - ``public_fields`` 是白名单子集；**不在其中的字段，其列必须为空**，
      由逐字段 CHECK 保证。这样"公开投影"不是靠 service 记得少填，
      而是数据库不接受越界组合。
    - ``(resource_id, revision_id)`` 复合外键绑定确切原稿版本；
      ``UNIQUE(resource_id, revision_id, id)`` 供知识索引表严格引用。
    - ``ai_enabled`` 决定这一版能否进入普通用户 AI 资料范围；
      ``raw_download_enabled`` 表示**整个原件**可访问，发布前必须明确预览。
    - 公开查询还要连接 resources，要求未归档、未删除。
    """

    __tablename__ = "publications"

    resource_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("resources.id", ondelete="CASCADE"), nullable=False
    )
    # 单独的 revision 外键被下面的复合外键取代：只有复合外键能证明同属一个资源。
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    publication_no: Mapped[int] = mapped_column(Integer, nullable=False)

    public_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    public_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    public_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    public_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    public_tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]")
    )
    public_fields: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]")
    )

    ai_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    raw_download_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    published_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # 发布序号是**资源内**序号（"该资源第几次发布"），不是全局序号。
        UniqueConstraint(
            "resource_id", "publication_no", name="uq_publications_resource_publication"
        ),
        # 供 knowledge_indexes 严格引用（资源 + 版本 + publication）。
        UniqueConstraint(
            "resource_id", "revision_id", "id", name="uq_publications_resource_revision_id"
        ),
        # 单资源仅一个现行公开版本。
        Index(
            "uq_publications_resource_id_current",
            "resource_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        # 复合外键：revision 必须与 resource_id 同属一个资源。
        ForeignKeyConstraint(
            ["resource_id", "revision_id"],
            ["resource_versions.resource_id", "resource_versions.id"],
            name="fk_publications_resource_id_revision_id",
            ondelete="RESTRICT",
        ),
        CheckConstraint("publication_no >= 1", name="publication_no_positive"),
        # public_fields 只能取白名单子集。
        CheckConstraint(
            f"public_fields <@ ARRAY[{_PUBLIC_FIELDS_LITERAL}]::text[]",
            name="public_fields_whitelisted",
        ),
        # 不在 public_fields 中的字段，其列必须为空或空数组。
        CheckConstraint(
            "('title' = ANY(public_fields)) = (public_title IS NOT NULL)",
            name="public_title_matches_fields",
        ),
        CheckConstraint(
            "('body' = ANY(public_fields)) = (public_body IS NOT NULL)",
            name="public_body_matches_fields",
        ),
        CheckConstraint(
            "('note' = ANY(public_fields)) = (public_note IS NOT NULL)",
            name="public_note_matches_fields",
        ),
        CheckConstraint(
            "('url' = ANY(public_fields)) = (public_url IS NOT NULL)",
            name="public_url_matches_fields",
        ),
        CheckConstraint(
            "('tags' = ANY(public_fields)) = (cardinality(public_tags) > 0)",
            name="public_tags_matches_fields",
        ),
        Index("ix_publications_resource_id_published_at", "resource_id", "published_at"),
    )


class Comment(UUIDPrimaryKey, Timestamped, Versioned, Deletable, Base):
    """留言。

    设计要点：

    - ``client_id`` 是 UUID，``UNIQUE(author_id, client_id)`` 用于提交去重；
      同标识不同正文由 service 返回冲突。
    - ``resource_id`` **可空**：为空表示独立留言板。
    - 父子同资源用自引用复合外键保证；"首版只有一级回复"是服务规则。
    - 修改已审核正文要重新进入 ``pending``（service 规则）。
    - 公开读取要求 ``status='approved'`` 且未删除；文章留言还要求文章当前公开。
    """

    __tablename__ = "comments"

    author_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("resources.id", ondelete="CASCADE"), nullable=True
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("comments.id", ondelete="CASCADE"), nullable=True
    )
    client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[CommentStatus] = mapped_column(
        enum_column_type(CommentStatus, length=16),
        nullable=False,
        default=CommentStatus.PENDING,
    )
    moderated_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    moderated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("author_id", "client_id", name="uq_comments_author_client"),
        # 供自引用复合外键使用。
        UniqueConstraint("id", "resource_id", name="uq_comments_id_resource_id"),
        CheckConstraint(enum_check_expression("status", CommentStatus), name="status_valid"),
        CheckConstraint("length(body) > 0", name="body_not_empty"),
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="parent_not_self"),
        # 跨行约束：父留言必须与本条属于同一资源。
        # MATCH SIMPLE：``resource_id`` 为空的留言板回复不受校验。
        ForeignKeyConstraint(
            ["parent_id", "resource_id"],
            ["comments.id", "comments.resource_id"],
            name="fk_comments_parent_id_resource_id",
            ondelete="CASCADE",
        ),
        Index(
            "ix_comments_resource_id_status_created_at",
            "resource_id",
            "status",
            "created_at",
            "id",
        ),
        Index("ix_comments_author_id_created_at", "author_id", "created_at"),
        Index("ix_comments_parent_id", "parent_id"),
    )


class Report(UUIDPrimaryKey, Timestamped, Versioned, Base):
    """留言举报。

    设计要点：

    - 每个用户对同一留言最多一条**未处理**举报：部分唯一索引
      ``(reporter_id, comment_id) WHERE status='open'``。
    - 普通用户只能报告已公开或自己可见的留言，不能枚举私密评论（service 规则）。
    """

    __tablename__ = "reports"

    reporter_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    comment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("comments.id", ondelete="CASCADE"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ReportStatus] = mapped_column(
        enum_column_type(ReportStatus, length=16), nullable=False, default=ReportStatus.OPEN
    )
    handled_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "uq_reports_reporter_id_comment_id_open",
            "reporter_id",
            "comment_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        CheckConstraint(enum_check_expression("status", ReportStatus), name="status_valid"),
        CheckConstraint("length(reason) > 0", name="reason_not_empty"),
        # open 时没有处理时刻；已处理（resolved/dismissed）必须有。
        CheckConstraint(
            "(status = 'open') = (resolved_at IS NULL)", name="resolved_at_matches_status"
        ),
        Index("ix_reports_status_created_at", "status", "created_at"),
        Index("ix_reports_comment_id", "comment_id"),
    )


__all__ = [
    "PUBLIC_FIELD_NAMES",
    "Comment",
    "FileObject",
    "Publication",
    "Report",
    "Resource",
    "ResourceVersion",
    "RetentionPolicy",
]

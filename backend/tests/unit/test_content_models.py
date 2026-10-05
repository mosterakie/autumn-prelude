"""内容与知识索引模型：元数据契约单测（不需要数据库）。

对应 ``docs/architecture/database.md`` §4、§5、§6、§9，以及
``db/models/content.py`` / ``db/models/knowledge.py``。
真库验收在 ``tests/integration/test_content_constraints.py``。

这里断言的是**结构层不变量**：哪些关系由复合外键证明、哪些由 CHECK 表达、
哪些由部分唯一索引承担。它们正是"公开投影不越界""公开索引不来自草稿"这类
安全性质的物理载体。
"""

from __future__ import annotations

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.sql.schema import Table

from autumn_backend.db import models
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
)
from autumn_backend.db.mixins import timestamped_tables
from autumn_backend.db.models import (
    EMBEDDING_DIMENSIONS,
    PUBLIC_FIELD_NAMES,
    Comment,
    FileObject,
    KnowledgeIndex,
    Publication,
    Report,
    Resource,
    ResourceVersion,
    RetentionPolicy,
)

pytestmark = pytest.mark.unit

A5_TABLES = (
    "retention_policies",
    "resources",
    "file_objects",
    "resource_versions",
    "publications",
    "comments",
    "reports",
    "knowledge_indexes",
    "knowledge_chunks",
)


@pytest.fixture(scope="module", autouse=True)
def _registered_models() -> None:
    models.load_all_models()


def table(name: str) -> Table:
    return Base.metadata.tables[name]


def checks(name: str) -> dict[str, str]:
    return {
        c.name: str(c.sqltext)
        for c in table(name).constraints
        if isinstance(c, CheckConstraint) and c.name is not None
    }


def uniques(name: str) -> set[str]:
    return {
        c.name
        for c in table(name).constraints
        if isinstance(c, UniqueConstraint) and c.name is not None
    }


def foreign_keys(name: str) -> set[str]:
    return {
        c.name
        for c in table(name).constraints
        if isinstance(c, ForeignKeyConstraint) and c.name is not None
    }


def composite_fk(name: str, fk_name: str) -> ForeignKeyConstraint:
    return next(
        c
        for c in table(name).constraints
        if isinstance(c, ForeignKeyConstraint) and c.name == fk_name
    )


def index_by_name(name: str, index_name: str) -> Index:
    return next(i for i in table(name).indexes if i.name == index_name)


def partial_predicate(name: str, index_name: str) -> str:
    where = index_by_name(name, index_name).dialect_options["postgresql"]["where"]
    assert where is not None, f"{index_name} 必须是部分索引"
    return str(where)


class TestTableRegistration:
    @pytest.mark.parametrize("name", A5_TABLES)
    def test_table_exists(self, name: str) -> None:
        assert name in Base.metadata.tables

    def test_timestamped_tables(self) -> None:
        """本批的**可变**记录都带 updated_at；分块表是只追加表，单独断言。"""
        mutable = tuple(name for name in A5_TABLES if name != "knowledge_chunks")
        assert set(mutable).issubset({t.name for t in timestamped_tables()})
        assert "knowledge_chunks" not in {t.name for t in timestamped_tables()}

    def test_chunks_are_append_only(self) -> None:
        """分块只追加：没有 updated_at / version / deleted_at。"""
        columns = set(table("knowledge_chunks").columns.keys())
        assert "created_at" in columns
        assert not {"updated_at", "version", "deleted_at"} & columns

    def test_single_ck_prefix(self) -> None:
        for name in A5_TABLES:
            for constraint_name in checks(name):
                assert constraint_name.count("ck_") == 1, constraint_name


class TestRetentionPolicyConstraints:
    def test_enum_checks(self) -> None:
        expressions = checks("retention_policies")
        assert expressions["ck_retention_policies_scope_valid"] == enum_check_expression(
            "scope", RetentionScope
        )
        assert expressions["ck_retention_policies_mode_valid"] == enum_check_expression(
            "mode", RetentionMode
        )
        assert expressions["ck_retention_policies_anchor_valid"] == enum_check_expression(
            "anchor", RetentionAnchor
        )
        assert expressions["ck_retention_policies_resource_kind_valid"] == enum_check_expression(
            "resource_kind", ResourceKind
        )

    def test_ttl_matches_mode(self) -> None:
        """文档 §9：forever 时 ttl_days 为空；ttl 时必须为正。"""
        expression = checks("retention_policies")["ck_retention_policies_ttl_matches_mode"]
        assert "mode = 'forever'" in expression
        assert "ttl_days IS NULL" in expression
        assert "mode = 'ttl'" in expression
        assert "ttl_days > 0" in expression

    def test_ttl_matches_mode_covers_null_ttl(self) -> None:
        """表达式必须显式覆盖 ``ttl_days IS NULL``，否则"ttl 但没有天数"会漏过去。

        反例写法 ``(mode='forever' AND ttl_days IS NULL) OR (mode='ttl' AND ttl_days > 0)``：
        当 ``mode='ttl'`` 且 ``ttl_days IS NULL`` 时整个表达式求值为 NULL，而 PostgreSQL 的
        CHECK 只拒绝 FALSE，因此这一组合会被放行——与文档 §9"ttl 时 ttl_days > 0"不符。
        这条断言把"必须显式写 NOT NULL"钉在模型上，防止回退。
        """
        expression = checks("retention_policies")["ck_retention_policies_ttl_matches_mode"]
        assert "ttl_days > 0" in expression
        assert "ttl_days IS NOT NULL" in expression
        # 守卫的形态：ttl 分支里 IS NOT NULL 必须与 > 0 同时出现。
        ttl_branch = expression[expression.index("mode = 'ttl'") :]
        assert "ttl_days IS NOT NULL" in ttl_branch
        assert "ttl_days > 0" in ttl_branch

    def test_two_active_partial_unique_indexes(self) -> None:
        """同一 scope + kind 只允许一个 active；NULL kind 走独立索引（文档 §9）。"""
        kind_index = index_by_name("retention_policies", "uq_retention_policies_active_kind")
        assert kind_index.unique is True
        assert [c.name for c in kind_index.columns] == ["scope", "resource_kind"]
        kind_predicate = partial_predicate(
            "retention_policies", "uq_retention_policies_active_kind"
        )
        assert "is_active" in kind_predicate
        assert "resource_kind IS NOT NULL" in kind_predicate

        global_index = index_by_name("retention_policies", "uq_retention_policies_active_global")
        assert global_index.unique is True
        assert [c.name for c in global_index.columns] == ["scope"]
        global_predicate = partial_predicate(
            "retention_policies", "uq_retention_policies_active_global"
        )
        assert "is_active" in global_predicate
        assert "resource_kind IS NULL" in global_predicate

    def test_created_by_is_restrict(self) -> None:
        """策略作者不能被删掉后留下无主策略：外键是 RESTRICT。"""
        fk = next(iter(table("retention_policies").c.created_by.foreign_keys))
        assert fk.target_fullname == "users.id"
        assert fk.ondelete == "RESTRICT"


class TestResourceConstraints:
    def test_kind_check_lists_enum_values(self) -> None:
        assert checks("resources")["ck_resources_kind_valid"] == enum_check_expression(
            "kind", ResourceKind
        )

    def test_acl_version_non_negative_and_slug_non_empty(self) -> None:
        expressions = checks("resources")
        assert "acl_version >= 0" in expressions["ck_resources_acl_version_non_negative"]
        assert "length(slug) > 0" in expressions["ck_resources_slug_not_empty"]

    def test_version_separation(self) -> None:
        """``version`` 与 ``acl_version`` 完全解耦（文档 §4）。"""
        columns = set(table("resources").columns.keys())
        assert {"version", "acl_version"}.issubset(columns)
        acl = table("resources").c.acl_version
        assert acl.server_default is None
        assert acl.default is not None  # Python 侧默认 0
        # acl_version 不属于 Versioned Mixin：可见性版本不是行级 CAS 版本。
        from autumn_backend.db.mixins import Versioned

        assert "acl_version" not in Versioned.__dict__

    def test_slug_is_globally_unique(self) -> None:
        """文档 §4：slug 是对外稳定标识且全局唯一（私密状态返回 404 而非改名）。"""
        assert "uq_resources_slug" in uniques("resources")
        unique = next(
            c
            for c in table("resources").constraints
            if isinstance(c, UniqueConstraint) and c.name == "uq_resources_slug"
        )
        assert [c.name for c in unique.columns] == ["slug"]
        # slug 不可空：全局唯一才不会被 NULL 绕过。
        assert table("resources").c.slug.nullable is False

    def test_owner_pair_is_unique_for_composite_references(self) -> None:
        assert "uq_resources_id_owner_id" in uniques("resources")

    def test_current_revision_is_a_deferrable_composite_fk(self) -> None:
        """``current_revision_id`` 必须指向**本资源**的版本（文档 §4）。"""
        fk = composite_fk("resources", "fk_resources_current_revision_id_resource_versions")
        assert [c.name for c in fk.columns] == ["current_revision_id", "id"]
        assert [e.target_fullname for e in fk.elements] == [
            "resource_versions.id",
            "resource_versions.resource_id",
        ]
        assert fk.deferrable is True
        assert fk.initially == "DEFERRED"
        # 可空：新建资源时还没有版本。
        assert table("resources").c.current_revision_id.nullable is True

    def test_soft_delete_has_two_independent_moments(self) -> None:
        columns = set(table("resources").columns.keys())
        assert {"deleted_at", "archived_at"}.issubset(columns)

    def test_visibility_and_cleanup_indexes(self) -> None:
        assert [
            c.name for c in index_by_name("resources", "ix_resources_active_owner_id").columns
        ] == ["owner_id"]
        active_predicate = partial_predicate("resources", "ix_resources_active_owner_id")
        assert "deleted_at IS NULL" in active_predicate
        assert "archived_at IS NULL" in active_predicate
        assert "expires_at IS NOT NULL" in partial_predicate("resources", "ix_resources_expires_at")


class TestFileObjectConstraints:
    def test_status_check(self) -> None:
        assert checks("file_objects")["ck_file_objects_status_valid"] == enum_check_expression(
            "status", FileObjectStatus
        )

    def test_finalized_at_matches_status(self) -> None:
        """ready 必须有定稿时刻；未 ready 不得有（文档 §4 的文件生命周期）。"""
        expression = checks("file_objects")["ck_file_objects_finalized_at_matches_status"]
        assert "(status = 'ready') = (finalized_at IS NOT NULL)" in expression

    def test_object_key_and_digest_guards(self) -> None:
        expressions = checks("file_objects")
        assert "length(object_key) > 0" in expressions["ck_file_objects_object_key_not_empty"]
        assert "length(sha256) > 0" in expressions["ck_file_objects_sha256_not_empty"]
        assert "byte_size >= 0" in expressions["ck_file_objects_byte_size_non_negative"]
        assert "uq_file_objects_object_key" in uniques("file_objects")

    def test_owner_cascades_but_resource_detaches(self) -> None:
        owner_fk = next(iter(table("file_objects").c.owner_id.foreign_keys))
        assert owner_fk.ondelete == "CASCADE"
        resource_fk = next(iter(table("file_objects").c.resource_id.foreign_keys))
        assert resource_fk.ondelete == "SET NULL"

    def test_deletable_but_not_archivable(self) -> None:
        columns = set(table("file_objects").columns.keys())
        assert "deleted_at" in columns
        assert "archived_at" not in columns


class TestResourceVersionConstraints:
    def test_revision_no_is_unique_per_resource(self) -> None:
        assert "uq_resource_versions_resource_revision" in uniques("resource_versions")

    def test_identity_pair_for_composite_references(self) -> None:
        """供 ``resources.current_revision_id`` 与 ``publications`` 复合引用。"""
        assert "uq_resource_versions_resource_id_id" in uniques("resource_versions")

    def test_content_format_check(self) -> None:
        assert checks("resource_versions")["ck_resource_versions_content_format_valid"] == (
            enum_check_expression("content_format", ContentFormat)
        )

    def test_revision_no_and_size_guards(self) -> None:
        expressions = checks("resource_versions")
        assert "revision_no >= 1" in expressions["ck_resource_versions_revision_no_positive"]
        assert (
            "byte_size IS NULL OR byte_size >= 0"
            in expressions["ck_resource_versions_byte_size_non_negative"]
        )

    def test_file_metadata_is_all_or_nothing(self) -> None:
        expression = checks("resource_versions")["ck_resource_versions_file_metadata_consistent"]
        for column in ("file_object_key", "file_sha256", "media_type", "byte_size"):
            assert column in expression

    def test_source_metadata_must_be_an_object(self) -> None:
        expression = checks("resource_versions")["ck_resource_versions_source_metadata_is_object"]
        assert "jsonb_typeof(source_metadata) = 'object'" in expression

    def test_versions_are_immutable_by_construction(self) -> None:
        """不可变版本：没有"当前版本"指针或取代标记，编辑只会新增行。"""
        columns = set(table("resource_versions").columns.keys())
        assert "is_current" not in columns
        assert "superseded_at" not in columns
        assert "current_revision_id" not in columns

    def test_tags_are_text_array_with_empty_default(self) -> None:
        column = table("resource_versions").c.tags
        assert isinstance(column.type, ARRAY)
        assert column.server_default is not None
        assert "'{}'::text[]" in str(column.server_default.arg)

    def test_file_object_reference_is_restrict(self) -> None:
        fk = next(iter(table("resource_versions").c.file_object_key.foreign_keys))
        assert fk.target_fullname == "file_objects.object_key"
        assert fk.ondelete == "RESTRICT"


class TestPublicationConstraints:
    def test_single_current_publication_partial_unique_index(self) -> None:
        index = index_by_name("publications", "uq_publications_resource_id_current")
        assert index.unique is True
        assert [c.name for c in index.columns] == ["resource_id"]
        assert "revoked_at IS NULL" in partial_predicate(
            "publications", "uq_publications_resource_id_current"
        )

    def test_publication_no_is_resource_scoped(self) -> None:
        """``publication_no`` 是"该资源第几次发布"，不是全局序号（文档 §4）。"""
        assert "uq_publications_resource_publication" in uniques("publications")
        unique = next(
            c
            for c in table("publications").constraints
            if isinstance(c, UniqueConstraint) and c.name == "uq_publications_resource_publication"
        )
        assert [c.name for c in unique.columns] == ["resource_id", "publication_no"]
        assert "uq_publications_public_no" not in uniques("publications")

    def test_revision_belongs_to_same_resource_by_composite_fk(self) -> None:
        fk = composite_fk("publications", "fk_publications_resource_id_revision_id")
        assert [c.name for c in fk.columns] == ["resource_id", "revision_id"]
        assert [e.target_fullname for e in fk.elements] == [
            "resource_versions.resource_id",
            "resource_versions.id",
        ]
        assert fk.ondelete == "RESTRICT"
        # 单独的 revision 外键必须已被移除，否则两行仍可来自不同资源。
        assert not {
            fk_name
            for fk_name in foreign_keys("publications")
            if fk_name.endswith("revision_id_resource_versions")
        }

    def test_strict_reference_key_for_knowledge_indexes(self) -> None:
        assert "uq_publications_resource_revision_id" in uniques("publications")

    def test_public_fields_whitelist(self) -> None:
        assert PUBLIC_FIELD_NAMES == ("title", "body", "note", "url", "tags")
        expression = checks("publications")["ck_publications_public_fields_whitelisted"]
        assert "public_fields <@" in expression
        for field in PUBLIC_FIELD_NAMES:
            assert f"'{field}'" in expression

    def test_every_projected_field_has_a_matching_check(self) -> None:
        """不在 ``public_fields`` 里的字段列必须为空：每个字段一条 CHECK。"""
        expressions = checks("publications")
        assert expressions["ck_publications_public_title_matches_fields"] == (
            "('title' = ANY(public_fields)) = (public_title IS NOT NULL)"
        )
        assert expressions["ck_publications_public_body_matches_fields"] == (
            "('body' = ANY(public_fields)) = (public_body IS NOT NULL)"
        )
        assert expressions["ck_publications_public_note_matches_fields"] == (
            "('note' = ANY(public_fields)) = (public_note IS NOT NULL)"
        )
        assert expressions["ck_publications_public_url_matches_fields"] == (
            "('url' = ANY(public_fields)) = (public_url IS NOT NULL)"
        )
        assert expressions["ck_publications_public_tags_matches_fields"] == (
            "('tags' = ANY(public_fields)) = (cardinality(public_tags) > 0)"
        )

    def test_counter_guard_and_publisher_fk(self) -> None:
        assert (
            "publication_no >= 1"
            in checks("publications")["ck_publications_publication_no_positive"]
        )
        fk = next(iter(table("publications").c.published_by.foreign_keys))
        assert fk.ondelete == "RESTRICT"

    def test_ai_and_raw_download_flags_exist(self) -> None:
        columns = set(table("publications").columns.keys())
        assert {"ai_enabled", "raw_download_enabled"}.issubset(columns)
        for name in ("ai_enabled", "raw_download_enabled"):
            assert table("publications").c[name].nullable is False


class TestCommentConstraints:
    def test_idempotency_identity(self) -> None:
        assert "uq_comments_author_client" in uniques("comments")

    def test_body_not_empty(self) -> None:
        assert "length(body) > 0" in checks("comments")["ck_comments_body_not_empty"]

    def test_status_check(self) -> None:
        assert checks("comments")["ck_comments_status_valid"] == enum_check_expression(
            "status", CommentStatus
        )

    def test_parent_cannot_be_self(self) -> None:
        assert "parent_id <> id" in checks("comments")["ck_comments_parent_not_self"]

    def test_cross_row_parent_invariant_is_not_a_check(self) -> None:
        """跨行约束只能由复合外键承担；CHECK 集合里不应有假装能跨行的表达式。"""
        expressions = checks("comments")
        assert set(expressions) == {
            "ck_comments_body_not_empty",
            "ck_comments_status_valid",
            "ck_comments_parent_not_self",
            "ck_comments_request_hash_shape",
        }
        assert expressions["ck_comments_parent_not_self"] == (
            "parent_id IS NULL OR parent_id <> id"
        )
        for expression in expressions.values():
            assert "SELECT" not in expression.upper()
            assert "TRIGGER" not in expression.upper()

    def test_parent_same_resource_is_a_composite_foreign_key(self) -> None:
        fk = composite_fk("comments", "fk_comments_parent_id_resource_id")
        assert [c.name for c in fk.columns] == ["parent_id", "resource_id"]
        assert [e.target_fullname for e in fk.elements] == [
            "comments.id",
            "comments.resource_id",
        ]
        assert "uq_comments_id_resource_id" in uniques("comments")

    def test_resource_id_is_nullable_for_guestbook(self) -> None:
        """独立留言板不挂文章：resource_id 必须可空（文档 §2、§6）。"""
        assert table("comments").c.resource_id.nullable is True

    def test_moderation_columns_exist(self) -> None:
        columns = set(table("comments").columns.keys())
        assert {"moderated_by", "moderated_at"}.issubset(columns)

    def test_lookup_indexes(self) -> None:
        index_names = {i.name for i in table("comments").indexes}
        assert "ix_comments_resource_id_status_created_at" in index_names
        assert "ix_comments_parent_id" in index_names
        assert "ix_comments_author_id_created_at" in index_names


class TestReportConstraints:
    def test_one_open_report_per_reporter_and_comment(self) -> None:
        index = index_by_name("reports", "uq_reports_reporter_id_comment_id_open")
        assert index.unique is True
        assert [c.name for c in index.columns] == ["reporter_id", "comment_id"]
        assert "status = 'open'" in partial_predicate(
            "reports", "uq_reports_reporter_id_comment_id_open"
        )

    def test_status_check_and_resolution_marker(self) -> None:
        expressions = checks("reports")
        assert expressions["ck_reports_status_valid"] == enum_check_expression(
            "status", ReportStatus
        )
        assert expressions["ck_reports_resolved_at_matches_status"] == (
            "(status = 'open') = (resolved_at IS NULL)"
        )
        assert "length(reason) > 0" in expressions["ck_reports_reason_not_empty"]

    def test_handled_by_is_set_null(self) -> None:
        fk = next(iter(table("reports").c.handled_by.foreign_keys))
        assert fk.ondelete == "SET NULL"


class TestKnowledgeIndexConstraints:
    def test_scope_and_status_checks(self) -> None:
        expressions = checks("knowledge_indexes")
        assert expressions["ck_knowledge_indexes_scope_valid"] == ("scope IN ('owner', 'public')")
        assert (
            "status IN ('queued', 'building', 'ready', 'failed', 'retired')"
            == (expressions["ck_knowledge_indexes_status_valid"])
        )

    def test_publication_must_match_scope(self) -> None:
        """scope=public 必须有 publication；owner 必须没有（文档 §5）。"""
        expression = checks("knowledge_indexes")[
            "ck_knowledge_indexes_publication_id_matches_scope"
        ]
        assert "(scope = 'public') = (publication_id IS NOT NULL)" in expression

    def test_active_index_must_be_ready(self) -> None:
        expression = checks("knowledge_indexes")["ck_knowledge_indexes_active_must_be_ready"]
        assert "NOT is_active OR status = 'ready'" in expression

    def test_embedding_dimension_matches_column(self) -> None:
        expression = checks("knowledge_indexes")[
            "ck_knowledge_indexes_embedding_dimension_matches_column"
        ]
        assert (
            expression
            == f"embedding_dimension = {EMBEDDING_DIMENSIONS}"
            == "embedding_dimension = 1024"
        )

    def test_identity_pair_for_reference_from_chunks(self) -> None:
        assert "uq_knowledge_indexes_id_embedding_dimension" in uniques("knowledge_indexes")

    def test_two_active_partial_unique_indexes(self) -> None:
        owner_index = index_by_name("knowledge_indexes", "uq_knowledge_indexes_owner_active")
        assert owner_index.unique is True
        assert [c.name for c in owner_index.columns] == ["revision_id"]
        owner_predicate = partial_predicate(
            "knowledge_indexes", "uq_knowledge_indexes_owner_active"
        )
        assert "is_active" in owner_predicate
        assert "scope = 'owner'" in owner_predicate

        public_index = index_by_name("knowledge_indexes", "uq_knowledge_indexes_public_active")
        assert public_index.unique is True
        assert [c.name for c in public_index.columns] == ["publication_id"]
        public_predicate = partial_predicate(
            "knowledge_indexes", "uq_knowledge_indexes_public_active"
        )
        assert "is_active" in public_predicate
        assert "scope = 'public'" in public_predicate

    def test_composite_foreign_keys(self) -> None:
        revision_fk = composite_fk(
            "knowledge_indexes", "fk_knowledge_indexes_resource_id_revision_id"
        )
        assert [c.name for c in revision_fk.columns] == ["resource_id", "revision_id"]
        assert [e.target_fullname for e in revision_fk.elements] == [
            "resource_versions.resource_id",
            "resource_versions.id",
        ]
        publication_fk = composite_fk(
            "knowledge_indexes", "fk_knowledge_indexes_resource_revision_publication"
        )
        assert [c.name for c in publication_fk.columns] == [
            "resource_id",
            "revision_id",
            "publication_id",
        ]
        assert [e.target_fullname for e in publication_fk.elements] == [
            "publications.resource_id",
            "publications.revision_id",
            "publications.id",
        ]

    def test_no_vector_column_on_the_metadata_table(self) -> None:
        """向量在分块表上：索引元数据与向量两层分开（文档 §5）。"""
        assert "embedding" not in table("knowledge_indexes").columns
        assert "embedding" in table("knowledge_chunks").columns


class TestKnowledgeChunkConstraints:
    def test_vector_column_dimensions(self) -> None:
        column = table("knowledge_chunks").c.embedding
        assert column.type.dim == EMBEDDING_DIMENSIONS == 1024
        assert column.nullable is False

    def test_chunk_no_unique_per_index(self) -> None:
        assert "uq_knowledge_chunks_index_chunk_no" in uniques("knowledge_chunks")

    def test_no_approximate_index_is_declared(self) -> None:
        """文档 §5 明确"首版精确向量查询，不建近似索引"。

        这是一处**有意偏离**常规向量检索实现：任何建在 ``embedding`` 列上的
        hnsw / ivfflat 索引都属于漂移。
        """
        for name in A5_TABLES:
            for index in table(name).indexes:
                assert "hnsw" not in index.name
                assert "ivfflat" not in index.name
                indexed_columns = {column.name for column in index.columns}
                assert "embedding" not in indexed_columns, f"{name}.{index.name} 建在向量列上"

    def test_content_and_locator_guards(self) -> None:
        expressions = checks("knowledge_chunks")
        assert "chunk_no >= 0" in expressions["ck_knowledge_chunks_chunk_no_non_negative"]
        assert (
            "length(content_text) > 0" in expressions["ck_knowledge_chunks_content_text_not_empty"]
        )
        assert (
            "jsonb_typeof(locator) = 'object'"
            in expressions["ck_knowledge_chunks_locator_is_object"]
        )

    def test_index_lookup_is_served_by_the_unique_constraint(self) -> None:
        """按 ``index_id`` 的检索由唯一约束的索引承担，不重复建同列索引。

        文档 §5 要求 ``knowledge_chunks`` 有 ``B-tree(index_id)``；
        ``UNIQUE(index_id, chunk_no)`` 建出的复合索引以 ``index_id`` 为前导列，
        前缀查询直接可用。再建一个同列的普通索引只是冗余（写放大、占空间），
        因此这里同时钉住"唯一约束存在且列序正确"与"没有重复索引"。
        """
        assert "uq_knowledge_chunks_index_chunk_no" in uniques("knowledge_chunks")
        chunk_table = table("knowledge_chunks")
        unique = next(
            c
            for c in chunk_table.constraints
            if isinstance(c, UniqueConstraint) and c.name == "uq_knowledge_chunks_index_chunk_no"
        )
        assert [column.name for column in unique.columns] == ["index_id", "chunk_no"]
        assert all(
            index.name != "ix_knowledge_chunks_index_id_chunk_no" for index in chunk_table.indexes
        ), "唯一约束已提供同列索引，不应再有重复的普通索引"


class TestModelShapeInvariants:
    """跨表的模型形状断言：Mixin 组合与"显式加载"纪律。"""

    def test_versioned_tables_use_bigint(self) -> None:
        from sqlalchemy import BigInteger

        for name in (
            "retention_policies",
            "resources",
            "file_objects",
            "resource_versions",
            "publications",
            "comments",
            "reports",
            "knowledge_indexes",
        ):
            assert isinstance(table(name).c.version.type, BigInteger), name

    @pytest.mark.parametrize(
        ("model", "table_name"),
        [
            (RetentionPolicy, "retention_policies"),
            (Resource, "resources"),
            (FileObject, "file_objects"),
            (ResourceVersion, "resource_versions"),
            (Publication, "publications"),
            (Comment, "comments"),
            (Report, "reports"),
            (KnowledgeIndex, "knowledge_indexes"),
        ],
    )
    def test_model_maps_to_expected_table(self, model: type, table_name: str) -> None:
        assert model.__tablename__ == table_name

    def test_models_declare_no_implicit_lazy_loading(self) -> None:
        """模型层不声明 relationship：所有跨表读取必须显式查询。

        这是旧契约 ``lazy="raise"`` 纪律在新契约下的等价保证——没有 relationship
        就没有可能在持有事务时被隐式触发 I/O。
        """
        for model in (
            RetentionPolicy,
            Resource,
            FileObject,
            ResourceVersion,
            Publication,
            Comment,
            Report,
            KnowledgeIndex,
        ):
            assert not list(model.__mapper__.relationships), f"{model.__name__} 声明了 relationship"

    def test_publication_projection_columns_are_nullable(self) -> None:
        """投影列可空是"白名单之外必须为空"能表达的前提。"""
        for name in ("public_title", "public_body", "public_note", "public_url"):
            assert table("publications").c[name].nullable is True
        assert isinstance(table("publications").c.public_fields.type, ARRAY)
        assert isinstance(table("resource_versions").c.source_metadata.type, JSONB)

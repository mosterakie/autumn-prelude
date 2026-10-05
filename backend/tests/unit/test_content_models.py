"""A5 第二批模型：元数据契约单测（不需要数据库）。

真库验收在 ``tests/integration/test_content_constraints.py``。
"""

from __future__ import annotations

import pytest
from sqlalchemy import CheckConstraint, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.schema import Table

from autumn_backend.db import models
from autumn_backend.db.base import Base
from autumn_backend.db.enums import (
    CommentStatus,
    ContentFormat,
    IndexKind,
    IndexSourceKind,
    PublicationRevokeReason,
    ResourceType,
    RunSourceKind,
)
from autumn_backend.db.mixins import timestamped_tables
from autumn_backend.db.models import (
    EMBEDDING_DIMENSIONS,
    KNOWLEDGE_INDEX_VECTOR_INDEX,
    Resource,
    ResourceVersion,
)

pytestmark = pytest.mark.unit

A5_TABLES = (
    "resources",
    "resource_versions",
    "publications",
    "comments",
    "knowledge_indexes",
    "run_sources",
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


def index_by_name(name: str, index_name: str) -> Index:
    return next(i for i in table(name).indexes if i.name == index_name)


class TestTableRegistration:
    @pytest.mark.parametrize("name", A5_TABLES)
    def test_table_exists(self, name: str) -> None:
        assert name in Base.metadata.tables

    def test_timestamped_tables(self) -> None:
        assert set(A5_TABLES).issubset({t.name for t in timestamped_tables()})

    def test_single_ck_prefix(self) -> None:
        for name in A5_TABLES:
            for constraint_name in checks(name):
                assert constraint_name.count("ck_") == 1, constraint_name


class TestVersionSeparation:
    """``resources.version`` 与 ``acl_version`` 必须完全解耦。"""

    def test_both_columns_exist(self) -> None:
        columns = set(table("resources").columns.keys())
        assert "version" in columns
        assert "acl_version" in columns

    def test_acl_version_is_not_on_the_versioned_mixin(self) -> None:
        assert "acl_version" not in models.__dict__
        from autumn_backend.db.mixins import Versioned

        assert "acl_version" not in Versioned.__dict__

    def test_acl_version_has_no_relationship_to_version(self) -> None:
        """两个版本列各自独立，不存在"一个派生另一个"的默认值或约束。"""
        acl = table("resources").c.acl_version
        version = table("resources").c.version
        assert acl.name != version.name
        assert acl.server_default is None
        assert acl.default is not None  # Python 侧默认 0，新建资源可见性版本为 0

    def test_publication_records_acl_version_at_publish(self) -> None:
        columns = set(table("publications").columns.keys())
        assert "acl_version_at_publish" in columns
        assert "resource_version" in columns

    def test_resources_are_soft_deletable(self) -> None:
        columns = set(table("resources").columns.keys())
        assert {"deleted_at", "archived_at"}.issubset(columns)


class TestResourceConstraints:
    def test_type_check_lists_enum_values(self) -> None:
        expression = checks("resources")["ck_resources_type_valid"]
        for value in ResourceType:
            assert f"'{value.value}'" in expression

    def test_acl_version_non_negative(self) -> None:
        assert "acl_version >= 0" in checks("resources")["ck_resources_acl_version_non_negative"]

    def test_tags_is_jsonb_with_empty_array_default(self) -> None:
        column = table("resources").c.tags
        assert isinstance(column.type, JSONB)
        assert column.server_default is not None
        assert "'[]'::jsonb" in str(column.server_default.arg)

    def test_private_note_column_exists(self) -> None:
        assert "private_note" in table("resources").columns

    def test_slug_unique_per_owner_only_when_present(self) -> None:
        index = index_by_name("resources", "uq_resources_owner_id_slug")
        assert index.unique is True
        where = index.dialect_options["postgresql"]["where"]
        assert where is not None
        assert "slug IS NOT NULL" in str(where)

    def test_public_read_index_covers_soft_delete_flags(self) -> None:
        index = index_by_name("resources", "ix_resources_deleted_at_archived_at")
        assert [c.name for c in index.columns] == ["deleted_at", "archived_at"]


class TestResourceVersionConstraints:
    def test_version_no_unique_per_resource(self) -> None:
        assert "uq_resource_versions_resource_version_no" in uniques("resource_versions")

    def test_content_format_check(self) -> None:
        expression = checks("resource_versions")["ck_resource_versions_content_format_valid"]
        for value in ContentFormat:
            assert f"'{value.value}'" in expression

    def test_version_no_positive_and_size_non_negative(self) -> None:
        expressions = checks("resource_versions")
        assert "version_no >= 1" in expressions["ck_resource_versions_version_no_positive"]
        assert (
            "size_bytes IS NULL OR size_bytes >= 0"
            in expressions["ck_resource_versions_size_bytes_non_negative"]
        )

    def test_storage_metadata_is_all_or_nothing(self) -> None:
        expression = checks("resource_versions")["ck_resource_versions_storage_metadata_consistent"]
        assert "storage_key IS NULL" in expression
        assert "mime_type IS NULL" in expression
        assert "size_bytes IS NULL" in expression

    def test_versions_are_immutable_by_construction(self) -> None:
        """不可变版本：没有指向"当前版本"的可变指针列，编辑只会新增行。"""
        columns = set(table("resource_versions").columns.keys())
        assert "is_current" not in columns
        assert "superseded_at" not in columns


class TestPublicationConstraints:
    def test_single_current_publication_partial_unique_index(self) -> None:
        index = index_by_name("publications", "uq_publications_resource_id_current")
        assert index.unique is True
        assert [c.name for c in index.columns] == ["resource_id"]
        where = index.dialect_options["postgresql"]["where"]
        assert where is not None
        assert "revoked_at IS NULL" in str(where)

    def test_public_no_and_url_are_unique(self) -> None:
        unique_names = uniques("publications")
        assert "uq_publications_public_no" in unique_names
        assert "uq_publications_public_url" in unique_names

    def test_revocation_marker_consistency(self) -> None:
        expression = checks("publications")["ck_publications_revocation_marker_consistent"]
        assert "revoked_at IS NULL" in expression
        assert "revoked_reason IS NULL" in expression

    def test_revoke_reason_check(self) -> None:
        expression = checks("publications")["ck_publications_revoked_reason_valid"]
        for value in PublicationRevokeReason:
            assert f"'{value.value}'" in expression

    def test_payload_is_jsonb_and_public_url_present(self) -> None:
        assert isinstance(table("publications").c.payload.type, JSONB)
        assert "public_url" in table("publications").columns

    def test_resource_version_fk_is_restrict(self) -> None:
        """已发布的内容版本不能被删掉：否则公开投影会指向空洞。"""
        fk = next(iter(table("publications").c.resource_version_id.foreign_keys))
        assert fk.target_fullname == "resource_versions.id"
        assert fk.ondelete == "RESTRICT"

    def test_counter_guards(self) -> None:
        expressions = checks("publications")
        assert "public_no >= 1" in expressions["ck_publications_public_no_positive"]
        assert "resource_version >= 1" in expressions["ck_publications_resource_version_positive"]
        assert (
            expressions["ck_publications_acl_version_at_publish_non_negative"]
            == "acl_version_at_publish >= 0"
        )


class TestCommentConstraints:
    def test_idempotency_identity(self) -> None:
        assert "uq_comments_author_client" in uniques("comments")

    def test_body_not_empty(self) -> None:
        assert "length(body) > 0" in checks("comments")["ck_comments_body_not_empty"]

    def test_status_check(self) -> None:
        expression = checks("comments")["ck_comments_status_valid"]
        for value in CommentStatus:
            assert f"'{value.value}'" in expression

    def test_parent_cannot_be_self(self) -> None:
        assert "parent_id <> id" in checks("comments")["ck_comments_parent_not_self"]

    def test_cross_row_parent_invariant_is_not_a_check(self) -> None:
        """父评论跨行不变量无法用 CHECK 表达，因此**不得**假装有约束。

        它由 Repository 在事务内锁定父行校验（阶段 B8）；DB 侧若要兜底只能靠 trigger。
        本用例把这件事写成断言：comments 的 CHECK 集合里**只有**同行的自引用检查，
        没有任何试图校验"父是一级评论 / 父属于同一资源"的表达式。
        """
        expressions = checks("comments")
        assert set(expressions) == {
            "ck_comments_body_not_empty",
            "ck_comments_status_valid",
            "ck_comments_parent_not_self",
        }
        # 同行约束：只禁止自引用。
        assert expressions["ck_comments_parent_not_self"] == "parent_id IS NULL OR parent_id <> id"
        # 不存在跨行校验的痕迹。
        for expression in expressions.values():
            assert "resource_id" not in expression
            assert "SELECT" not in expression.upper()
            assert "TRIGGER" not in expression.upper()

    def test_lookup_indexes(self) -> None:
        index_names = {i.name for i in table("comments").indexes}
        assert "ix_comments_resource_id_created_at" in index_names
        assert "ix_comments_parent_id_created_at" in index_names
        assert "ix_comments_author_id_created_at" in index_names


class TestKnowledgeIndexConstraints:
    def test_vector_column_dimensions(self) -> None:
        column = table("knowledge_indexes").c.embedding
        assert column.type.dim == EMBEDDING_DIMENSIONS == 1024

    def test_corpus_kind_and_source_kind_checks(self) -> None:
        expressions = checks("knowledge_indexes")
        for value in IndexKind:
            assert f"'{value.value}'" in expressions["ck_knowledge_indexes_corpus_kind_valid"]
        for value in IndexSourceKind:
            assert f"'{value.value}'" in expressions["ck_knowledge_indexes_source_kind_valid"]

    def test_corpus_source_is_structurally_bound(self) -> None:
        """公开索引只能来自公开投影：CHECK 把 corpus_kind 与具体外键列绑死。"""
        expression = checks("knowledge_indexes")["ck_knowledge_indexes_corpus_source_consistent"]
        assert "corpus_kind = 'private'" in expression
        assert "source_kind = 'resource_version'" in expression
        assert "publication_id IS NULL" in expression
        assert "corpus_kind = 'public'" in expression
        assert "source_kind = 'publication'" in expression
        assert "resource_version_id IS NULL" in expression

    def test_embedding_dimensions_column_must_match(self) -> None:
        expression = checks("knowledge_indexes")[
            "ck_knowledge_indexes_embedding_dimensions_matches_column"
        ]
        assert expression == f"embedding_dimensions = {EMBEDDING_DIMENSIONS}"

    def test_fragment_range_ordered(self) -> None:
        expressions = checks("knowledge_indexes")
        assert (
            "fragment_end > fragment_start"
            in expressions["ck_knowledge_indexes_fragment_range_ordered"]
        )
        assert (
            "fragment_start >= 0" in expressions["ck_knowledge_indexes_fragment_start_non_negative"]
        )
        assert "chunk_index >= 0" in expressions["ck_knowledge_indexes_chunk_index_non_negative"]

    def test_chunk_uniqueness_per_corpus(self) -> None:
        assert "uq_knowledge_indexes_corpus_resource_chunk" in uniques("knowledge_indexes")

    def test_vector_index_is_hnsw_cosine_with_visibility_predicate(self) -> None:
        index = index_by_name("knowledge_indexes", KNOWLEDGE_INDEX_VECTOR_INDEX)
        options = index.dialect_options["postgresql"]
        assert options["using"] == "hnsw"
        assert options["ops"] == {"embedding": "vector_cosine_ops"}
        assert options["with"] == {"m": 16, "ef_construction": 64}
        assert "superseded_at IS NULL" in str(options["where"])

    def test_hnsw_requires_an_immutable_predicate_column(self) -> None:
        """HNSW 的部分索引谓词只能用不可变函数/普通列。

        用 ``is_active`` 这类布尔列也行，但 ``superseded_at`` 更贴合"被取代"的语义，
        并且让检索谓词与 CHECK 语义一致。
        """
        assert "superseded_at" in table("knowledge_indexes").columns
        assert "is_active" not in table("knowledge_indexes").columns

    def test_superseded_at_nulls_first_for_retrieval(self) -> None:
        assert table("knowledge_indexes").c.superseded_at.nullable is True

    def test_chunk_text_column_is_named_text(self) -> None:
        """列名就是 ``text``：类体内的属性遮蔽 ``sqlalchemy.text``，
        因此本类的部分索引谓词改用 ``sa.text(...)``（见模型注释）。"""
        from autumn_backend.db.models import CHUNK_TEXT_COLUMN

        column = table("knowledge_indexes").c[CHUNK_TEXT_COLUMN]
        assert column.key == "text"
        assert column.name == "text"

    def test_vector_index_predicate_is_a_sql_expression(self) -> None:
        """谓词必须是 SQL 表达式；若被列属性遮蔽成字符串，索引将退化为全表索引。"""
        index = index_by_name("knowledge_indexes", KNOWLEDGE_INDEX_VECTOR_INDEX)
        where = index.dialect_options["postgresql"]["where"]
        assert where is not None
        assert not isinstance(where, str)
        compiled = str(where.compile(compile_kwargs={"literal_binds": True}))
        assert compiled == "superseded_at IS NULL"


class TestRunSourceConstraints:
    def test_append_only_shape(self) -> None:
        """AppendOnly：没有 deleted_at / 状态列这类可改字段。"""
        columns = set(table("run_sources").columns.keys())
        assert "deleted_at" not in columns
        assert "status" not in columns
        assert "updated_by" not in columns

    def test_grounding_kind_check(self) -> None:
        expression = checks("run_sources")["ck_run_sources_grounding_valid"]
        for value in RunSourceKind:
            assert f"'{value.value}'" in expression

    def test_grounding_target_consistency(self) -> None:
        expression = checks("run_sources")["ck_run_sources_grounding_target_consistent"]
        assert "grounding = 'private'" in expression
        assert "publication_id IS NULL" in expression
        assert "grounding = 'public'" in expression
        assert "resource_version_id IS NULL" in expression

    def test_chunk_unique_per_run(self) -> None:
        assert "uq_run_sources_run_chunk" in uniques("run_sources")

    def test_fragment_markers_are_all_or_nothing(self) -> None:
        expressions = checks("run_sources")
        assert (
            "(fragment_start IS NULL) = (fragment_end IS NULL)"
            in expressions["ck_run_sources_fragment_markers_consistent"]
        )
        assert (
            "fragment_end > fragment_start" in expressions["ck_run_sources_fragment_range_ordered"]
        )

    def test_records_acl_version_for_audit(self) -> None:
        assert "acl_version" in table("run_sources").columns
        assert (
            "acl_version >= 0" in checks("run_sources")["ck_run_sources_acl_version_non_negative"]
        )

    def test_run_id_references_runs(self) -> None:
        """A6 建立 runs 表后补上外键；删除 run 时来源记录随之消失。"""
        fk = next(iter(table("run_sources").c.run_id.foreign_keys))
        assert fk.target_fullname == "runs.id"
        assert fk.ondelete == "CASCADE"

    def test_metadata_column_avoids_reserved_name(self) -> None:
        columns = set(table("run_sources").columns.keys())
        assert "metadata_json" in columns
        assert "metadata" not in columns


class TestRelationshipsDeclared:
    def test_resource_relationships(self) -> None:
        for attribute in ("versions", "publications", "comments"):
            assert hasattr(Resource, attribute)

    def test_relationships_are_raise_on_lazy_load(self) -> None:
        """``lazy="raise"`` 强制显式加载，避免在持有事务时意外触发 I/O。"""
        for relationship in Resource.__mapper__.relationships:
            assert relationship.lazy == "raise", relationship.key
        for relationship in ResourceVersion.__mapper__.relationships:
            assert relationship.lazy == "raise", relationship.key

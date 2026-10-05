"""内容、公开投影、留言与知识索引表：真实 PostgreSQL 约束验收。

对应 ``docs/architecture/database.md`` §4、§5、§6、§9。重点是结构性不变量：

1. ``resources.current_revision_id`` 只能指向**本资源**的版本（可延迟复合外键）；
2. ``publications`` 的 ``public_fields`` 白名单与逐字段 CHECK——不在白名单里的
   字段列必须为空；
3. ``comments`` 的父子同资源复合外键与 ``reports`` 的"每用户每留言一条 open"；
4. ``knowledge_indexes`` 的 scope/publication 一致性、活动索引唯一性与 ready 要求；
5. ``file_objects`` / ``retention_policies`` 的生命周期与部分唯一索引。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from autumn_backend.db.enums import (
    CommentStatus,
    ContentFormat,
    FileObjectStatus,
    IndexScope,
    IndexStatus,
    ResourceKind,
    RetentionAnchor,
    RetentionMode,
    RetentionScope,
)
from autumn_backend.db.models import (
    EMBEDDING_DIMENSIONS,
    PUBLIC_FIELD_NAMES,
    Comment,
    FileObject,
    KnowledgeChunk,
    KnowledgeIndex,
    Publication,
    Report,
    Resource,
    ResourceVersion,
    RetentionPolicy,
)

pytestmark = pytest.mark.integration

UTC = dt.UTC

#: (public_fields 成员名, 投影列名, 该列的非空取值)
PUBLIC_FIELD_MATRIX: tuple[tuple[str, str, Any], ...] = (
    ("title", "public_title", "公开标题"),
    ("body", "public_body", "公开正文"),
    ("note", "public_note", "公开备注"),
    ("url", "public_url", "https://example.com/p"),
    ("tags", "public_tags", ["tag1"]),
)

#: 每条 CHECK 的完整约束名，用于断言"错误来自这条约束"。
PUBLIC_FIELD_CHECK: dict[str, str] = {
    "title": "ck_publications_public_title_matches_fields",
    "body": "ck_publications_public_body_matches_fields",
    "note": "ck_publications_public_note_matches_fields",
    "url": "ck_publications_public_url_matches_fields",
    "tags": "ck_publications_public_tags_matches_fields",
}

TIMESTAMPED = (
    "retention_policies",
    "resources",
    "file_objects",
    "resource_versions",
    "publications",
    "comments",
    "reports",
    "knowledge_indexes",
)


async def _expect_error(
    session: AsyncSession, message: str | None = None, *, statement: Any = None
) -> str:
    with pytest.raises((IntegrityError, DBAPIError, StatementError)) as excinfo:
        if statement is not None:
            await session.execute(statement)
        await session.flush()
    raw = str(excinfo.value)
    if message is not None:
        assert message in raw, raw
    return raw


def _vector(value: float = 0.0) -> list[float]:
    return [value] * EMBEDDING_DIMENSIONS


async def _retention_policy(
    session: AsyncSession, creator_id: uuid.UUID, **overrides: Any
) -> RetentionPolicy:
    policy = RetentionPolicy(
        scope=overrides.pop("scope", RetentionScope.RESOURCES),
        mode=overrides.pop("mode", RetentionMode.FOREVER),
        anchor=overrides.pop("anchor", RetentionAnchor.CREATED_AT),
        created_by=creator_id,
        **overrides,
    )
    session.add(policy)
    await session.flush()
    return policy


async def _index(
    session: AsyncSession,
    resource: Resource,
    revision: ResourceVersion,
    **overrides: Any,
) -> KnowledgeIndex:
    """构造知识索引元数据；默认是 owner scope 的未激活索引。"""
    index = KnowledgeIndex(
        resource_id=resource.id,
        revision_id=revision.id,
        scope=overrides.pop("scope", IndexScope.OWNER),
        embedding_provider=overrides.pop("embedding_provider", "bailian"),
        embedding_model=overrides.pop("embedding_model", "text-embedding-v3"),
        embedding_dimension=overrides.pop("embedding_dimension", EMBEDDING_DIMENSIONS),
        content_hash=overrides.pop("content_hash", "c" * 64),
        **overrides,
    )
    session.add(index)
    await session.flush()
    return index


class TestSchemaObjectsExist:
    async def test_new_tables_present(self, session: AsyncSession) -> None:
        names = [
            "retention_policies",
            "resources",
            "file_objects",
            "resource_versions",
            "publications",
            "comments",
            "reports",
            "knowledge_indexes",
            "knowledge_chunks",
        ]
        rows = await session.execute(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename = ANY(:names)"
            ),
            {"names": names},
        )
        assert {row[0] for row in rows} == set(names)

    async def test_updated_at_triggers_installed(self, session: AsyncSession) -> None:
        rows = await session.execute(
            text(
                "SELECT c.relname, t.tgenabled FROM pg_trigger t "
                "JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE c.relnamespace = 'public'::regnamespace AND NOT t.tgisinternal "
                "AND t.tgname LIKE 'trg%set_updated_at'"
            )
        )
        found = {row[0]: bytes(row[1]).decode() for row in rows}
        for name in TIMESTAMPED:
            assert found.get(name) == "O", f"{name} 缺少已启用的 updated_at 触发器"
        # 只追加的分块表不挂时间戳触发器。
        assert "knowledge_chunks" not in found

    async def test_no_approximate_vector_index_installed(self, session: AsyncSession) -> None:
        """文档 §5：首版精确向量查询，不建近似索引——索引方法必须仍是 btree。"""
        row = (
            await session.execute(
                text(
                    "SELECT am.amname FROM pg_index i "
                    "JOIN pg_class c ON c.oid = i.indexrelid "
                    "JOIN pg_class t ON t.oid = i.indrelid "
                    "JOIN pg_am am ON am.oid = c.relam "
                    "WHERE t.relname = 'knowledge_chunks'"
                )
            )
        ).all()
        assert row, "knowledge_chunks 至少要有 (index_id, chunk_no) 索引"
        for (access_method,) in row:
            assert access_method == "btree", f"出现了近似向量索引：{access_method}"

    async def test_partial_unique_indexes_installed(self, session: AsyncSession) -> None:
        # 注意：不能写 ``:name::regclass``——asyncpg 不接受在绑定参数上直接加类型转换。
        expected = {
            "uq_publications_resource_id_current": "revoked_at IS NULL",
            "uq_reports_reporter_id_comment_id_open": "'open'",
            "uq_knowledge_indexes_owner_active": "is_active",
            "uq_knowledge_indexes_public_active": "is_active",
            "uq_retention_policies_active_kind": "resource_kind IS NOT NULL",
            "uq_retention_policies_active_global": "resource_kind IS NULL",
            "ix_resources_active_owner_id": "archived_at IS NULL",
        }
        for name, fragment in expected.items():
            predicate = await session.scalar(
                text(
                    "SELECT pg_get_expr(i.indpred, i.indrelid) "
                    "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                    "WHERE c.relname = :name"
                ),
                {"name": name},
            )
            assert predicate is not None, f"{name} 不存在或不是部分索引"
            assert fragment in predicate, f"{name} 的谓词缺少 {fragment}：{predicate}"


class TestRetentionPolicyConstraints:
    async def test_forever_requires_null_ttl(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="rp1@example.com")
        await session.flush()
        session.add(
            RetentionPolicy(
                scope=RetentionScope.RESOURCES,
                mode=RetentionMode.FOREVER,
                ttl_days=30,
                created_by=user.id,
            )
        )
        await _expect_error(session, "ck_retention_policies_ttl_matches_mode")

    async def test_ttl_requires_positive_ttl(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """``ttl`` 模式却没有天数必须被拒。

        这条容易写成假的安全断言：``(mode='ttl' AND ttl_days > 0)`` 在 ``ttl_days``
        为 NULL 时求值为 NULL，而 PostgreSQL 的 CHECK 只拒绝 FALSE，
        因此"ttl + 无天数"会漏过去。表达式必须显式写 ``ttl_days IS NOT NULL``。
        """
        user = make_user(email="rp2@example.com")
        await session.flush()
        session.add(
            RetentionPolicy(
                scope=RetentionScope.RESOURCES,
                mode=RetentionMode.TTL,
                ttl_days=None,
                created_by=user.id,
            )
        )
        await _expect_error(session, "ck_retention_policies_ttl_matches_mode")

    async def test_ttl_must_be_strictly_positive(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="rp3@example.com")
        await session.flush()
        session.add(
            RetentionPolicy(
                scope=RetentionScope.RESOURCES,
                mode=RetentionMode.TTL,
                ttl_days=0,
                created_by=user.id,
            )
        )
        await _expect_error(session, "ck_retention_policies_ttl_matches_mode")

    async def test_both_modes_are_representable(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="rp4@example.com")
        await session.flush()
        await _retention_policy(session, user.id)
        await _retention_policy(
            session,
            user.id,
            scope=RetentionScope.CONVERSATIONS,
            mode=RetentionMode.TTL,
            ttl_days=90,
        )

    async def test_only_one_active_policy_per_scope_and_kind(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="rp5@example.com")
        await session.flush()
        await _retention_policy(session, user.id, resource_kind=ResourceKind.ARTICLE)

        session.add(
            RetentionPolicy(
                scope=RetentionScope.RESOURCES,
                resource_kind=ResourceKind.ARTICLE,
                mode=RetentionMode.FOREVER,
                created_by=user.id,
            )
        )
        await _expect_error(session, "uq_retention_policies_active_kind")

    async def test_inactive_policy_frees_the_slot(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """Partial Unique Index 只约束 ``is_active`` 的行：历史策略可以共存。"""
        user = make_user(email="rp6@example.com")
        await session.flush()
        await _retention_policy(
            session, user.id, resource_kind=ResourceKind.ARTICLE, is_active=False
        )
        await _retention_policy(session, user.id, resource_kind=ResourceKind.ARTICLE)

    async def test_only_one_active_generic_policy_per_scope(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """``resource_kind IS NULL`` 的策略按"同一个分类"处理（文档 §9）。"""
        user = make_user(email="rp7@example.com")
        await session.flush()
        await _retention_policy(session, user.id, scope=RetentionScope.AUDIT_EVENTS)

        session.add(
            RetentionPolicy(
                scope=RetentionScope.AUDIT_EVENTS,
                resource_kind=None,
                mode=RetentionMode.FOREVER,
                created_by=user.id,
            )
        )
        await _expect_error(session, "uq_retention_policies_active_global")

    async def test_generic_and_kind_specific_policies_coexist(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="rp8@example.com")
        await session.flush()
        await _retention_policy(session, user.id, scope=RetentionScope.RESOURCES)
        await _retention_policy(
            session, user.id, scope=RetentionScope.RESOURCES, resource_kind=ResourceKind.BOOKMARK
        )

    async def test_policy_author_cannot_be_deleted(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """``created_by`` 是 RESTRICT：删账号不能留下无主策略。"""
        user = make_user(email="rp9@example.com")
        await session.flush()
        await _retention_policy(session, user.id)
        await _expect_error(
            session,
            "fk_retention_policies_created_by_users",
            statement=text("DELETE FROM users WHERE id = :id").bindparams(id=user.id),
        )


class TestResourceConstraints:
    async def test_version_columns_default_to_zero(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="r-acl@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        assert resource.acl_version == 0
        assert resource.version == 0

    async def test_acl_version_changes_without_touching_version(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
    ) -> None:
        """publish / revoke 只递增 acl_version，绝不伪造内容版本变化。"""
        user = make_user(email="r-sep@example.com")
        await session.flush()
        resource = await make_resource(user.id)

        await session.execute(
            text("UPDATE resources SET acl_version = acl_version + 1 WHERE id = :id"),
            {"id": resource.id},
        )
        await session.refresh(resource)
        assert resource.acl_version == 1
        assert resource.version == 0

    async def test_acl_version_cannot_be_negative(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="r-neg@example.com")
        await session.flush()
        await _expect_error(
            session,
            "ck_resources_acl_version_non_negative",
            statement=text(
                "INSERT INTO resources (owner_id, kind, slug, acl_version, version) "
                "VALUES (:owner, 'article', 'neg-acl', -1, 0)"
            ).bindparams(owner=user.id),
        )

    async def test_kind_must_be_whitelisted(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="r-kind@example.com")
        await session.flush()
        await _expect_error(
            session,
            "ck_resources_kind_valid",
            statement=text(
                "INSERT INTO resources (owner_id, kind, slug, acl_version, version) "
                "VALUES (:owner, 'video', 'bad-kind', 0, 0)"
            ).bindparams(owner=user.id),
        )

    async def test_slug_is_globally_unique_across_owners(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
    ) -> None:
        """文档 §4：slug 是**全局**唯一标识，不同作者也不能撞名。"""
        first = make_user(email="slug1@example.com")
        second = make_user(email="slug2@example.com")
        await session.flush()
        await make_resource(first.id, slug="shared-slug")

        session.add(Resource(owner_id=second.id, kind=ResourceKind.ARTICLE, slug="shared-slug"))
        await _expect_error(session, "uq_resources_slug")

    async def test_slug_cannot_be_empty(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="slug-empty@example.com")
        await session.flush()
        await _expect_error(
            session,
            "ck_resources_slug_not_empty",
            statement=text(
                "INSERT INTO resources (owner_id, kind, slug, acl_version, version) "
                "VALUES (:owner, 'article', '', 0, 0)"
            ).bindparams(owner=user.id),
        )

    async def test_current_revision_may_point_to_own_version(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
    ) -> None:
        """正例：可延迟复合外键指向本资源版本时，强制检查立即通过。"""
        user = make_user(email="cur1@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)

        await session.execute(
            text("UPDATE resources SET current_revision_id = :vid WHERE id = :rid"),
            {"vid": revision.id, "rid": resource.id},
        )
        # 把延迟约束提前到当前时刻检查：通过则证明 (revision, resource) 组合合法。
        await session.execute(
            text("SET CONSTRAINTS fk_resources_current_revision_id_resource_versions IMMEDIATE")
        )
        stored = await session.scalar(
            text("SELECT current_revision_id FROM resources WHERE id = :rid"),
            {"rid": resource.id},
        )
        assert stored == revision.id

    async def test_current_revision_cannot_point_to_another_resources_version(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
    ) -> None:
        """核心防线：``current_revision_id`` 不得跨资源（文档 §4 的可延迟复合外键）。"""
        user = make_user(email="cur2@example.com")
        await session.flush()
        first = await make_resource(user.id)
        second = await make_resource(user.id)
        foreign_revision = await make_resource_version(second)

        await session.execute(
            text("UPDATE resources SET current_revision_id = :vid WHERE id = :rid"),
            {"vid": foreign_revision.id, "rid": first.id},
        )
        # 延迟约束在提交时才会报错；这里显式提前到 IMMEDIATE 才能在同事务内观察。
        await _expect_error(
            session,
            "fk_resources_current_revision_id_resource_versions",
            statement=text(
                "SET CONSTRAINTS fk_resources_current_revision_id_resource_versions IMMEDIATE"
            ),
        )

    async def test_deleting_the_current_revision_is_refused_while_it_is_pointed_to(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
    ) -> None:
        """``(current_revision_id, id)`` 是 NO ACTION：不允许把指针指向已删除的版本。

        复合外键的列组含 NOT NULL 主键 ``id``，因此 ``SET NULL`` 永远无法完成
        （会把主键也置空）。正确语义是"先由 service 重新指向新版本，再删旧版本"，
        约束负责拒绝静默留下悬挂指针。
        """
        user = make_user(email="cur3@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)
        await session.execute(
            text("UPDATE resources SET current_revision_id = :vid WHERE id = :rid"),
            {"vid": revision.id, "rid": resource.id},
        )

        await session.execute(
            text("SAVEPOINT probe"),
        )
        with pytest.raises((IntegrityError, DBAPIError, StatementError)) as excinfo:
            await session.execute(
                text("DELETE FROM resource_versions WHERE id = :id"), {"id": revision.id}
            )
            # 该外键是 DEFERRABLE INITIALLY DEFERRED：默认在提交时校验，
            # 因此必须显式提前触发，否则这里观察不到任何错误。
            await session.execute(
                text("SET CONSTRAINTS fk_resources_current_revision_id_resource_versions IMMEDIATE")
            )
        await session.execute(text("ROLLBACK TO SAVEPOINT probe"))
        assert "fk_resources_current_revision_id_resource_versions" in str(excinfo.value)

    async def test_deleting_the_current_revision_succeeds_after_repointing(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
    ) -> None:
        """重新指向新版本之后，旧版本可以正常删除。"""
        user = make_user(email="cur4@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        old_revision = await make_resource_version(resource, revision_no=1)
        new_revision = await make_resource_version(resource, revision_no=2)
        await session.execute(
            text("UPDATE resources SET current_revision_id = :vid WHERE id = :rid"),
            {"vid": old_revision.id, "rid": resource.id},
        )

        await session.execute(
            text("UPDATE resources SET current_revision_id = :vid WHERE id = :rid"),
            {"vid": new_revision.id, "rid": resource.id},
        )
        await session.execute(
            text("DELETE FROM resource_versions WHERE id = :id"), {"id": old_revision.id}
        )
        await session.refresh(resource)
        assert resource.current_revision_id == new_revision.id

    async def test_deleting_the_resource_removes_its_versions(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
    ) -> None:
        """删除整个资源时版本级联删除：提交时没有悬挂引用，NO ACTION 不会拦。

        这条用例证明"把 SET NULL 改成 NO ACTION"没有把级联删除一起破坏。
        """
        user = make_user(email="cur5@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)
        await session.execute(
            text("UPDATE resources SET current_revision_id = :vid WHERE id = :rid"),
            {"vid": revision.id, "rid": resource.id},
        )

        await session.execute(text("DELETE FROM resources WHERE id = :id"), {"id": resource.id})
        remaining = await session.scalar(
            text("SELECT count(1) FROM resource_versions WHERE resource_id = :id"),
            {"id": resource.id},
        )
        assert remaining == 0

    async def test_soft_delete_and_archive_are_independent(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="r-soft@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        await session.execute(
            text("UPDATE resources SET archived_at = now() WHERE id = :id"), {"id": resource.id}
        )
        await session.refresh(resource)
        assert resource.archived_at is not None
        assert resource.deleted_at is None

    async def test_deleting_owner_cascades_resources(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="r-cascade@example.com")
        await session.flush()
        await make_resource(user.id)
        await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
        remaining = await session.scalar(
            text("SELECT count(*) FROM resources WHERE owner_id = :uid"), {"uid": user.id}
        )
        assert remaining == 0


class TestFileObjectConstraints:
    async def _file_object(
        self, session: AsyncSession, owner_id: uuid.UUID, **overrides: Any
    ) -> FileObject:
        file_object = FileObject(
            owner_id=owner_id,
            object_key=overrides.pop("object_key", f"objects/{uuid.uuid4().hex}"),
            status=overrides.pop("status", FileObjectStatus.STAGED),
            sha256=overrides.pop("sha256", "d" * 64),
            media_type=overrides.pop("media_type", "application/pdf"),
            byte_size=overrides.pop("byte_size", 1024),
            **overrides,
        )
        session.add(file_object)
        await session.flush()
        return file_object

    async def test_ready_requires_finalized_at(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="fo1@example.com")
        await session.flush()
        session.add(
            FileObject(
                owner_id=user.id,
                object_key="objects/ready-without-time",
                status=FileObjectStatus.READY,
                sha256="d" * 64,
                media_type="application/pdf",
                byte_size=10,
            )
        )
        await _expect_error(session, "ck_file_objects_finalized_at_matches_status")

    async def test_unready_cannot_have_finalized_at(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="fo2@example.com")
        await session.flush()
        session.add(
            FileObject(
                owner_id=user.id,
                object_key="objects/staged-with-time",
                status=FileObjectStatus.STAGED,
                sha256="d" * 64,
                media_type="application/pdf",
                byte_size=10,
                finalized_at=dt.datetime.now(UTC),
            )
        )
        await _expect_error(session, "ck_file_objects_finalized_at_matches_status")

    async def test_ready_with_finalized_at_is_accepted(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="fo3@example.com")
        await session.flush()
        file_object = await self._file_object(
            session,
            user.id,
            status=FileObjectStatus.READY,
            finalized_at=dt.datetime.now(UTC),
        )
        assert file_object.status is FileObjectStatus.READY

    async def test_object_key_is_unique(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="fo4@example.com")
        await session.flush()
        await self._file_object(session, user.id, object_key="objects/dup")
        session.add(
            FileObject(
                owner_id=user.id,
                object_key="objects/dup",
                sha256="e" * 64,
                media_type="text/plain",
                byte_size=1,
            )
        )
        await _expect_error(session, "uq_file_objects_object_key")

    async def test_digest_and_size_guards(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="fo5@example.com")
        await session.flush()
        session.add(
            FileObject(
                owner_id=user.id,
                object_key="objects/empty-digest",
                sha256="",
                media_type="text/plain",
                byte_size=1,
            )
        )
        await _expect_error(session, "ck_file_objects_sha256_not_empty")

    async def test_byte_size_cannot_be_negative(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="fo6@example.com")
        await session.flush()
        session.add(
            FileObject(
                owner_id=user.id,
                object_key="objects/neg-size",
                sha256="d" * 64,
                media_type="text/plain",
                byte_size=-1,
            )
        )
        await _expect_error(session, "ck_file_objects_byte_size_non_negative")

    async def test_deleting_resource_detaches_the_file_object(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
    ) -> None:
        """文件生命周期不随资源删除消失：外键是 SET NULL。"""
        user = make_user(email="fo7@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        file_object = await self._file_object(session, user.id, resource_id=resource.id)

        await session.execute(text("DELETE FROM resources WHERE id = :id"), {"id": resource.id})
        await session.refresh(file_object)
        assert file_object.resource_id is None


class TestResourceVersionConstraints:
    async def test_revision_no_is_unique_per_resource(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="v1@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        await make_resource_version(resource, revision_no=1)

        session.add(
            ResourceVersion(
                resource_id=resource.id,
                revision_no=1,
                content_format=ContentFormat.PLAIN,
                body_text="重复版本号",
            )
        )
        await _expect_error(session, "uq_resource_versions_resource_revision")

    async def test_same_revision_no_allowed_for_another_resource(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="v2@example.com")
        await session.flush()
        first = await make_resource(user.id)
        second = await make_resource(user.id)
        await make_resource_version(first, revision_no=1)
        await make_resource_version(second, revision_no=1)

    async def test_revision_no_must_be_positive(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="v3@example.com")
        await session.flush()
        await _expect_error(
            session,
            "ck_resource_versions_revision_no_positive",
            statement=text(
                "INSERT INTO resource_versions (resource_id, revision_no, content_format, "
                "title, body_text, version) "
                "VALUES (gen_random_uuid(), 0, 'plain', 't', 'b', 0)"
            ),
        )
        assert user is not None

    async def test_content_format_must_be_whitelisted(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="v4@example.com")
        await session.flush()
        await _expect_error(
            session,
            "ck_resource_versions_content_format_valid",
            statement=text(
                "INSERT INTO resource_versions (resource_id, revision_no, content_format, "
                "title, body_text, version) "
                "VALUES (gen_random_uuid(), 1, 'docx', 't', 'b', 0)"
            ),
        )
        assert user is not None

    async def test_file_metadata_must_be_all_or_nothing(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="v5@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        await _expect_error(
            session,
            "ck_resource_versions_file_metadata_consistent",
            statement=text(
                "INSERT INTO resource_versions (resource_id, revision_no, content_format, "
                "title, file_object_key, file_sha256, media_type, byte_size, version) "
                "VALUES (:rid, 1, 'plain', 't', 'objects/x', :hash, NULL, NULL, 0)"
            ).bindparams(rid=resource.id, hash="h" * 64),
        )

    async def test_source_metadata_must_be_an_object(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="v6@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        await _expect_error(
            session,
            "ck_resource_versions_source_metadata_is_object",
            statement=text(
                "INSERT INTO resource_versions (resource_id, revision_no, content_format, "
                "title, source_metadata, version) "
                "VALUES (:rid, 1, 'markdown', 't', '[1, 2]'::jsonb, 0)"
            ).bindparams(rid=resource.id),
        )

    async def test_byte_size_cannot_be_negative(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="v7@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        await _expect_error(
            session,
            "ck_resource_versions_byte_size_non_negative",
            statement=text(
                "INSERT INTO resource_versions (resource_id, revision_no, content_format, "
                "title, file_object_key, file_sha256, media_type, byte_size, version) "
                "VALUES (:rid, 1, 'plain', 't', 'objects/y', :hash, 'text/plain', -1, 0)"
            ).bindparams(rid=resource.id, hash="h" * 64),
        )

    async def test_referenced_file_object_cannot_be_deleted(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
    ) -> None:
        """文件对象被版本引用时删除必须被拒绝（RESTRICT），否则版本会指向空洞。"""
        user = make_user(email="v8@example.com")
        await session.flush()
        resource = await make_resource(user.id, kind=ResourceKind.DOCUMENT)
        file_object = FileObject(
            owner_id=user.id,
            object_key="objects/referenced",
            status=FileObjectStatus.READY,
            sha256="f" * 64,
            media_type="application/pdf",
            byte_size=42,
            finalized_at=dt.datetime.now(UTC),
        )
        session.add(file_object)
        await session.flush()
        session.add(
            ResourceVersion(
                resource_id=resource.id,
                revision_no=1,
                content_format=ContentFormat.PLAIN,
                title="文档版本",
                file_object_key=file_object.object_key,
                file_sha256="f" * 64,
                media_type="application/pdf",
                byte_size=42,
            )
        )
        await session.flush()
        await _expect_error(
            session,
            "fk_resource_versions_file_object_key_file_objects",
            statement=text("DELETE FROM file_objects WHERE id = :id").bindparams(id=file_object.id),
        )

    async def test_versions_are_immutable_and_keep_full_content(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
    ) -> None:
        """标题/正文/标签都在**版本**上：编辑只新增版本行。"""
        user = make_user(email="v9@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource, title="第一版", tags=["python"])
        await session.refresh(revision)
        assert revision.title == "第一版"
        assert revision.tags == ["python"]
        assert revision.revision_no == 1


class TestPublicationConstraints:
    async def test_only_one_current_publication_per_resource(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
        make_publication: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="p1@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        first = await make_resource_version(resource)
        await make_publication(resource, first, publication_no=1)

        second = await make_resource_version(resource, revision_no=2)
        session.add(
            Publication(
                resource_id=resource.id,
                revision_id=second.id,
                publication_no=2,
                published_by=user.id,
                public_fields=[],
            )
        )
        await _expect_error(session, "uq_publications_resource_id_current")

    async def test_revoked_publication_frees_the_slot(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
        make_publication: Callable[..., Awaitable[Any]],
    ) -> None:
        """先撤销再发布：这正是"重新发布"的事务形状。"""
        user = make_user(email="p2@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        first_revision = await make_resource_version(resource)
        first = await make_publication(resource, first_revision, publication_no=1)

        await session.execute(
            text("UPDATE publications SET revoked_at = now() WHERE id = :id"),
            {"id": first.id},
        )

        second_revision = await make_resource_version(resource, revision_no=2)
        await make_publication(resource, second_revision, publication_no=2)

        active = await session.scalar(
            text(
                "SELECT count(*) FROM publications WHERE resource_id = :rid AND revoked_at IS NULL"
            ),
            {"rid": resource.id},
        )
        assert active == 1

    async def test_publication_no_is_scoped_to_one_resource(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
        make_publication: Callable[..., Awaitable[Any]],
    ) -> None:
        """发布序号是资源内序号：另一个资源用同一序号必须被允许。"""
        user = make_user(email="p3@example.com")
        await session.flush()
        first = await make_resource(user.id)
        second = await make_resource(user.id)
        first_revision = await make_resource_version(first)
        second_revision = await make_resource_version(second)
        await make_publication(first, first_revision, publication_no=7)
        await make_publication(second, second_revision, publication_no=7)

        total = await session.scalar(text("SELECT count(*) FROM publications"))
        assert total == 2

    async def test_duplicate_publication_no_rejected_within_one_resource(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
        make_publication: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="p4@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)
        first = await make_publication(resource, revision, publication_no=1)
        # 先撤销现行版本，避免触发"单资源一个现行公开版本"的部分唯一索引。
        await session.execute(
            text("UPDATE publications SET revoked_at = now() WHERE id = :id"),
            {"id": first.id},
        )

        second_revision = await make_resource_version(resource, revision_no=2)
        session.add(
            Publication(
                resource_id=resource.id,
                revision_id=second_revision.id,
                publication_no=1,
                published_by=user.id,
                public_fields=[],
            )
        )
        await _expect_error(session, "uq_publications_resource_publication")

    async def test_revision_of_another_resource_is_rejected(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
    ) -> None:
        """复合外键：``(resource_id, revision_id)`` 必须指向同一资源的版本。"""
        user = make_user(email="p5@example.com")
        await session.flush()
        first = await make_resource(user.id)
        second = await make_resource(user.id)
        foreign_revision = await make_resource_version(second)

        session.add(
            Publication(
                resource_id=first.id,
                revision_id=foreign_revision.id,
                publication_no=1,
                published_by=user.id,
                public_fields=[],
            )
        )
        await _expect_error(session, "fk_publications_resource_id_revision_id")

    async def test_matching_revision_is_accepted(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
        make_publication: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="p6@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)
        publication = await make_publication(resource, revision)
        assert publication.revision_id == revision.id

    async def test_public_fields_whitelist_is_enforced(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
    ) -> None:
        """``public_fields`` 只能取白名单子集——否则公开投影可被任意扩张。"""
        user = make_user(email="p7@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)
        session.add(
            Publication(
                resource_id=resource.id,
                revision_id=revision.id,
                publication_no=1,
                published_by=user.id,
                public_fields=["private_note"],
            )
        )
        await _expect_error(session, "ck_publications_public_fields_whitelisted")

    @pytest.mark.parametrize(("field", "column", "value"), PUBLIC_FIELD_MATRIX)
    async def test_projectable_field_requires_whitelist_membership(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
        make_publication: Callable[..., Awaitable[Any]],
        field: str,
        column: str,
        value: Any,
    ) -> None:
        """每个公开字段的正反例：在 public_fields 中才允许有值，否则必须为空。"""
        assert field in PUBLIC_FIELD_NAMES
        user = make_user(email=f"pf-{field}@example.com")
        await session.flush()

        # 模板只公开这一列；其余投影列必须留空。
        values: dict[str, Any] = {column: value}
        if field != "title":
            values["public_title"] = None

        allowed_resource = await make_resource(user.id)
        allowed_revision = await make_resource_version(allowed_resource)
        allowed = await make_publication(
            allowed_resource,
            allowed_revision,
            public_fields=[field],
            **values,
        )
        assert getattr(allowed, column) == value

        rejected_resource = await make_resource(user.id)
        rejected_revision = await make_resource_version(rejected_resource)
        rejected_values: dict[str, Any] = {column: value}
        if field != "title":
            rejected_values["public_title"] = None
        session.add(
            Publication(
                resource_id=rejected_resource.id,
                revision_id=rejected_revision.id,
                publication_no=1,
                published_by=user.id,
                public_fields=[],
                **rejected_values,
            )
        )
        await _expect_error(session, PUBLIC_FIELD_CHECK[field])

    async def test_published_revision_cannot_be_deleted(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
        make_publication: Callable[..., Awaitable[Any]],
    ) -> None:
        """已发布版本不能被删掉：否则公开投影会指向空洞（RESTRICT）。"""
        user = make_user(email="p8@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)
        await make_publication(resource, revision)

        await _expect_error(
            session,
            "fk_publications_resource_id_revision_id",
            statement=text("DELETE FROM resource_versions WHERE id = :id").bindparams(
                id=revision.id
            ),
        )

    async def test_publisher_cannot_be_deleted_while_publication_lives(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
        make_publication: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="p9@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)
        await make_publication(resource, revision, published_by=user.id)
        await _expect_error(
            session,
            "fk_publications_published_by_users",
            statement=text("DELETE FROM users WHERE id = :id").bindparams(id=user.id),
        )

    async def test_ai_and_raw_download_flags_default_to_false(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
        make_publication: Callable[..., Awaitable[Any]],
    ) -> None:
        """默认不进入 AI 资料范围、默认不允许下载原件。"""
        user = make_user(email="p10@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)
        publication = await make_publication(resource, revision)
        assert publication.ai_enabled is False
        assert publication.raw_download_enabled is False


class TestCommentConstraints:
    async def _comment(
        self, session: AsyncSession, author_id: uuid.UUID, **overrides: Any
    ) -> Comment:
        comment = Comment(
            author_id=author_id,
            client_id=overrides.pop("client_id", uuid.uuid4()),
            body=overrides.pop("body", "留言正文"),
            **overrides,
        )
        session.add(comment)
        await session.flush()
        return comment

    async def test_author_client_id_is_the_idempotency_identity(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="c1@example.com")
        await session.flush()
        client_id = uuid.uuid4()
        await self._comment(session, user.id, client_id=client_id)
        session.add(Comment(author_id=user.id, client_id=client_id, body="重放"))
        await _expect_error(session, "uq_comments_author_client")

    async def test_same_client_id_allowed_for_different_authors(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        first = make_user(email="c2@example.com")
        second = make_user(email="c3@example.com")
        await session.flush()
        client_id = uuid.uuid4()
        await self._comment(session, first.id, client_id=client_id)
        await self._comment(session, second.id, client_id=client_id)

    async def test_empty_body_is_rejected(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="c4@example.com")
        await session.flush()
        session.add(Comment(author_id=user.id, client_id=uuid.uuid4(), body=""))
        await _expect_error(session, "ck_comments_body_not_empty")

    async def test_status_must_be_whitelisted(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="c5@example.com")
        await session.flush()
        await _expect_error(
            session,
            "ck_comments_status_valid",
            statement=text(
                "INSERT INTO comments (author_id, client_id, body, status, version) "
                "VALUES (:aid, gen_random_uuid(), 'x', 'visible', 0)"
            ).bindparams(aid=user.id),
        )

    async def test_parent_cannot_be_itself(self, session: AsyncSession) -> None:
        comment_id = uuid.uuid4()
        await _expect_error(
            session,
            "ck_comments_parent_not_self",
            statement=text(
                "INSERT INTO comments (id, author_id, parent_id, client_id, body, status, version) "
                "VALUES (:cid, gen_random_uuid(), :cid, gen_random_uuid(), 'x', 'pending', 0)"
            ).bindparams(cid=comment_id),
        )

    async def test_reply_must_share_the_parent_resource(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
    ) -> None:
        """跨行约束：父子必须同资源，由复合自引用外键保证（文档 §6）。"""
        user = make_user(email="c6@example.com")
        await session.flush()
        first = await make_resource(user.id)
        second = await make_resource(user.id)
        parent = await self._comment(session, user.id, resource_id=first.id)

        session.add(
            Comment(
                author_id=user.id,
                resource_id=second.id,
                parent_id=parent.id,
                client_id=uuid.uuid4(),
                body="跨资源回复",
            )
        )
        await _expect_error(session, "fk_comments_parent_id_resource_id")

    async def test_reply_in_the_same_resource_is_accepted(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="c7@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        parent = await self._comment(session, user.id, resource_id=resource.id)
        reply = await self._comment(session, user.id, resource_id=resource.id, parent_id=parent.id)
        assert reply.parent_id == parent.id

    async def test_guestbook_comment_without_resource(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """留言板留言：``resource_id`` 为空也必须能落库。"""
        user = make_user(email="c8@example.com")
        await session.flush()
        comment = await self._comment(session, user.id, resource_id=None)
        assert comment.resource_id is None

    async def test_guestbook_reply_is_not_cross_checked(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
    ) -> None:
        """MATCH SIMPLE：``resource_id`` 为空时复合外键**不校验**。

        这是设计决定的直接后果：留言板回复的资源归属由 Repository/Service 在事务内
        校验。本用例把该边界写成可执行文档——数据库放行，服务层就必须自己拦。
        """
        user = make_user(email="c9@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        parent = await self._comment(session, user.id, resource_id=resource.id)
        reply = await self._comment(session, user.id, resource_id=None, parent_id=parent.id)
        assert reply.resource_id is None

    async def test_deeper_nesting_is_not_enforced_by_database(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
    ) -> None:
        """首版只有一级回复是**服务规则**：数据库不阻止"回复的回复"。"""
        user = make_user(email="c10@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        top = await self._comment(session, user.id, resource_id=resource.id)
        child = await self._comment(session, user.id, resource_id=resource.id, parent_id=top.id)
        grandchild = await self._comment(
            session, user.id, resource_id=resource.id, parent_id=child.id
        )
        assert grandchild.id is not None

    async def test_deleting_resource_cascades_to_comments(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="c11@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        await self._comment(session, user.id, resource_id=resource.id)
        await session.execute(text("DELETE FROM resources WHERE id = :id"), {"id": resource.id})
        remaining = await session.scalar(
            text("SELECT count(*) FROM comments WHERE resource_id = :rid"), {"rid": resource.id}
        )
        assert remaining == 0

    async def test_default_status_is_pending(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="c12@example.com")
        await session.flush()
        comment = await self._comment(session, user.id)
        assert comment.status is CommentStatus.PENDING


class TestReportConstraints:
    async def _comment(self, session: AsyncSession, author_id: uuid.UUID) -> Comment:
        comment = Comment(author_id=author_id, client_id=uuid.uuid4(), body="被举报的留言")
        session.add(comment)
        await session.flush()
        return comment

    async def test_one_open_report_per_reporter_and_comment(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        reporter = make_user(email="rep1@example.com")
        author = make_user(email="rep2@example.com")
        await session.flush()
        comment = await self._comment(session, author.id)

        session.add(Report(reporter_id=reporter.id, comment_id=comment.id, reason="垃圾信息"))
        await session.flush()
        session.add(Report(reporter_id=reporter.id, comment_id=comment.id, reason="重复举报"))
        await _expect_error(session, "uq_reports_reporter_id_comment_id_open")

    async def test_handled_report_frees_the_slot(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """部分唯一索引只约束 ``status='open'``：处理完之后可以再举报。"""
        reporter = make_user(email="rep3@example.com")
        author = make_user(email="rep4@example.com")
        await session.flush()
        comment = await self._comment(session, author.id)

        first = Report(reporter_id=reporter.id, comment_id=comment.id, reason="垃圾信息")
        session.add(first)
        await session.flush()
        await session.execute(
            text("UPDATE reports SET status = 'resolved', resolved_at = now() WHERE id = :id"),
            {"id": first.id},
        )
        session.add(Report(reporter_id=reporter.id, comment_id=comment.id, reason="再次出现"))
        await session.flush()

    async def test_different_comments_are_independent(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        reporter = make_user(email="rep5@example.com")
        author = make_user(email="rep6@example.com")
        await session.flush()
        for _ in range(2):
            comment = await self._comment(session, author.id)
            session.add(Report(reporter_id=reporter.id, comment_id=comment.id, reason="垃圾信息"))
        await session.flush()

    async def test_status_must_be_whitelisted(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        reporter = make_user(email="rep7@example.com")
        author = make_user(email="rep8@example.com")
        await session.flush()
        comment = await self._comment(session, author.id)
        await _expect_error(
            session,
            "ck_reports_status_valid",
            statement=text(
                # resolved_at 非空：先让"处理时刻与状态一致"这条 CHECK 通过，
                # 才能真正打到状态白名单这条 CHECK。
                "INSERT INTO reports (reporter_id, comment_id, reason, status, resolved_at, version) "
                "VALUES (:rid, :cid, 'x', 'pending', now(), 0)"
            ).bindparams(rid=reporter.id, cid=comment.id),
        )

    async def test_resolution_timestamp_must_match_status(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """open 不能有处理时刻；已处理必须有（文档 §6）。"""
        reporter = make_user(email="rep9@example.com")
        author = make_user(email="rep10@example.com")
        await session.flush()
        comment = await self._comment(session, author.id)
        await _expect_error(
            session,
            "ck_reports_resolved_at_matches_status",
            statement=text(
                "INSERT INTO reports (reporter_id, comment_id, reason, status, resolved_at, version) "
                "VALUES (:rid, :cid, 'x', 'resolved', NULL, 0)"
            ).bindparams(rid=reporter.id, cid=comment.id),
        )

    async def test_empty_reason_is_rejected(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        reporter = make_user(email="rep11@example.com")
        author = make_user(email="rep12@example.com")
        await session.flush()
        comment = await self._comment(session, author.id)
        session.add(Report(reporter_id=reporter.id, comment_id=comment.id, reason=""))
        await _expect_error(session, "ck_reports_reason_not_empty")


class TestKnowledgeIndexConstraints:
    async def test_owner_scope_must_not_bind_a_publication(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
        make_publication: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="k1@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)
        publication = await make_publication(resource, revision)

        await _expect_error(
            session,
            "ck_knowledge_indexes_publication_id_matches_scope",
            statement=text(
                "INSERT INTO knowledge_indexes (resource_id, revision_id, publication_id, "
                "scope, embedding_provider, embedding_model, embedding_dimension, generation, "
                "status, is_active, content_hash, version) "
                "VALUES (:rid, :vid, :pid, 'owner', 'p', 'm', 1024, 1, 'queued', false, 'h', 0)"
            ).bindparams(rid=resource.id, vid=revision.id, pid=publication.id),
        )

    async def test_public_scope_must_bind_a_publication(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
    ) -> None:
        """文档 §5：``scope='public'`` 必须绑定当时的 publication。"""
        user = make_user(email="k2@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)

        await _expect_error(
            session,
            "ck_knowledge_indexes_publication_id_matches_scope",
            statement=text(
                "INSERT INTO knowledge_indexes (resource_id, revision_id, scope, "
                "embedding_provider, embedding_model, embedding_dimension, generation, "
                "status, is_active, content_hash, version) "
                "VALUES (:rid, :vid, 'public', 'p', 'm', 1024, 1, 'queued', false, 'h', 0)"
            ).bindparams(rid=resource.id, vid=revision.id),
        )

    async def test_publication_must_match_resource_and_revision(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
        make_publication: Callable[..., Awaitable[Any]],
    ) -> None:
        """三列复合外键：publication 必须与本行的资源与版本一致。"""
        user = make_user(email="k3@example.com")
        await session.flush()
        first = await make_resource(user.id)
        second = await make_resource(user.id)
        first_revision = await make_resource_version(first)
        second_revision = await make_resource_version(second)
        foreign_publication = await make_publication(second, second_revision)

        await _expect_error(
            session,
            "fk_knowledge_indexes_resource_revision_publication",
            statement=text(
                "INSERT INTO knowledge_indexes (resource_id, revision_id, publication_id, "
                "scope, embedding_provider, embedding_model, embedding_dimension, generation, "
                "status, is_active, content_hash, version) "
                "VALUES (:rid, :vid, :pid, 'public', 'p', 'm', 1024, 1, 'queued', false, 'h', 0)"
            ).bindparams(rid=first.id, vid=first_revision.id, pid=foreign_publication.id),
        )

    async def test_revision_must_belong_to_the_resource(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="k4@example.com")
        await session.flush()
        first = await make_resource(user.id)
        second = await make_resource(user.id)
        foreign_revision = await make_resource_version(second)

        await _expect_error(
            session,
            "fk_knowledge_indexes_resource_id_revision_id",
            statement=text(
                "INSERT INTO knowledge_indexes (resource_id, revision_id, scope, "
                "embedding_provider, embedding_model, embedding_dimension, generation, "
                "status, is_active, content_hash, version) "
                "VALUES (:rid, :vid, 'owner', 'p', 'm', 1024, 1, 'queued', false, 'h', 0)"
            ).bindparams(rid=first.id, vid=foreign_revision.id),
        )

    async def test_active_index_must_be_ready(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
    ) -> None:
        """不允许把构建中的索引暴露给查询（文档 §5）。"""
        user = make_user(email="k5@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)

        await _index(
            session,
            resource,
            revision,
            status=IndexStatus.READY,
            is_active=True,
        )
        session.add(
            KnowledgeIndex(
                resource_id=resource.id,
                revision_id=revision.id,
                scope=IndexScope.OWNER,
                embedding_provider="p",
                embedding_model="m",
                embedding_dimension=EMBEDDING_DIMENSIONS,
                content_hash="h" * 64,
                status=IndexStatus.QUEUED,
                is_active=True,
            )
        )
        await _expect_error(session, "ck_knowledge_indexes_active_must_be_ready")

    async def test_one_active_owner_index_per_revision(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="k6@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)
        await _index(session, resource, revision, status=IndexStatus.READY, is_active=True)

        session.add(
            KnowledgeIndex(
                resource_id=resource.id,
                revision_id=revision.id,
                scope=IndexScope.OWNER,
                embedding_provider="p",
                embedding_model="m",
                embedding_dimension=EMBEDDING_DIMENSIONS,
                content_hash="h" * 64,
                status=IndexStatus.READY,
                is_active=True,
            )
        )
        await _expect_error(session, "uq_knowledge_indexes_owner_active")

    async def test_one_active_public_index_per_publication(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
        make_publication: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="k7@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)
        publication = await make_publication(resource, revision)
        await _index(
            session,
            resource,
            revision,
            scope=IndexScope.PUBLIC,
            publication_id=publication.id,
            status=IndexStatus.READY,
            is_active=True,
        )

        session.add(
            KnowledgeIndex(
                resource_id=resource.id,
                revision_id=revision.id,
                publication_id=publication.id,
                scope=IndexScope.PUBLIC,
                embedding_provider="p",
                embedding_model="m",
                embedding_dimension=EMBEDDING_DIMENSIONS,
                content_hash="h" * 64,
                status=IndexStatus.READY,
                is_active=True,
            )
        )
        await _expect_error(session, "uq_knowledge_indexes_public_active")

    async def test_owner_and_public_active_indexes_coexist(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
        make_publication: Callable[..., Awaitable[Any]],
    ) -> None:
        """两条部分唯一索引分别管 owner 与 public：同一版本可以各有一个活动索引。"""
        user = make_user(email="k8@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)
        publication = await make_publication(resource, revision)
        await _index(session, resource, revision, status=IndexStatus.READY, is_active=True)
        await _index(
            session,
            resource,
            revision,
            scope=IndexScope.PUBLIC,
            publication_id=publication.id,
            status=IndexStatus.READY,
            is_active=True,
        )

    async def test_inactive_rebuild_can_coexist_with_active_index(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
    ) -> None:
        """重建为新 generation、再原子切换：切换前新索引必须是非活动的。"""
        user = make_user(email="k9@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)
        await _index(session, resource, revision, status=IndexStatus.READY, is_active=True)
        await _index(
            session,
            resource,
            revision,
            generation=2,
            status=IndexStatus.BUILDING,
            is_active=False,
        )

    async def test_embedding_dimension_must_match_the_vector_column(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="k10@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)
        session.add(
            KnowledgeIndex(
                resource_id=resource.id,
                revision_id=revision.id,
                scope=IndexScope.OWNER,
                embedding_provider="p",
                embedding_model="m",
                embedding_dimension=512,
                content_hash="h" * 64,
            )
        )
        await _expect_error(session, "ck_knowledge_indexes_embedding_dimension_matches_column")

    async def test_metadata_guards(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="k11@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)
        session.add(
            KnowledgeIndex(
                resource_id=resource.id,
                revision_id=revision.id,
                scope=IndexScope.OWNER,
                embedding_provider="p",
                embedding_model="m",
                embedding_dimension=EMBEDDING_DIMENSIONS,
                content_hash="",
                generation=0,
            )
        )
        await _expect_error(session, "ck_knowledge_indexes_content_hash_not_empty")

    async def test_retired_index_is_terminal_and_inactive(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="k12@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)
        index = await _index(session, resource, revision, status=IndexStatus.RETIRED)
        await session.refresh(index)
        assert index.status is IndexStatus.RETIRED
        assert index.is_active is False


class TestKnowledgeChunkConstraints:
    async def _index(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        resource: Resource,
        revision: ResourceVersion,
    ) -> KnowledgeIndex:
        return await _index(session, resource, revision)

    async def _chunk(
        self, session: AsyncSession, index: KnowledgeIndex, **overrides: Any
    ) -> KnowledgeChunk:
        chunk = KnowledgeChunk(
            index_id=index.id,
            chunk_no=overrides.pop("chunk_no", 0),
            content_text=overrides.pop("content_text", "片段正文"),
            embedding=overrides.pop("embedding", _vector(0.0)),
            **overrides,
        )
        session.add(chunk)
        await session.flush()
        return chunk

    async def test_chunk_no_unique_per_index(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="ch1@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)
        index = await self._index(session, user.id, resource, revision)
        await self._chunk(session, index, chunk_no=0)

        session.add(
            KnowledgeChunk(
                index_id=index.id,
                chunk_no=0,
                content_text="重复分块号",
                embedding=_vector(0.0),
            )
        )
        await _expect_error(session, "uq_knowledge_chunks_index_chunk_no")

    async def test_same_chunk_no_allowed_for_another_index(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="ch2@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)
        first = await self._index(session, user.id, resource, revision)
        second = await _index(session, resource, revision, generation=2)
        await self._chunk(session, first, chunk_no=0)
        await self._chunk(session, second, chunk_no=0)

    async def test_vector_dimension_is_enforced_by_the_column(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
    ) -> None:
        """列维度是 1024：错误长度必须被数据库拒绝（不建近似索引不等于不校验维度）。"""
        user = make_user(email="ch3@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)
        index = await self._index(session, user.id, resource, revision)
        session.add(
            KnowledgeChunk(
                index_id=index.id,
                chunk_no=0,
                content_text="维度错误",
                embedding=[0.0] * 512,
            )
        )
        await _expect_error(session)

    async def test_chunk_guards(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="ch4@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)
        index = await self._index(session, user.id, resource, revision)
        session.add(
            KnowledgeChunk(
                index_id=index.id,
                chunk_no=-1,
                content_text="负数分块号",
                embedding=_vector(0.0),
            )
        )
        await _expect_error(session, "ck_knowledge_chunks_chunk_no_non_negative")

    async def test_empty_content_is_rejected(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="ch5@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)
        index = await self._index(session, user.id, resource, revision)
        session.add(
            KnowledgeChunk(
                index_id=index.id,
                chunk_no=0,
                content_text="",
                embedding=_vector(0.0),
            )
        )
        await _expect_error(session, "ck_knowledge_chunks_content_text_not_empty")

    async def test_locator_must_be_an_object(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="ch6@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)
        index = await self._index(session, user.id, resource, revision)
        chunk = KnowledgeChunk(
            index_id=index.id,
            chunk_no=0,
            content_text="定位",
            embedding=_vector(0.0),
            locator={"page_start": 1, "page_end": 2},
        )
        session.add(chunk)
        await session.flush()
        await session.refresh(chunk)
        assert chunk.locator == {"page_start": 1, "page_end": 2}

    async def test_cosine_distance_query_works(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
    ) -> None:
        """精确向量检索可用：``<=>`` 余弦距离可直接执行（首版不建近似索引）。"""
        user = make_user(email="ch7@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)
        index = await self._index(session, user.id, resource, revision)

        first = [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1)
        second = [0.0, 1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 2)
        left = await self._chunk(session, index, chunk_no=0, embedding=first)
        right = await self._chunk(session, index, chunk_no=1, embedding=second)

        distance = await session.scalar(
            text(
                "SELECT a.embedding <=> b.embedding FROM knowledge_chunks a, knowledge_chunks b "
                "WHERE a.id = :left AND b.id = :right"
            ),
            {"left": left.id, "right": right.id},
        )
        assert distance is not None
        # 正交向量的余弦距离是 1。
        assert abs(float(distance) - 1.0) < 1e-6

    async def test_deleting_index_cascades_to_chunks(
        self,
        session: AsyncSession,
        make_user: Callable[..., Any],
        make_resource: Callable[..., Awaitable[Any]],
        make_resource_version: Callable[..., Awaitable[Any]],
    ) -> None:
        user = make_user(email="ch8@example.com")
        await session.flush()
        resource = await make_resource(user.id)
        revision = await make_resource_version(resource)
        index = await self._index(session, user.id, resource, revision)
        await self._chunk(session, index)

        await session.execute(
            text("DELETE FROM knowledge_indexes WHERE id = :id"), {"id": index.id}
        )
        remaining = await session.scalar(
            text("SELECT count(*) FROM knowledge_chunks WHERE index_id = :iid"), {"iid": index.id}
        )
        assert remaining == 0

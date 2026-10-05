"""A5 第二批表：真实 PostgreSQL 约束验收。

重点是三条结构性不变量：
1. ``resources.version`` 与 ``acl_version`` 互不影响；
2. ``publications`` 单资源只有一个现行公开版本（Partial Unique Index）；
3. ``knowledge_indexes`` 的公开索引**只能**来自公开投影（CHECK 绑死外键列）。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from autumn_backend.db.enums import (
    ContentFormat,
    ConversationMode,
    IndexKind,
    IndexSourceKind,
    ResourceType,
    RunSourceKind,
)
from autumn_backend.db.models import (
    EMBEDDING_DIMENSIONS,
    KNOWLEDGE_INDEX_VECTOR_INDEX,
    Comment,
    Conversation,
    KnowledgeIndex,
    Publication,
    Resource,
    ResourceVersion,
    Run,
    RunSource,
)

pytestmark = pytest.mark.integration

UTC = dt.UTC


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


async def _make_resource(session: AsyncSession, owner_id: uuid.UUID, **overrides: Any) -> Resource:
    resource = Resource(
        owner_id=owner_id,
        type=overrides.pop("type", ResourceType.ARTICLE),
        title=overrides.pop("title", "标题"),
        **overrides,
    )
    session.add(resource)
    await session.flush()
    return resource


async def _make_version(
    session: AsyncSession, resource: Resource, version_no: int = 1, **overrides: Any
) -> ResourceVersion:
    version = ResourceVersion(
        resource_id=resource.id,
        version_no=version_no,
        content_format=overrides.pop("content_format", ContentFormat.MARKDOWN),
        body=overrides.pop("body", "正文"),
        body_hash="h" * 64,
        **overrides,
    )
    session.add(version)
    await session.flush()
    return version


async def _make_publication(
    session: AsyncSession,
    resource: Resource,
    version: ResourceVersion,
    public_no: int = 1,
    **overrides: Any,
) -> Publication:
    publication = Publication(
        resource_id=resource.id,
        resource_version_id=version.id,
        resource_version=version.version_no,
        acl_version_at_publish=resource.acl_version,
        public_no=public_no,
        public_url=overrides.pop("public_url", f"https://example.com/p/{resource.id}/{public_no}"),
        payload=overrides.pop("payload", {"title": resource.title}),
        **overrides,
    )
    session.add(publication)
    await session.flush()
    return publication


class TestSchemaObjectsExist:
    async def test_new_tables_present(self, session: AsyncSession) -> None:
        rows = await session.execute(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename = ANY(:names)"
            ),
            {
                "names": [
                    "resources",
                    "resource_versions",
                    "publications",
                    "comments",
                    "knowledge_indexes",
                    "run_sources",
                ]
            },
        )
        assert {row[0] for row in rows} == {
            "resources",
            "resource_versions",
            "publications",
            "comments",
            "knowledge_indexes",
            "run_sources",
        }

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
        for table in (
            "resources",
            "resource_versions",
            "publications",
            "comments",
            "knowledge_indexes",
            "run_sources",
        ):
            assert found.get(table) == "O", f"{table} 缺少已启用的 updated_at 触发器"

    async def test_hnsw_vector_index_installed_with_predicate(self, session: AsyncSession) -> None:
        row = await session.execute(
            text(
                "SELECT am.amname, pg_get_expr(i.indpred, i.indrelid) AS predicate "
                "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "JOIN pg_am am ON am.oid = c.relam WHERE c.relname = :name"
            ),
            {"name": KNOWLEDGE_INDEX_VECTOR_INDEX},
        )
        record = row.one_or_none()
        assert record is not None, "HNSW 向量索引没有建出来"
        assert record[0] == "hnsw"
        assert "superseded_at IS NULL" in record[1]

    async def test_partial_unique_indexes_installed(self, session: AsyncSession) -> None:
        # 注意：不能写 ``:name::regclass``——asyncpg 不接受在绑定参数上直接加类型转换。
        expected = {
            "uq_resources_owner_id_slug": "slug IS NOT NULL",
            "uq_publications_resource_id_current": "revoked_at IS NULL",
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
            assert fragment in predicate


class TestResourceConstraints:
    async def test_acl_version_defaults_to_zero(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="r-acl@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id)
        assert resource.acl_version == 0
        assert resource.version == 0

    async def test_acl_version_can_change_without_touching_version(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """publish / revoke 只递增 acl_version，绝不伪造 version 变化。"""
        user = make_user(email="r-sep@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id)

        await session.execute(
            text("UPDATE resources SET acl_version = acl_version + 1 WHERE id = :id"),
            {"id": resource.id},
        )
        await session.refresh(resource)
        assert resource.acl_version == 1
        assert resource.version == 0  # 内容版本原封不动

    async def test_acl_version_cannot_be_negative(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="r-neg@example.com")
        await session.flush()
        await _expect_error(
            session,
            "ck_resources_acl_version_non_negative",
            statement=text(
                "INSERT INTO resources (owner_id, type, title, tags, acl_version, version, "
                "created_at, updated_at) VALUES (:owner, 'article', 't', '[]'::jsonb, -1, 0, "
                "now(), now())"
            ).bindparams(owner=user.id),
        )

    async def test_type_must_be_whitelisted(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="r-type@example.com")
        await session.flush()
        await _expect_error(
            session,
            "ck_resources_type_valid",
            statement=text(
                "INSERT INTO resources (owner_id, type, title, tags, acl_version, version, "
                "created_at, updated_at) VALUES (:owner, 'video', 't', '[]'::jsonb, 0, 0, "
                "now(), now())"
            ).bindparams(owner=user.id),
        )

    async def test_slug_is_unique_within_one_owner(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="slug1@example.com")
        await session.flush()
        await _make_resource(session, user.id, slug="same-slug")

        session.add(
            Resource(owner_id=user.id, type=ResourceType.ARTICLE, title="b", slug="same-slug")
        )
        await _expect_error(session, "uq_resources_owner_id_slug")

    async def test_same_slug_is_allowed_for_a_different_owner(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """slug 只在作者范围内唯一：不同作者可以取同名。"""
        first = make_user(email="slug2@example.com")
        second = make_user(email="slug3@example.com")
        await session.flush()

        await _make_resource(session, first.id, slug="shared-slug")
        await _make_resource(session, second.id, slug="shared-slug")

    async def test_multiple_null_slugs_are_allowed(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="slug-null@example.com")
        await session.flush()
        for _ in range(2):
            await _make_resource(session, user.id, slug=None)

    async def test_private_note_is_stored(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="note@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id, private_note="只在私人上下文可见")
        await session.refresh(resource)
        assert resource.private_note == "只在私人上下文可见"

    async def test_tags_round_trip_as_jsonb(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="tags@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id, tags=["python", "数据库"])
        await session.refresh(resource)
        assert resource.tags == ["python", "数据库"]


class TestResourceVersionConstraints:
    async def test_version_no_unique_per_resource(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="v1@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id)
        await _make_version(session, resource, version_no=1)

        session.add(
            ResourceVersion(
                resource_id=resource.id,
                version_no=1,
                content_format=ContentFormat.TEXT,
                body="重复版本号",
                body_hash="x" * 64,
            )
        )
        await _expect_error(session, "uq_resource_versions_resource_version_no")

    async def test_storage_metadata_must_be_all_or_nothing(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="v2@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id, type=ResourceType.FILE)
        await _expect_error(
            session,
            "ck_resource_versions_storage_metadata_consistent",
            statement=text(
                "INSERT INTO resource_versions (resource_id, version_no, content_format, body, "
                "body_hash, storage_key, mime_type, size_bytes, version, created_at, updated_at) "
                "VALUES (:rid, 1, 'text', '', :hash, 'staging/x', NULL, NULL, 0, now(), now())"
            ).bindparams(rid=resource.id, hash="h" * 64),
        )

    async def test_content_format_must_be_whitelisted(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="v3@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id)
        await _expect_error(
            session,
            "ck_resource_versions_content_format_valid",
            statement=text(
                "INSERT INTO resource_versions (resource_id, version_no, content_format, body, "
                "body_hash, version, created_at, updated_at) "
                "VALUES (:rid, 1, 'docx', '', :hash, 0, now(), now())"
            ).bindparams(rid=resource.id, hash="h" * 64),
        )

    async def test_version_no_must_be_positive(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="v4@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id)
        await _expect_error(
            session,
            "ck_resource_versions_version_no_positive",
            statement=text(
                "INSERT INTO resource_versions (resource_id, version_no, content_format, body, "
                "body_hash, version, created_at, updated_at) "
                "VALUES (:rid, 0, 'text', '', :hash, 0, now(), now())"
            ).bindparams(rid=resource.id, hash="h" * 64),
        )

    async def test_published_version_cannot_be_deleted(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """resource_version_id 是 RESTRICT：已发布的内容版本不能被删掉。"""
        user = make_user(email="v5@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id)
        version = await _make_version(session, resource)
        await _make_publication(session, resource, version)

        await _expect_error(
            session,
            "fk_publications_resource_version_id_resource_versions",
            statement=text("DELETE FROM resource_versions WHERE id = :id").bindparams(
                id=version.id
            ),
        )


class TestPublicationConstraints:
    async def test_only_one_current_publication_per_resource(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="p1@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id)
        first_version = await _make_version(session, resource)
        await _make_publication(session, resource, first_version, public_no=1)

        # 第二条现行 publication：违反 Partial Unique Index。
        second_version = await _make_version(session, resource, version_no=2)
        session.add(
            Publication(
                resource_id=resource.id,
                resource_version_id=second_version.id,
                resource_version=second_version.version_no,
                acl_version_at_publish=0,
                public_no=2,
                public_url="https://example.com/second",
                payload={},
            )
        )
        await _expect_error(session, "uq_publications_resource_id_current")

    async def test_revoked_publication_frees_the_slot(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """撤销旧行后可以再插入新行——这正是"先撤销再发布"的事务形状。"""
        user = make_user(email="p2@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id)
        first_version = await _make_version(session, resource)
        first = await _make_publication(session, resource, first_version, public_no=1)

        await session.execute(
            text(
                "UPDATE publications SET revoked_at = now(), revoked_reason = 'republished' "
                "WHERE id = :id"
            ),
            {"id": first.id},
        )

        second_version = await _make_version(session, resource, version_no=2)
        await _make_publication(session, resource, second_version, public_no=2)

        active = await session.scalar(
            text(
                "SELECT count(*) FROM publications WHERE resource_id = :rid AND revoked_at IS NULL"
            ),
            {"rid": resource.id},
        )
        assert active == 1

    async def test_revocation_marker_must_be_consistent(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="p3@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id)
        version = await _make_version(session, resource)
        await _expect_error(
            session,
            "ck_publications_revocation_marker_consistent",
            statement=text(
                "INSERT INTO publications (resource_id, resource_version_id, resource_version, "
                "acl_version_at_publish, public_no, public_url, payload, revoked_at, "
                "revoked_reason, version, created_at, updated_at) "
                "VALUES (:rid, :vid, 1, 0, 1, 'https://example.com/x', '{}'::jsonb, "
                "now(), NULL, 0, now(), now())"
            ).bindparams(rid=resource.id, vid=version.id),
        )

    async def test_public_no_is_globally_unique(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="p4@example.com")
        await session.flush()
        first = await _make_resource(session, user.id)
        second = await _make_resource(session, user.id, title="第二个")
        v1 = await _make_version(session, first)
        v2 = await _make_version(session, second)
        await _make_publication(session, first, v1, public_no=7)

        session.add(
            Publication(
                resource_id=second.id,
                resource_version_id=v2.id,
                resource_version=1,
                acl_version_at_publish=0,
                public_no=7,
                public_url="https://example.com/other",
                payload={},
            )
        )
        await _expect_error(session, "uq_publications_public_no")

    async def test_payload_holds_public_projection_only(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """公开投影由 service 按类型白名单构造；这里验证 JSONB 往返不改内容。"""
        user = make_user(email="p5@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id, private_note="秘密")
        version = await _make_version(session, resource)
        payload = {"title": resource.title, "tags": []}
        publication = await _make_publication(session, resource, version, payload=payload)
        await session.refresh(publication)
        assert publication.payload == payload
        assert "private_note" not in publication.payload


class TestCommentConstraints:
    async def test_author_client_id_is_the_idempotency_identity(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="c1@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id)

        session.add(
            Comment(
                resource_id=resource.id,
                author_id=user.id,
                client_id="client-abc",
                request_hash="r" * 64,
                body="第一条",
            )
        )
        await session.flush()

        session.add(
            Comment(
                resource_id=resource.id,
                author_id=user.id,
                client_id="client-abc",
                request_hash="r" * 64,
                body="重放",
            )
        )
        await _expect_error(session, "uq_comments_author_client")

    async def test_same_client_id_allowed_for_different_authors(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        first = make_user(email="c2@example.com")
        second = make_user(email="c3@example.com")
        await session.flush()
        resource = await _make_resource(session, first.id)
        for author in (first, second):
            session.add(
                Comment(
                    resource_id=resource.id,
                    author_id=author.id,
                    client_id="shared-client",
                    request_hash="r" * 64,
                    body="ok",
                )
            )
        await session.flush()

    async def test_empty_body_is_rejected(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="c4@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id)
        session.add(
            Comment(
                resource_id=resource.id,
                author_id=user.id,
                client_id="c-empty",
                request_hash="r" * 64,
                body="",
            )
        )
        await _expect_error(session, "ck_comments_body_not_empty")

    async def test_parent_cannot_be_itself(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="c5@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id)
        comment_id = uuid.uuid4()
        await _expect_error(
            session,
            "ck_comments_parent_not_self",
            statement=text(
                "INSERT INTO comments (id, resource_id, author_id, parent_id, client_id, "
                "request_hash, body, status, version, created_at, updated_at) "
                "VALUES (:cid, :rid, :aid, :cid, 'self-parent', :rh, 'x', 'visible', 0, now(), now())"
            ).bindparams(cid=comment_id, rid=resource.id, aid=user.id, rh="r" * 64),
        )

    async def test_deleting_resource_cascades_to_comments(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="c6@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id)
        session.add(
            Comment(
                resource_id=resource.id,
                author_id=user.id,
                client_id="cascade",
                request_hash="r" * 64,
                body="将被级联删除",
            )
        )
        await session.flush()

        await session.execute(
            text("DELETE FROM resources WHERE id = :id").bindparams(id=resource.id)
        )
        remaining = await session.scalar(
            text("SELECT count(*) FROM comments WHERE resource_id = :rid"), {"rid": resource.id}
        )
        assert remaining == 0

    async def test_parent_invariant_is_not_enforced_by_database(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """明确记录：数据库**不**能阻止"父评论本身是子评论"这类跨行违规。

        这是设计决定的直接后果，由 Repository 在事务内锁定父行校验（阶段 B8）。
        本用例把该边界写成可执行文档：既然数据库放行，Repository 就必须自己拦。
        """
        user = make_user(email="c7@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id)

        top = Comment(
            resource_id=resource.id,
            author_id=user.id,
            client_id="top",
            request_hash="r" * 64,
            body="一级评论",
        )
        session.add(top)
        await session.flush()

        child = Comment(
            resource_id=resource.id,
            author_id=user.id,
            parent_id=top.id,
            client_id="child",
            request_hash="r" * 64,
            body="二级评论",
        )
        session.add(child)
        await session.flush()

        # 数据库接受"二级评论的父是二级评论"——跨行不变量不在 DB 层。
        grandchild = Comment(
            resource_id=resource.id,
            author_id=user.id,
            parent_id=child.id,
            client_id="grandchild",
            request_hash="r" * 64,
            body="三级（DB 不拦）",
        )
        session.add(grandchild)
        await session.flush()
        assert grandchild.id is not None


class TestKnowledgeIndexConstraints:
    async def test_private_index_requires_resource_version_and_no_publication(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="k1@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id)
        version = await _make_version(session, resource)

        session.add(
            KnowledgeIndex(
                owner_id=user.id,
                resource_id=resource.id,
                corpus_kind=IndexKind.PRIVATE,
                source_kind=IndexSourceKind.RESOURCE_VERSION,
                resource_version_id=version.id,
                chunk_index=0,
                fragment_start=0,
                fragment_end=10,
                text="片段",
                text_hash="t" * 64,
                embedding=_vector(),
                embedding_model="text-embedding-v3",
                embedding_dimensions=EMBEDDING_DIMENSIONS,
            )
        )
        await session.flush()

    async def test_private_index_cannot_reference_publication(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """复用私人 chunk 再加 public 标记的相邻错误：私人索引不得指向 publication。"""
        user = make_user(email="k2@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id)
        version = await _make_version(session, resource)
        publication = await _make_publication(session, resource, version)

        await _expect_error(
            session,
            "ck_knowledge_indexes_corpus_source_consistent",
            statement=text(
                "INSERT INTO knowledge_indexes (owner_id, resource_id, corpus_kind, source_kind, "
                "resource_version_id, publication_id, chunk_id, chunk_index, fragment_start, "
                "fragment_end, text, text_hash, embedding, embedding_model, "
                "embedding_dimensions, created_at, updated_at) "
                "VALUES (:oid, :rid, 'private', 'resource_version', :vid, :pid, gen_random_uuid(), "
                "0, 0, 5, 'x', :th, (:vec)::vector, 'm', :dim, now(), now())"
            ).bindparams(
                oid=user.id,
                rid=resource.id,
                vid=version.id,
                pid=publication.id,
                th="t" * 64,
                vec="[" + ",".join("0" for _ in range(EMBEDDING_DIMENSIONS)) + "]",
                dim=EMBEDDING_DIMENSIONS,
            ),
        )

    async def test_public_index_cannot_come_from_private_version(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """核心防线的负向证明：public 索引不得指向 resource_version。"""
        user = make_user(email="k3@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id)
        version = await _make_version(session, resource)

        await _expect_error(
            session,
            "ck_knowledge_indexes_corpus_source_consistent",
            statement=text(
                "INSERT INTO knowledge_indexes (owner_id, resource_id, corpus_kind, source_kind, "
                "resource_version_id, publication_id, chunk_id, chunk_index, fragment_start, "
                "fragment_end, text, text_hash, embedding, embedding_model, "
                "embedding_dimensions, created_at, updated_at) "
                "VALUES (:oid, :rid, 'public', 'resource_version', :vid, NULL, gen_random_uuid(), "
                "0, 0, 5, 'x', :th, (:vec)::vector, 'm', :dim, now(), now())"
            ).bindparams(
                oid=user.id,
                rid=resource.id,
                vid=version.id,
                th="t" * 64,
                vec="[" + ",".join("0" for _ in range(EMBEDDING_DIMENSIONS)) + "]",
                dim=EMBEDDING_DIMENSIONS,
            ),
        )

    async def test_public_index_from_publication_is_accepted(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="k4@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id)
        version = await _make_version(session, resource)
        publication = await _make_publication(session, resource, version)

        session.add(
            KnowledgeIndex(
                owner_id=user.id,
                resource_id=resource.id,
                corpus_kind=IndexKind.PUBLIC,
                source_kind=IndexSourceKind.PUBLICATION,
                publication_id=publication.id,
                chunk_index=0,
                fragment_start=0,
                fragment_end=12,
                text="公开片段",
                text_hash="t" * 64,
                embedding=_vector(0.5),
                embedding_model="text-embedding-v3",
                embedding_dimensions=EMBEDDING_DIMENSIONS,
            )
        )
        await session.flush()

    async def test_vector_dimension_is_enforced(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """列维度是 1024：错误长度必须被数据库拒绝。"""
        user = make_user(email="k5@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id)
        version = await _make_version(session, resource)

        await _expect_error(
            session,
            statement=text(
                "INSERT INTO knowledge_indexes (owner_id, resource_id, corpus_kind, source_kind, "
                "resource_version_id, chunk_id, chunk_index, fragment_start, fragment_end, text, "
                "text_hash, embedding, embedding_model, embedding_dimensions, created_at, "
                "updated_at) VALUES (:oid, :rid, 'private', 'resource_version', :vid, "
                "gen_random_uuid(), 0, 0, 5, 'x', :th, (:vec)::vector, 'm', 1024, now(), now())"
            ).bindparams(
                oid=user.id,
                rid=resource.id,
                vid=version.id,
                th="t" * 64,
                vec="[" + ",".join("0" for _ in range(512)) + "]",
            ),
        )

    async def test_embedding_dimensions_column_must_match_column_type(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="k6@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id)
        version = await _make_version(session, resource)

        await _expect_error(
            session,
            "ck_knowledge_indexes_embedding_dimensions_matches_column",
            statement=text(
                "INSERT INTO knowledge_indexes (owner_id, resource_id, corpus_kind, source_kind, "
                "resource_version_id, chunk_id, chunk_index, fragment_start, fragment_end, text, "
                "text_hash, embedding, embedding_model, embedding_dimensions, created_at, "
                "updated_at) VALUES (:oid, :rid, 'private', 'resource_version', :vid, "
                "gen_random_uuid(), 0, 0, 5, 'x', :th, (:vec)::vector, 'm', 512, now(), now())"
            ).bindparams(
                oid=user.id,
                rid=resource.id,
                vid=version.id,
                th="t" * 64,
                vec="[" + ",".join("0" for _ in range(EMBEDDING_DIMENSIONS)) + "]",
            ),
        )

    async def test_fragment_range_must_be_ordered(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="k7@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id)
        version = await _make_version(session, resource)

        await _expect_error(
            session,
            "ck_knowledge_indexes_fragment_range_ordered",
            statement=text(
                "INSERT INTO knowledge_indexes (owner_id, resource_id, corpus_kind, source_kind, "
                "resource_version_id, chunk_id, chunk_index, fragment_start, fragment_end, text, "
                "text_hash, embedding, embedding_model, embedding_dimensions, created_at, "
                "updated_at) VALUES (:oid, :rid, 'private', 'resource_version', :vid, "
                "gen_random_uuid(), 0, 10, 10, 'x', :th, (:vec)::vector, 'm', :dim, now(), now())"
            ).bindparams(
                oid=user.id,
                rid=resource.id,
                vid=version.id,
                th="t" * 64,
                vec="[" + ",".join("0" for _ in range(EMBEDDING_DIMENSIONS)) + "]",
                dim=EMBEDDING_DIMENSIONS,
            ),
        )

    async def test_similarity_query_works_with_cosine_operator(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """向量检索可用性：``<=>`` 余弦距离可直接执行。"""
        user = make_user(email="k8@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id)
        version = await _make_version(session, resource)
        session.add(
            KnowledgeIndex(
                owner_id=user.id,
                resource_id=resource.id,
                corpus_kind=IndexKind.PRIVATE,
                source_kind=IndexSourceKind.RESOURCE_VERSION,
                resource_version_id=version.id,
                chunk_index=0,
                fragment_start=0,
                fragment_end=4,
                text="检索目标",
                text_hash="t" * 64,
                embedding=_vector(1.0),
                embedding_model="text-embedding-v3",
                embedding_dimensions=EMBEDDING_DIMENSIONS,
            )
        )
        await session.flush()

        distance = await session.scalar(
            text(
                "SELECT embedding <=> (:vec)::vector FROM knowledge_indexes "
                "WHERE resource_id = :rid AND superseded_at IS NULL"
            ),
            {
                "vec": "[" + ",".join("1" for _ in range(EMBEDDING_DIMENSIONS)) + "]",
                "rid": resource.id,
            },
        )
        assert distance is not None
        assert abs(float(distance)) < 1e-6  # 同向量余弦距离为 0


async def _make_run_id(session: AsyncSession, owner_id: uuid.UUID) -> uuid.UUID:
    """``run_sources.run_id`` 有指向 ``runs`` 的外键，因此必须先建出真实 run。"""
    conversation = Conversation(user_id=owner_id, mode=ConversationMode.PUBLIC)
    conversation.thread_id = f"thread-{uuid.uuid4()}"
    session.add(conversation)
    await session.flush()

    run = Run(
        user_id=owner_id,
        conversation_id=conversation.id,
        idempotency_key=f"key-{uuid.uuid4()}",
        request_hash="r" * 64,
        mode=ConversationMode.PUBLIC,
    )
    session.add(run)
    await session.flush()
    return run.id


class TestRunSourceConstraints:
    async def test_private_and_public_grounding(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="rs1@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id)
        version = await _make_version(session, resource)
        publication = await _make_publication(session, resource, version)
        run_id = await _make_run_id(session, user.id)

        session.add(
            RunSource(
                run_id=run_id,
                grounding=RunSourceKind.PRIVATE,
                resource_id=resource.id,
                resource_version_id=version.id,
                acl_version=0,
                chunk_id=uuid.uuid4(),
                fragment_start=0,
                fragment_end=5,
            )
        )
        session.add(
            RunSource(
                run_id=run_id,
                grounding=RunSourceKind.PUBLIC,
                resource_id=resource.id,
                publication_id=publication.id,
                acl_version=1,
                chunk_id=uuid.uuid4(),
                fragment_start=0,
                fragment_end=5,
            )
        )
        await session.flush()

    async def test_grounding_target_must_match_kind(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """private 来源不得挂 publication_id。"""
        user = make_user(email="rs2@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id)
        version = await _make_version(session, resource)
        publication = await _make_publication(session, resource, version)
        run_id = await _make_run_id(session, user.id)

        await _expect_error(
            session,
            "ck_run_sources_grounding_target_consistent",
            statement=text(
                "INSERT INTO run_sources (run_id, grounding, resource_id, resource_version_id, "
                "publication_id, acl_version, cited, created_at, updated_at) "
                "VALUES (:run, 'private', :rid, :vid, :pid, 0, false, now(), now())"
            ).bindparams(run=run_id, rid=resource.id, vid=version.id, pid=publication.id),
        )

    async def test_chunk_recorded_once_per_run(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="rs3@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id)
        version = await _make_version(session, resource)
        run_id = await _make_run_id(session, user.id)
        chunk = uuid.uuid4()

        for _ in range(2):
            session.add(
                RunSource(
                    run_id=run_id,
                    grounding=RunSourceKind.PRIVATE,
                    resource_id=resource.id,
                    resource_version_id=version.id,
                    acl_version=0,
                    chunk_id=chunk,
                )
            )
        await _expect_error(session, "uq_run_sources_run_chunk")

    async def test_fragment_markers_are_all_or_nothing(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="rs4@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id)
        version = await _make_version(session, resource)
        run_id = await _make_run_id(session, user.id)

        await _expect_error(
            session,
            "ck_run_sources_fragment_markers_consistent",
            statement=text(
                "INSERT INTO run_sources (run_id, grounding, resource_id, resource_version_id, "
                "acl_version, fragment_start, fragment_end, cited, created_at, updated_at) "
                "VALUES (:run, 'private', :rid, :vid, 0, 0, NULL, false, now(), now())"
            ).bindparams(run=run_id, rid=resource.id, vid=version.id),
        )

    async def test_acl_version_is_recorded_for_staleness_checks(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        user = make_user(email="rs5@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id)
        version = await _make_version(session, resource)
        await session.execute(
            text("UPDATE resources SET acl_version = 3 WHERE id = :id"), {"id": resource.id}
        )
        source = RunSource(
            run_id=await _make_run_id(session, user.id),
            grounding=RunSourceKind.PRIVATE,
            resource_id=resource.id,
            resource_version_id=version.id,
            acl_version=3,
        )
        session.add(source)
        await session.flush()
        assert source.acl_version == 3


class TestV1AcceptanceInvariants:
    """把 v1 验收标准直接落成可执行断言（结构层面）。"""

    async def test_private_note_never_reaches_the_public_projection(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """私人备注不进入公开 API 或公开索引。

        公开侧只有两个出口：``publications.payload``（service 按类型白名单构造）
        与 ``knowledge_indexes``（CHECK 强制 public 只能来自 publication）。
        本用例证明：即使资源上挂着私密备注，公开行里也没有它的载体。
        """
        user = make_user(email="acc1@example.com")
        await session.flush()
        secret = "绝密备注-do-not-leak"
        resource = await _make_resource(session, user.id, private_note=secret)
        version = await _make_version(session, resource, body="公开正文")
        publication = await _make_publication(
            session, resource, version, payload={"title": resource.title, "tags": []}
        )

        session.add(
            KnowledgeIndex(
                owner_id=user.id,
                resource_id=resource.id,
                corpus_kind=IndexKind.PUBLIC,
                source_kind=IndexSourceKind.PUBLICATION,
                publication_id=publication.id,
                chunk_index=0,
                fragment_start=0,
                fragment_end=4,
                text="公开正文",
                text_hash="t" * 64,
                embedding=_vector(0.1),
                embedding_model="text-embedding-v3",
                embedding_dimensions=EMBEDDING_DIMENSIONS,
            )
        )
        await session.flush()

        # 公开投影里没有 private_note 这个键。
        assert set(publication.payload) == {"title", "tags"}
        # 公开索引的文本不包含私密字段内容。
        leaked = await session.scalar(
            text(
                "SELECT count(*) FROM knowledge_indexes "
                "WHERE corpus_kind = 'public' AND text LIKE '%' || :secret || '%'"
            ),
            {"secret": secret},
        )
        assert leaked == 0

    async def test_editing_the_draft_does_not_change_the_published_version(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """更新私人原稿不改变已发布版本：publication 绑定的是不可变版本行。"""
        user = make_user(email="acc2@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id)
        published_version = await _make_version(session, resource, body="已发布正文")
        publication = await _make_publication(session, resource, published_version)

        # 编辑原稿：新增一行版本 + 递增 resources.version（内容变化）。
        draft = await _make_version(session, resource, version_no=2, body="改稿中的正文")
        await session.execute(
            text("UPDATE resources SET version = version + 1 WHERE id = :id"), {"id": resource.id}
        )
        await session.refresh(publication)
        await session.refresh(resource)

        # 现行公开版本仍指向旧版本行，公开正文不变。
        assert publication.resource_version_id == published_version.id
        assert publication.resource_version == 1
        assert published_version.body == "已发布正文"
        assert draft.body == "改稿中的正文"
        # 内容版本前进了，但可见性版本没有动（编辑不是可见性变化）。
        assert resource.version == 1
        assert resource.acl_version == 0

    async def test_draft_version_cannot_feed_a_public_index(
        self, session: AsyncSession, make_user: Callable[..., Any]
    ) -> None:
        """未发布的改稿不能作为公开索引来源——public 必须指向 publication。"""
        user = make_user(email="acc3@example.com")
        await session.flush()
        resource = await _make_resource(session, user.id)
        draft = await _make_version(session, resource, body="未发布草稿")

        await _expect_error(
            session,
            "ck_knowledge_indexes_corpus_source_consistent",
            statement=text(
                "INSERT INTO knowledge_indexes (owner_id, resource_id, corpus_kind, source_kind, "
                "resource_version_id, chunk_id, chunk_index, fragment_start, fragment_end, text, "
                "text_hash, embedding, embedding_model, embedding_dimensions, created_at, "
                "updated_at) VALUES (:oid, :rid, 'public', 'publication', :vid, "
                "gen_random_uuid(), 0, 0, 4, '草稿', :th, (:vec)::vector, 'm', :dim, now(), now())"
            ).bindparams(
                oid=user.id,
                rid=resource.id,
                vid=draft.id,
                th="t" * 64,
                vec="[" + ",".join("0" for _ in range(EMBEDDING_DIMENSIONS)) + "]",
                dim=EMBEDDING_DIMENSIONS,
            ),
        )

"""权限范围先于向量排序；索引切换与来源写入只在调用方 UoW 中。"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from autumn_backend.db.enums import IndexScope, IndexStatus, RunSourceType
from autumn_backend.db.models import (
    KnowledgeChunk,
    KnowledgeIndex,
    Publication,
    Resource,
    RunSource,
)
from autumn_backend.errors import IdempotencyConflictError
from autumn_backend.knowledge.text import TextPart
from autumn_backend.repositories.base import RepositoryBase
from autumn_backend.repositories.constraints import database_errors


@dataclass(frozen=True, slots=True)
class Hit:
    chunk_id: UUID
    index_id: UUID
    resource_id: UUID
    revision_id: UUID
    publication_id: UUID | None
    acl_version: int
    text: str
    locator: dict[str, Any] | None


class KnowledgeRepository(RepositoryBase):
    async def retire_public(self, resource_id: UUID) -> None:
        await self.session.execute(
            update(KnowledgeIndex)
            .where(
                KnowledgeIndex.resource_id == resource_id,
                KnowledgeIndex.scope == IndexScope.PUBLIC,
                KnowledgeIndex.is_active.is_(True),
            )
            .values(is_active=False, version=KnowledgeIndex.version + 1)
        )

    async def allowed_indexes(
        self,
        *,
        owner_id: UUID | None,
        provider: str,
        model: str,
        resource_ids: tuple[UUID, ...] = (),
    ) -> list[KnowledgeIndex]:
        statement = (
            select(KnowledgeIndex)
            .join(Resource, Resource.id == KnowledgeIndex.resource_id)
            .where(
                Resource.deleted_at.is_(None),
                Resource.archived_at.is_(None),
                or_(Resource.expires_at.is_(None), Resource.expires_at > func.clock_timestamp()),
                KnowledgeIndex.is_active.is_(True),
                KnowledgeIndex.status == IndexStatus.READY,
                KnowledgeIndex.embedding_provider == provider,
                KnowledgeIndex.embedding_model == model,
                KnowledgeIndex.embedding_dimension == 1024,
            )
        )
        if owner_id is not None:
            statement = statement.where(
                KnowledgeIndex.scope == IndexScope.OWNER,
                Resource.owner_id == owner_id,
                KnowledgeIndex.revision_id == Resource.current_revision_id,
            )
        else:
            statement = statement.join(
                Publication, Publication.id == KnowledgeIndex.publication_id
            ).where(
                KnowledgeIndex.scope == IndexScope.PUBLIC,
                Publication.revoked_at.is_(None),
                Publication.ai_enabled.is_(True),
                Publication.resource_id == KnowledgeIndex.resource_id,
                Publication.revision_id == KnowledgeIndex.revision_id,
            )
        if resource_ids:
            statement = statement.where(Resource.id.in_(resource_ids))
        return list(
            (await self.session.scalars(statement.order_by(Resource.id, KnowledgeIndex.id))).all()
        )

    async def activate(
        self,
        *,
        resource_id: UUID,
        revision_id: UUID,
        publication_id: UUID | None,
        provider: str,
        model: str,
        content_hash: str,
        parts: tuple[TextPart, ...],
        vectors: list[list[float]],
    ) -> KnowledgeIndex:
        # Service 已持有 Resource 锁，先退役再激活，不出现两个活动版本。
        scope = IndexScope.PUBLIC if publication_id is not None else IndexScope.OWNER
        target = (
            (KnowledgeIndex.publication_id == publication_id)
            if publication_id is not None
            else (KnowledgeIndex.revision_id == revision_id)
        )
        generation = (
            await self.session.execute(
                select(func.coalesce(func.max(KnowledgeIndex.generation), 0) + 1).where(
                    target, KnowledgeIndex.scope == scope
                )
            )
        ).scalar_one()
        with database_errors():
            record = KnowledgeIndex(
                resource_id=resource_id,
                revision_id=revision_id,
                publication_id=publication_id,
                scope=scope,
                embedding_provider=provider,
                embedding_model=model,
                embedding_dimension=1024,
                generation=generation,
                content_hash=content_hash,
                status=IndexStatus.READY,
                is_active=False,
            )
            self.session.add(record)
            await self.session.flush()
            self.session.add_all(
                [
                    KnowledgeChunk(
                        index_id=record.id,
                        chunk_no=number,
                        content_text=part.text,
                        locator=part.locator,
                        embedding=vector,
                    )
                    for number, (part, vector) in enumerate(zip(parts, vectors, strict=True))
                ]
            )
            await self.session.flush()
            await self.session.execute(
                update(KnowledgeIndex)
                .where(target, KnowledgeIndex.scope == scope, KnowledgeIndex.is_active.is_(True))
                .values(is_active=False, version=KnowledgeIndex.version + 1)
            )
            record.is_active = True
            await self.session.flush()
        return record

    async def nearest(
        self, index_ids: tuple[UUID, ...], vector: list[float], *, limit: int
    ) -> tuple[Hit, ...]:
        if not index_ids:
            return ()
        statement = (
            select(KnowledgeChunk, KnowledgeIndex, Resource.acl_version)
            .join(KnowledgeIndex, KnowledgeIndex.id == KnowledgeChunk.index_id)
            .join(Resource, Resource.id == KnowledgeIndex.resource_id)
            .where(KnowledgeChunk.index_id.in_(index_ids))
            .order_by(KnowledgeChunk.embedding.cosine_distance(vector), KnowledgeChunk.id)
            .limit(limit)
        )
        rows = (await self.session.execute(statement)).all()
        return tuple(
            Hit(
                chunk.id,
                index.id,
                index.resource_id,
                index.revision_id,
                index.publication_id,
                acl,
                chunk.content_text,
                chunk.locator,
            )
            for chunk, index, acl in rows
        )

    async def sources(
        self, run_id: UUID, *, context_generation: int | None = None
    ) -> tuple[RunSource, ...]:
        statement = select(RunSource).where(RunSource.run_id == run_id)
        if context_generation is not None:
            statement = statement.where(RunSource.context_generation == context_generation)
        return tuple((await self.session.scalars(statement.order_by(RunSource.id))).all())

    async def record(
        self, run_id: UUID, hit: Hit, *, context_generation: int, source_key: str | None = None
    ) -> RunSource:
        return await self.record_values(
            run_id,
            source_key or f"chunk:{hit.chunk_id}",
            {
                "source_type": RunSourceType.RESOURCE,
                "resource_id": hit.resource_id,
                "revision_id": hit.revision_id,
                "publication_id": hit.publication_id,
                "index_id": hit.index_id,
                "chunk_id": hit.chunk_id,
                "observed_acl_version": hit.acl_version,
                "locator": hit.locator,
            },
            context_generation=context_generation,
        )

    async def record_values(
        self, run_id: UUID, key: str, values: dict[str, Any], *, context_generation: int
    ) -> RunSource:
        key = f"g{context_generation}:{key}"
        values = {**values, "context_generation": context_generation}
        with database_errors():
            record = (
                await self.session.execute(
                    insert(RunSource)
                    .values(run_id=run_id, source_key=key, **values)
                    .on_conflict_do_nothing(constraint="uq_run_sources_run_id_source_key")
                    .returning(RunSource)
                )
            ).scalar_one_or_none()
        if record is not None:
            return record
        record = (
            await self.session.execute(
                select(RunSource).where(RunSource.run_id == run_id, RunSource.source_key == key)
            )
        ).scalar_one()
        if any(getattr(record, field) != value for field, value in values.items()):
            raise IdempotencyConflictError("运行来源键已绑定不同来源")
        return record

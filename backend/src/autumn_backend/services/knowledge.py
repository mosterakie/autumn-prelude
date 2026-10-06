"""E6 文档/网页版本、分开索引、先授权再召回与完整来源登记。"""

import asyncio
import hashlib
import math
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid5

from sqlalchemy import select

from autumn_backend.db.enums import (
    FileObjectStatus,
    MessageStatus,
    ProviderCallPurpose,
    ProviderCallStatus,
    ResourceKind,
    RunSourceType,
    RunStatus,
    SummaryStatus,
)
from autumn_backend.db.models import (
    ConversationSummary,
    KnowledgeChunk,
    Memory,
    Message,
    ProviderCall,
    Resource,
    ResourceVersion,
    Run,
    RunSource,
)
from autumn_backend.db.session import UnitOfWork, UnitOfWorkFactory
from autumn_backend.errors import (
    ConflictError,
    InvalidInputError,
    NotFoundError,
    OptimisticLockError,
)
from autumn_backend.io_boundary import require_outside_uow
from autumn_backend.jobs.queue import enqueue
from autumn_backend.knowledge.text import TextPart, chunks, extract
from autumn_backend.knowledge.web import SafeWebFetcher, WebPage, validate_url
from autumn_backend.policies import ActorContext, Decision, DenialCode
from autumn_backend.policies.context import source_decision
from autumn_backend.policies.facts import ConversationMode, Operation, TargetKind
from autumn_backend.repositories.jobs import JobSpec
from autumn_backend.repositories.knowledge import Hit
from autumn_backend.repositories.provider_calls import external_idempotency_key
from autumn_backend.repositories.resources import RevisionDraft
from autumn_backend.services.access import (
    AuthorizationError,
    lock_authentication,
    publication_facts,
    require_allowed,
)
from autumn_backend.services.context import TaskFence, require_task, run_facts, source_fact
from autumn_backend.services.storage import StorageService


class Embedder(Protocol):
    provider: str
    model: str

    async def embed(
        self, texts: tuple[str, ...], *, external_idempotency_key: str
    ) -> list[list[float]]: ...


class Fetcher(Protocol):
    async def fetch(self, url: str) -> WebPage: ...


class Reranker(Protocol):
    provider: str
    model: str

    async def rank(
        self, query: str, hits: tuple[Hit, ...], *, external_idempotency_key: str
    ) -> tuple[UUID, ...]: ...


@dataclass(frozen=True, slots=True)
class Ingested:
    resource_id: UUID
    revision_id: UUID
    job_id: UUID


@dataclass(frozen=True, slots=True)
class Citation:
    id: UUID
    text: str
    resource_id: UUID | None
    revision_id: UUID | None
    publication_id: UUID | None
    locator: dict[str, object] | None


def validate_vectors(vectors: list[list[float]], count: int) -> None:
    if len(vectors) != count or any(
        len(vector) != 1024 or any(not math.isfinite(value) for value in vector) or not any(vector)
        for vector in vectors
    ):
        raise InvalidInputError("向量数量、维度或数值无效；要求 1024 维且模型一致")


class KnowledgeService:
    @staticmethod
    async def _prepare_external(
        uow: UnitOfWork,
        *,
        provider: str,
        model: str,
        purpose: ProviderCallPurpose,
        key: str,
        job_id: UUID | None = None,
        run_id: UUID | None = None,
    ) -> ProviderCall:
        unsettled = await uow.session.scalar(
            select(ProviderCall.id)
            .where(
                ProviderCall.run_id == run_id
                if run_id is not None
                else ProviderCall.job_id == job_id,
                ProviderCall.status.in_(
                    (ProviderCallStatus.DISPATCHED, ProviderCallStatus.UNKNOWN)
                ),
            )
            .limit(1)
        )
        if unsettled is not None:
            raise ConflictError("当前运行/任务的供应商结果尚未确定")
        call = (
            await uow.repositories.provider_calls.prepare(
                provider=provider,
                model=model,
                purpose=purpose,
                logical_call_key=key,
                job_id=job_id,
                run_id=run_id,
            )
        ).record
        if call.status is not ProviderCallStatus.PREPARED:
            raise ConflictError("既有外部调用不能自动重放")
        return await uow.repositories.provider_calls.mark_dispatched(call.id)

    def __init__(
        self,
        uows: UnitOfWorkFactory,
        storage: StorageService,
        embedder: Embedder,
        *,
        fetcher: Fetcher | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        if not embedder.provider.strip() or not embedder.model.strip():
            raise InvalidInputError("嵌入模型身份不能为空")
        self._uows, self.storage, self.embedder = uows, storage, embedder
        self.fetcher = fetcher if fetcher is not None else SafeWebFetcher()
        self.reranker = reranker

    async def _owner(
        self, uow: UnitOfWork, actor: ActorContext, resource_id: UUID | None = None
    ) -> Resource | None:
        auth = await lock_authentication(uow, actor)
        resource = (
            await uow.repositories.resources.get_for_update_or_raise(resource_id)
            if resource_id is not None
            else None
        )
        require_allowed(
            actor, await publication_facts(uow, Operation.INGEST_RESOURCE, auth, resource)
        )
        return resource

    async def _queue(
        self,
        uow: UnitOfWork,
        actor: ActorContext,
        resource: Resource,
        *,
        publication_id: UUID | None = None,
        key: str | None = None,
    ) -> Ingested:
        assert resource.current_revision_id is not None
        revision_id = resource.current_revision_id
        if publication_id is not None:
            publication = await uow.repositories.publications.current_for_resource(resource.id)
            if (
                publication is None
                or publication.id != publication_id
                or not publication.ai_enabled
                or not resource.is_active
            ):
                raise NotFoundError("可供 AI 使用的公开投影不存在")
            revision_id = publication.revision_id
        payload = {
            "schema_version": 1,
            "revision_id": str(revision_id),
            "publication_id": str(publication_id) if publication_id else None,
            "resource_version": resource.version,
            "acl_version": resource.acl_version,
            "provider": self.embedder.provider,
            "model": self.embedder.model,
        }
        identity = hashlib.sha256(repr(sorted(payload.items())).encode()).hexdigest()
        job = await enqueue(
            uow,
            JobSpec(
                kind="knowledge.ingest",
                idempotency_key=f"knowledge.ingest:{resource.id}:{key or identity}",
                actor_id=actor.user_id,
                auth_session_id=actor.auth_session_id,
                resource_id=resource.id,
                payload=payload,
            ),
        )
        return Ingested(resource.id, revision_id, job.record.id)

    async def ingest_file(self, actor: ActorContext, file_id: UUID, *, title: str) -> Ingested:
        if not title.strip() or len(title) > 300:
            raise InvalidInputError("文档标题无效")
        data = await self.storage.read_private(actor, file_id)
        async with self._uows() as uow:
            await self._owner(uow, actor)
            file = await uow.repositories.files.get_for_update_or_raise(file_id)
            media_type = file.media_type
        require_outside_uow()
        parts = await asyncio.to_thread(extract, data, media_type)
        async with self._uows() as uow:
            await self._owner(uow, actor)
            assert actor.user_id is not None
            file = await uow.repositories.files.get_for_update_or_raise(file_id)
            if (
                file.owner_id != actor.user_id
                or file.status is not FileObjectStatus.READY
                or file.deleted_at is not None
            ):
                raise NotFoundError("文件不存在")
            if file.resource_id is not None:
                resource = await self._owner(uow, actor, file.resource_id)
                assert resource is not None
                revision = await uow.session.get(ResourceVersion, resource.current_revision_id)
                if revision is None or revision.title != title.strip():
                    raise ConflictError("文件已按另一标题导入")
            else:
                resource = await uow.repositories.resources.create(
                    owner_id=actor.user_id,
                    kind=ResourceKind.DOCUMENT,
                    slug=f"document-{file.id.hex}",
                    draft=RevisionDraft(
                        title=title.strip(),
                        body_text="\n\n".join(part.text for part in parts),
                        file_object_key=file.object_key,
                        file_sha256=file.sha256,
                        media_type=file.media_type,
                        byte_size=file.byte_size,
                    ),
                )
                await uow.repositories.files.attach(file.id, resource.id)
            return await self._queue(uow, actor, resource)

    async def ingest_web(self, actor: ActorContext, *, url: str, idempotency_key: str) -> Ingested:
        url = validate_url(url)
        if not idempotency_key.strip() or len(idempotency_key) > 128:
            raise InvalidInputError("网页导入幂等标识无效")
        slug = f"web-{uuid5(UUID('c04d1aac-fdcd-5652-931b-9a930261507c'), f'{actor.user_id}:{idempotency_key}').hex}"
        async with self._uows() as uow:
            await self._owner(uow, actor)
            existing = await uow.session.scalar(
                select(Resource).where(Resource.owner_id == actor.user_id, Resource.slug == slug)
            )
            if existing is not None:
                await self._owner(uow, actor, existing.id)
                revision = await uow.session.get(ResourceVersion, existing.current_revision_id)
                if revision is None or revision.private_note != f"ingest_url:{url}":
                    raise ConflictError("网页标识已用于另一个链接")
                return await self._queue(uow, actor, existing)
        require_outside_uow()
        page = await self.fetcher.fetch(url)
        validate_url(page.url)
        if not page.text.strip() or len(page.text) > 2_000_000:
            raise InvalidInputError("网页正文无效")
        async with self._uows() as uow:
            await self._owner(uow, actor)
            assert actor.user_id is not None
            existing = await uow.session.scalar(
                select(Resource).where(Resource.owner_id == actor.user_id, Resource.slug == slug)
            )
            if existing is not None:
                revision = await uow.session.get(ResourceVersion, existing.current_revision_id)
                if revision is None or revision.private_note != f"ingest_url:{url}":
                    raise ConflictError("网页标识已用于另一个链接")
                resource = existing
            else:
                resource = await uow.repositories.resources.create(
                    owner_id=actor.user_id,
                    kind=ResourceKind.WEBPAGE,
                    slug=slug,
                    draft=RevisionDraft(
                        title=page.title[:300],
                        body_text=page.text,
                        url=page.url,
                        private_note=f"ingest_url:{url}",
                    ),
                )
            return await self._queue(uow, actor, resource)

    async def request_index(
        self,
        actor: ActorContext,
        resource_id: UUID,
        *,
        publication_id: UUID | None = None,
        rebuild_key: str | None = None,
    ) -> Ingested:
        if rebuild_key is not None and (not rebuild_key.strip() or len(rebuild_key) > 128):
            raise InvalidInputError("重建标识无效")
        async with self._uows() as uow:
            resource = await self._owner(uow, actor, resource_id)
            assert resource is not None
            return await self._queue(
                uow, actor, resource, publication_id=publication_id, key=rebuild_key
            )

    async def sync_publication(self, actor: ActorContext, job_id: UUID, token: UUID) -> UUID | None:
        """承接 E1 的 publication_sync；旧通知不得覆盖新的公开状态。"""
        async with self._uows() as uow:
            await self._owner(uow, actor)
            job = await uow.repositories.jobs.require_lease(job_id, token)
            if (
                job.kind != "knowledge.publication_sync"
                or job.actor_id != actor.user_id
                or job.auth_session_id != actor.auth_session_id
                or job.resource_id is None
                or not isinstance(job.payload, dict)
            ):
                raise NotFoundError("公开索引同步任务不存在")
            resource = await uow.repositories.resources.get_for_update_or_raise(job.resource_id)
            if resource.owner_id != actor.user_id:
                raise NotFoundError("资源不存在")
            current = await uow.repositories.publications.current_for_resource(resource.id)
            requested = job.payload.get("publication_id")
            if (
                resource.acl_version != job.payload.get("acl_version")
                or (str(current.id) if current else None) != requested
            ):
                await uow.repositories.jobs.finish(job_id, token, result={"superseded": True})
                return None
            await uow.repositories.knowledge.retire_public(resource.id)
            next_job = None
            if current is not None and current.ai_enabled and resource.is_active:
                next_job = (
                    await self._queue(uow, actor, resource, publication_id=current.id)
                ).job_id
            await uow.repositories.jobs.finish(
                job_id, token, result={"index_job_id": str(next_job) if next_job else None}
            )
            await self._owner(uow, actor)
            return next_job

    async def _index_input(
        self, uow: UnitOfWork, actor: ActorContext, job_id: UUID, token: UUID
    ) -> tuple[Resource, UUID, UUID | None, tuple[TextPart, ...]]:
        await self._owner(uow, actor)
        job = await uow.repositories.jobs.require_lease(job_id, token)
        if (
            job.kind != "knowledge.ingest"
            or job.actor_id != actor.user_id
            or job.auth_session_id != actor.auth_session_id
            or job.resource_id is None
            or not isinstance(job.payload, dict)
        ):
            raise NotFoundError("知识任务不存在")
        resource = await self._owner(uow, actor, job.resource_id)
        assert resource is not None
        payload = job.payload
        if (payload.get("schema_version"), payload.get("provider"), payload.get("model")) != (
            1,
            self.embedder.provider,
            self.embedder.model,
        ):
            raise ConflictError("知识任务或模型版本无效")
        if (payload.get("resource_version"), payload.get("acl_version")) != (
            resource.version,
            resource.acl_version,
        ) or not resource.is_active:
            raise OptimisticLockError("索引输入已失效，请重新排队")
        try:
            revision_id = UUID(payload["revision_id"])
            publication_id = (
                UUID(payload["publication_id"])
                if payload.get("publication_id") is not None
                else None
            )
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise InvalidInputError("知识任务载荷无效") from error
        if publication_id is not None:
            publication = await uow.repositories.publications.current_for_resource(resource.id)
            if (
                publication is None
                or publication.id != publication_id
                or publication.revision_id != revision_id
                or not publication.ai_enabled
            ):
                raise NotFoundError("公开投影已失效")
            # 只能取公开投影列，禁止回读 revision 的完整正文。
            values = [
                (name, getattr(publication, f"public_{name}")) for name in publication.public_fields
            ]
        else:
            revision = await uow.session.get(ResourceVersion, revision_id)
            if (
                revision is None
                or revision.resource_id != resource.id
                or revision.id != resource.current_revision_id
            ):
                raise NotFoundError("原稿版本已失效")
            values = [
                ("title", revision.title),
                ("body", revision.body_text),
                ("note", revision.private_note),
                ("url", revision.url),
                ("tags", revision.tags),
            ]
        parts = tuple(
            TextPart(
                " ".join(value) if isinstance(value, list) else value,
                {"kind": "projection" if publication_id else "revision", "field": name},
            )
            for name, value in values
            if value
        )
        return resource, revision_id, publication_id, parts

    async def build_index(self, actor: ActorContext, job_id: UUID, token: UUID) -> UUID:
        async with self._uows() as uow:
            _, revision_id, publication_id, parts = await self._index_input(
                uow, actor, job_id, token
            )
            file_input = None
            if publication_id is None:
                revision = await uow.session.get(ResourceVersion, revision_id)
                if revision is not None and revision.file_object_key is not None:
                    file = await uow.repositories.files.by_key(revision.file_object_key)
                    if (
                        file is None
                        or file.status is not FileObjectStatus.READY
                        or file.deleted_at is not None
                    ):
                        raise NotFoundError("索引原文件不可读")
                    file_input = (file.id, file.media_type)
        if file_input is not None:
            data = await self.storage.read_private(actor, file_input[0])
            require_outside_uow()
            file_parts = await asyncio.to_thread(extract, data, file_input[1])
            parts = (
                tuple(part for part in parts if part.locator.get("field") != "body") + file_parts
            )
        parts = chunks(parts)
        batch_size = getattr(self.embedder, "batch_size", 10)
        if type(batch_size) is not int or not 1 <= batch_size <= 10 or not parts:
            raise InvalidInputError("嵌入批次配置或索引文本无效")
        vectors = []
        for offset in range(0, len(parts), batch_size):
            batch = parts[offset : offset + batch_size]
            async with self._uows() as uow:
                await self._index_input(uow, actor, job_id, token)
                call = await self._prepare_external(
                    uow,
                    provider=self.embedder.provider,
                    model=self.embedder.model,
                    purpose=ProviderCallPurpose.EMBEDDING,
                    key=f"index:{job_id}:embedding"
                    + (f":batch{offset // batch_size}" if len(parts) > batch_size else ""),
                    job_id=job_id,
                )
                call_id, call_key = call.id, external_idempotency_key(call.logical_call_key)
            require_outside_uow()
            batch_vectors = await self.embedder.embed(
                tuple(part.text for part in batch), external_idempotency_key=call_key
            )
            validate_vectors(batch_vectors, len(batch))
            vectors.extend(batch_vectors)
            if offset + batch_size < len(parts):
                async with self._uows() as uow:
                    await self._index_input(uow, actor, job_id, token)
                    await uow.repositories.provider_calls.settle_succeeded(call_id)
        validate_vectors(vectors, len(parts))
        digest = hashlib.sha256(repr(parts).encode()).hexdigest()
        async with self._uows() as uow:
            resource, current_revision, current_publication, _ = await self._index_input(
                uow, actor, job_id, token
            )
            if (current_revision, current_publication) != (revision_id, publication_id):
                raise ConflictError("索引输入变化")
            index = await uow.repositories.knowledge.activate(
                resource_id=resource.id,
                revision_id=revision_id,
                publication_id=publication_id,
                provider=self.embedder.provider,
                model=self.embedder.model,
                content_hash=digest,
                parts=parts,
                vectors=vectors,
            )
            await uow.repositories.provider_calls.settle_succeeded(call_id)
            await uow.repositories.jobs.finish(job_id, token, result={"index_id": str(index.id)})
            await self._owner(uow, actor, resource.id)
            return index.id

    async def _search_scope(
        self,
        uow: UnitOfWork,
        actor: ActorContext,
        run_id: UUID,
        *,
        expected_version: int | None = None,
    ) -> tuple[Run, tuple[UUID, ...]]:
        _, facts, run = await run_facts(
            uow,
            actor,
            run_id,
            Operation.CONTINUE_RUN if expected_version is not None else Operation.READ_RUN,
            expected_version=expected_version,
            require_context=expected_version is not None,
        )
        if run.status not in (RunStatus.QUEUED, RunStatus.RUNNING):
            raise ConflictError("当前运行不能检索")
        assert facts.target is not None
        owner = facts.target.mode is ConversationMode.OWNER
        require_allowed(
            actor,
            replace(
                facts,
                operation=Operation.SEARCH_PRIVATE_KNOWLEDGE
                if owner
                else Operation.SEARCH_PUBLIC_KNOWLEDGE,
                target=None,
                context=None,
            ),
        )
        selected = run.config_snapshot.get("resource_ids", [])
        try:
            ids = tuple(UUID(item) for item in selected)
        except (ValueError, TypeError, AttributeError) as error:
            raise ConflictError("运行资源范围损坏") from error
        indexes = await uow.repositories.knowledge.allowed_indexes(
            owner_id=actor.user_id if owner else None,
            provider=self.embedder.provider,
            model=self.embedder.model,
            resource_ids=ids,
        )
        for resource_id in sorted({index.resource_id for index in indexes}):
            await uow.repositories.resources.get_for_update_or_raise(resource_id)
        # 等锁后重查，防止撤回正好发生在选范围与加锁之间。
        locked_resource_ids = {index.resource_id for index in indexes}
        indexes = await uow.repositories.knowledge.allowed_indexes(
            owner_id=actor.user_id if owner else None,
            provider=self.embedder.provider,
            model=self.embedder.model,
            resource_ids=ids,
        )
        return run, tuple(index.id for index in indexes if index.resource_id in locked_resource_ids)

    async def retrieve(
        self,
        actor: ActorContext,
        run_id: UUID,
        query: str,
        *,
        limit: int = 5,
        fence: TaskFence | None = None,
    ) -> tuple[Citation, ...]:
        if not query.strip() or len(query) > 8000 or not 1 <= limit <= 20:
            raise InvalidInputError("检索输入无效")
        async with self._uows() as uow:
            run, _ = await self._search_scope(uow, actor, run_id)
            if fence is not None:
                await run_facts(uow, actor, run_id, Operation.CONTINUE_RUN)
                await require_task(uow, actor, run, fence)
            version = run.version
            # 没有 job 的直接服务调用由 Run 当前版本/代际 + 稳定逻辑键仲裁。
            logical = f"retrieval:{run.id}:g{run.execution_generation}:v{version}:{hashlib.sha256(query.encode()).hexdigest()}"
            call = await self._prepare_external(
                uow,
                provider=self.embedder.provider,
                model=self.embedder.model,
                purpose=ProviderCallPurpose.EMBEDDING,
                key=f"{logical}:embedding",
                run_id=run.id,
                job_id=fence.job_id if fence else None,
            )
            call_id, call_key = call.id, external_idempotency_key(call.logical_call_key)
        require_outside_uow()
        vectors = await self.embedder.embed((query,), external_idempotency_key=call_key)
        validate_vectors(vectors, 1)
        async with self._uows() as uow:
            run, allowed = await self._search_scope(uow, actor, run_id)
            if fence is not None:
                await run_facts(uow, actor, run_id, Operation.CONTINUE_RUN)
                await require_task(uow, actor, run, fence)
            if run.version != version:
                raise OptimisticLockError("旧检索执行已失效")
            await uow.repositories.provider_calls.settle_succeeded(call_id)
            hits = await uow.repositories.knowledge.nearest(
                allowed, vectors[0], limit=min(40, limit * 3) if self.reranker else limit
            )
            rerank_call = None
            if self.reranker is not None and hits:
                rerank_call = await self._prepare_external(
                    uow,
                    provider=self.reranker.provider,
                    model=self.reranker.model,
                    purpose=ProviderCallPurpose.RERANK,
                    key=f"{logical}:rerank",
                    run_id=run.id,
                    job_id=fence.job_id if fence else None,
                )
                rerank_call_id = rerank_call.id
                rerank_key = external_idempotency_key(rerank_call.logical_call_key)
        if self.reranker is not None and hits:
            require_outside_uow()
            order = await self.reranker.rank(query, hits, external_idempotency_key=rerank_key)
            by_id = {hit.chunk_id: hit for hit in hits}
            if len(order) != len(set(order)) or any(item not in by_id for item in order):
                raise InvalidInputError("重排返回了无效片段")
            hits = tuple(by_id[item] for item in order[:limit])
        async with self._uows() as uow:
            run, allowed = await self._search_scope(uow, actor, run_id)
            if fence is not None:
                await require_task(uow, actor, run, fence)
            if run.version != version or any(hit.index_id not in allowed for hit in hits):
                raise OptimisticLockError("检索结果已失效")
            for hit in hits:
                resource = await uow.repositories.resources.get_or_raise(hit.resource_id)
                if resource.acl_version != hit.acl_version:
                    raise OptimisticLockError("来源权限已变化")
            result = []
            for hit in hits:
                source = await uow.repositories.knowledge.record(
                    run_id, hit, context_generation=run.execution_generation
                )
                result.append(
                    Citation(
                        source.id,
                        hit.text,
                        hit.resource_id,
                        hit.revision_id,
                        hit.publication_id,
                        hit.locator,
                    )
                )
            if fence is not None:
                await run_facts(uow, actor, run_id, Operation.CONTINUE_RUN)
                await require_task(uow, actor, run, fence)
            if rerank_call is not None:
                await uow.repositories.provider_calls.settle_succeeded(rerank_call_id)
            return tuple(result)

    async def capture_dependencies(
        self,
        actor: ActorContext,
        run_id: UUID,
        *,
        message_ids: tuple[UUID, ...] = (),
        summary_id: UUID | None = None,
        memory_ids: tuple[UUID, ...] = (),
        fence: TaskFence | None = None,
    ) -> tuple[str, ...]:
        """G 只能将此入口返回的历史/摘要/记忆与 retrieve 返回的片段送入模型。"""
        if len(message_ids) + len(memory_ids) > 200:
            raise InvalidInputError("上下文对象过多")
        async with self._uows() as uow:
            current_actor, facts, run = await run_facts(
                uow, actor, run_id, Operation.READ_RUN, require_context=False
            )
            if fence is not None and run.scope_epoch != facts.current_scope_epoch:
                job = await uow.repositories.jobs.get_or_raise(fence.job_id)
                manifest = run.config_snapshot.get("context_manifest", {})
                if not (
                    (job.payload or {}).get("rebuild_context") is True
                    and isinstance(manifest, dict)
                    and manifest.get("complete") is False
                ):
                    raise AuthorizationError(Decision(code=DenialCode.ACL_CONTEXT_INVALIDATED))
            assert facts.target is not None
            origin_ids: set[UUID] = set()
            texts = []
            for message_id in message_ids:
                message = await uow.session.get(Message, message_id)
                if (
                    message is None
                    or message.conversation_id != run.conversation_id
                    or message.status is not MessageStatus.COMPLETE
                ):
                    raise NotFoundError("历史消息不存在")
                texts.append(message.body_text)
                if message.run_id is not None and message.run_id != run_id:
                    origin_ids.add(message.run_id)
            if summary_id is not None:
                summary = await uow.session.get(ConversationSummary, summary_id)
                if (
                    summary is None
                    or summary.conversation_id != run.conversation_id
                    or summary.status is not SummaryStatus.ACTIVE
                    or summary.scope_epoch != facts.current_scope_epoch
                ):
                    raise NotFoundError("会话摘要已失效")
                texts.append(summary.body_text)
                summary_origins = (
                    await uow.session.scalars(
                        select(Message.run_id).where(
                            Message.conversation_id == run.conversation_id,
                            Message.seq <= summary.upto_message_seq,
                            Message.run_id.is_not(None),
                            Message.run_id != run_id,
                        )
                    )
                ).all()
                origin_ids.update(value for value in summary_origins if value is not None)
            for memory_id in memory_ids:
                memory = await uow.session.get(Memory, memory_id)
                if (
                    memory is None
                    or memory.user_id != actor.user_id
                    or facts.target.mode is not ConversationMode.OWNER
                    or memory.deleted_at is not None
                    or memory.confirmed_at is None
                    or (memory.expires_at is not None and memory.expires_at <= facts.now)
                ):
                    raise NotFoundError("记忆不存在")
                require_allowed(
                    actor,
                    replace(
                        facts,
                        operation=Operation.SEARCH_PRIVATE_KNOWLEDGE,
                        target=None,
                        context=None,
                    ),
                )
                texts.append(memory.content_text)
                if memory.origin_run_id is not None:
                    origin_ids.add(memory.origin_run_id)
                if memory.origin_message_id is not None:
                    origin_message = await uow.session.get(Message, memory.origin_message_id)
                    if origin_message is None or origin_message.run_id is None:
                        raise ConflictError("记忆来源消息不可验证")
                    origin_ids.add(origin_message.run_id)
            inherited: list[RunSource] = []
            for origin_id in sorted(origin_ids):
                origin = await uow.session.get(Run, origin_id)
                if origin is None or origin.user_id != actor.user_id:
                    raise NotFoundError("上下文来源运行不存在")
                manifest = origin.config_snapshot.get("context_manifest", {})
                if (
                    not isinstance(manifest, dict)
                    or manifest.get("schema_version") != 1
                    or manifest.get("complete") is not True
                ):
                    raise ConflictError("历史来源闭包不完整，必须重建上下文")
                inherited.extend(await uow.repositories.knowledge.sources(origin_id))
            for resource_id in sorted(
                {
                    source.resource_id
                    for source in (
                        *inherited,
                        *await uow.repositories.knowledge.sources(
                            run_id, context_generation=run.execution_generation
                        ),
                    )
                    if source.resource_id is not None
                }
            ):
                await uow.repositories.resources.get_for_update(resource_id)
            if fence is not None:
                await require_task(uow, actor, run, fence)
            for source in inherited:
                dependency = await source_fact(uow, source, run.user_id)
                if not source_decision(current_actor, facts, dependency).allowed:
                    raise ConflictError("历史/摘要/记忆的来源已失效")
                values = {
                    name: getattr(source, name)
                    for name in (
                        "source_type",
                        "resource_id",
                        "revision_id",
                        "publication_id",
                        "index_id",
                        "chunk_id",
                        "observed_acl_version",
                        "locator",
                        "web_url",
                        "web_title",
                        "fetched_at",
                        "excerpt",
                    )
                }
                await uow.repositories.knowledge.record_values(
                    run_id,
                    f"dependency:{source.id}",
                    values,
                    context_generation=run.execution_generation,
                )
            # 已有直接来源也必须有效，不能借 manifest 更新给旧来源重新授权。
            for source in await uow.repositories.knowledge.sources(
                run_id, context_generation=run.execution_generation
            ):
                if not source_decision(
                    current_actor, facts, await source_fact(uow, source, run.user_id)
                ).allowed:
                    raise ConflictError("来源已失效")
            run.scope_epoch = facts.current_scope_epoch
            run.config_snapshot = {
                **run.config_snapshot,
                "context_manifest": {
                    "schema_version": 1,
                    "complete": True,
                    "context_generation": run.execution_generation,
                    "message_ids": [str(value) for value in message_ids],
                    "summary_id": str(summary_id) if summary_id else None,
                    "memory_ids": [str(value) for value in memory_ids],
                },
            }
            run.version += 1
            await uow.session.flush()
            if fence is not None:
                await run_facts(uow, actor, run_id, Operation.CONTINUE_RUN)
                await require_task(uow, actor, run, fence)
            return tuple(texts)

    async def current_sources(
        self, actor: ActorContext, run_id: UUID, *, fence: TaskFence
    ) -> tuple[Citation, ...]:
        """恢复时重读当前代际获准片段；不复用 checkpoint 或旧工具载荷。"""
        async with self._uows() as uow:
            _, _, run = await run_facts(uow, actor, run_id, Operation.CONTINUE_RUN)
            await require_task(uow, actor, run, fence)
            sources = await uow.repositories.knowledge.sources(
                run_id, context_generation=run.execution_generation
            )
            result = []
            for source in sources:
                chunk = (
                    await uow.session.get(KnowledgeChunk, source.chunk_id)
                    if source.chunk_id
                    else None
                )
                if source.chunk_id is not None and (
                    chunk is None or chunk.index_id != source.index_id
                ):
                    raise ConflictError("获准来源片段缺失或归属无效")
                text = chunk.content_text if chunk else source.excerpt or ""
                if text:
                    result.append(
                        Citation(
                            source.id,
                            text,
                            source.resource_id,
                            source.revision_id,
                            source.publication_id,
                            source.locator,
                        )
                    )
            await run_facts(uow, actor, run_id, Operation.CONTINUE_RUN)
            await require_task(uow, actor, run, fence)
            return tuple(result)

    async def record_web_sources(
        self, actor: ActorContext, run_id: UUID, pages: tuple[WebPage, ...], *, fetched_at: datetime
    ) -> tuple[Citation, ...]:
        if (
            fetched_at.tzinfo is None
            or len(pages) > 20
            or any(not page.text.strip() or len(page.text) > 8000 for page in pages)
        ):
            raise InvalidInputError("联网来源载荷无效")
        async with self._uows() as uow:
            _, facts, run = await run_facts(
                uow, actor, run_id, Operation.READ_RUN, require_context=False
            )
            require_allowed(actor, replace(facts, operation=Operation.SEARCH_WEB, context=None))
            if run.status not in (RunStatus.QUEUED, RunStatus.RUNNING) or fetched_at > facts.now:
                raise ConflictError("联网来源时间或运行状态无效")
            result = []
            for page in pages:
                url = validate_url(page.url)
                key = "web:" + hashlib.sha256(f"{url}\n{page.text}".encode()).hexdigest()
                source = await uow.repositories.knowledge.record_values(
                    run_id,
                    key,
                    {
                        "source_type": RunSourceType.WEB,
                        "observed_acl_version": 0,
                        "web_url": url,
                        "web_title": page.title[:300],
                        "fetched_at": fetched_at,
                        "excerpt": page.text,
                        "locator": {"kind": "web", "url": url},
                    },
                    context_generation=run.execution_generation,
                )
                result.append(Citation(source.id, page.text, None, None, None, source.locator))
            return tuple(result)

    async def citation(self, actor: ActorContext, run_id: UUID, citation_id: UUID) -> Citation:
        async with self._uows() as uow:
            current_actor, facts, run = await run_facts(
                uow, actor, run_id, Operation.READ_RUN, require_context=False
            )
            source = await uow.session.get(RunSource, citation_id)
            if source is None or source.run_id != run_id:
                raise NotFoundError("引用不存在")
            dependency = await source_fact(uow, source, run.user_id)
            assert facts.target is not None
            require_allowed(
                current_actor,
                replace(
                    facts,
                    operation=Operation.READ_CITATION,
                    target=replace(
                        facts.target,
                        object_id=citation_id,
                        kind=TargetKind.CITATION,
                        resource_id=source.resource_id,
                    ),
                    source=dependency,
                    context=None,
                ),
            )
            chunk = (
                await uow.session.get(KnowledgeChunk, source.chunk_id)
                if source.chunk_id is not None
                else None
            )
            if chunk is not None and chunk.index_id != source.index_id:
                raise ConflictError("引用片段归属无效")
            return Citation(
                source.id,
                chunk.content_text if chunk else source.excerpt or "",
                source.resource_id,
                source.revision_id,
                source.publication_id,
                source.locator,
            )

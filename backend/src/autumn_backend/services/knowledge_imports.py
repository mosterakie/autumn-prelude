"""知识导入受理与 Worker 准备阶段；网络、对象存储和解析均在事务之外。"""

import asyncio
import hashlib
import json
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from autumn_backend.db.enums import FileObjectStatus, JobPhase, ResourceKind
from autumn_backend.db.models import Job, Resource
from autumn_backend.db.session import UnitOfWork, UnitOfWorkFactory
from autumn_backend.errors import (
    ConfigurationError,
    ConflictError,
    IdempotencyConflictError,
    InvalidInputError,
    NotFoundError,
    OptimisticLockError,
)
from autumn_backend.knowledge.text import extract
from autumn_backend.knowledge.web import validate_url
from autumn_backend.policies import ActorContext
from autumn_backend.policies.facts import Operation
from autumn_backend.repositories.jobs import JobSpec
from autumn_backend.repositories.resources import RevisionDraft
from autumn_backend.services.access import lock_authentication, publication_facts, require_allowed
from autumn_backend.services.knowledge import KnowledgeService
from autumn_backend.services.resources import ResourceService, validate_draft
from autumn_backend.services.storage import StorageService, inspect_upload
from autumn_backend.services.tasks import job_dto


class ImportURL(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str = Field(min_length=1, max_length=2048)
    mode: Literal["bookmark_only", "knowledge_only", "bookmark_and_knowledge"]
    title: str | None = Field(default=None, max_length=300)
    tags: tuple[str, ...] = Field(default=(), max_length=20)


class KnowledgeImportService:
    def __init__(
        self,
        uows: UnitOfWorkFactory,
        storage: StorageService,
        knowledge: KnowledgeService | None,
    ) -> None:
        self.uows, self.storage, self.knowledge = uows, storage, knowledge
        self.resources = ResourceService(uows, storage)

    async def _authorize(self, uow: UnitOfWork, actor: ActorContext) -> None:
        require_allowed(
            actor,
            await publication_facts(
                uow, Operation.INGEST_RESOURCE, await lock_authentication(uow, actor), None
            ),
        )

    async def authorize(self, actor: ActorContext, *, needs_embedding: bool = True) -> None:
        async with self.uows() as uow:
            await self._authorize(uow, actor)
        if needs_embedding and self.knowledge is None:
            raise ConfigurationError("知识导入需要配置嵌入服务")

    @staticmethod
    def _key(actor: ActorContext, scope: str, key: str) -> str:
        if not key.strip() or len(key) > 128:
            raise InvalidInputError("导入幂等标识无效")
        return f"knowledge.{scope}:{actor.user_id}:{key}"

    @staticmethod
    def _hash(value: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    async def _existing(self, uow: UnitOfWork, key: str, request_hash: str) -> Job | None:
        job = await uow.session.scalar(select(Job).where(Job.idempotency_key == key))
        if job is not None and (job.payload or {}).get("request_hash") != request_hash:
            raise IdempotencyConflictError("导入标识已用于不同内容")
        return job

    async def _response(self, uow: UnitOfWork, actor: ActorContext, job: Job) -> dict[str, Any]:
        assert job.resource_id is not None
        resource = await self.resources._resource(uow, actor, job.resource_id)
        bookmark_id = (job.payload or {}).get("bookmark_id")
        bookmark = (
            await self.resources._resource(uow, actor, UUID(bookmark_id)) if bookmark_id else None
        )
        return {
            "resource": await self.resources._dto(uow, resource),
            "job": job_dto(job),
            "bookmark": await self.resources._dto(uow, bookmark) if bookmark else None,
        }

    def _payload(self, resource: Resource, source: dict[str, Any], digest: str) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "revision_id": str(resource.current_revision_id),
            "publication_id": None,
            "resource_version": resource.version,
            "acl_version": resource.acl_version,
            "provider": self.knowledge.embedder.provider if self.knowledge else None,
            "model": self.knowledge.embedder.model if self.knowledge else None,
            "import": source,
            "request_hash": digest,
        }

    async def file(
        self, actor: ActorContext, *, key: str, filename: str, title: str, data: bytes, mime: str
    ) -> dict[str, Any]:
        if not title.strip() or len(title.strip()) > 300:
            raise InvalidInputError("标题无效")
        await self.authorize(actor)
        actual_mime = inspect_upload(filename, data, mime)
        identity = self._key(actor, "file", key)
        digest = self._hash(
            {
                "title": title.strip(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "mime": actual_mime,
            }
        )
        async with self.uows() as uow:
            await self._authorize(uow, actor)
            existing = await self._existing(uow, identity, digest)
            if existing:
                return await self._response(uow, actor, existing)
        file = await self.storage.upload(
            actor,
            idempotency_key=key,
            filename=filename,
            data=data,
            media_type=mime,
            queue_finalize=False,
        )
        async with self.uows() as uow:
            await self._authorize(uow, actor)
            existing = await self._existing(uow, identity, digest)
            if existing:
                return await self._response(uow, actor, existing)
            record = await uow.repositories.files.get_for_update_or_raise(file.id)
            if record.resource_id is not None or record.deleted_at is not None:
                raise IdempotencyConflictError("文件已用于其他资源")
            assert actor.user_id is not None
            resource = await uow.repositories.resources.create(
                owner_id=actor.user_id,
                kind=ResourceKind.DOCUMENT,
                slug=f"document-{file.id.hex}",
                draft=RevisionDraft(
                    title=title.strip(),
                    file_object_key=file.object_key,
                    file_sha256=file.sha256,
                    media_type=file.media_type,
                    byte_size=file.byte_size,
                ),
            )
            await uow.repositories.files.attach(file.id, resource.id, allow_staged=True)
            job = (
                await uow.repositories.jobs.enqueue(
                    JobSpec(
                        kind="knowledge.ingest",
                        idempotency_key=identity,
                        actor_id=actor.user_id,
                        auth_session_id=actor.auth_session_id,
                        resource_id=resource.id,
                        payload=self._payload(
                            resource, {"kind": "file", "file_id": str(file.id)}, digest
                        ),
                    )
                )
            ).record
            return await self._response(uow, actor, job)

    async def url(self, actor: ActorContext, command: ImportURL, *, key: str) -> dict[str, Any]:
        await self.authorize(actor, needs_embedding=command.mode != "bookmark_only")
        url = validate_url(command.url.strip())
        draft = validate_draft(
            RevisionDraft(
                title=(command.title or "").strip() or url[:300], url=url, tags=command.tags
            ),
            ResourceKind.BOOKMARK,
        )
        source = {
            "kind": "url",
            "url": url,
            "title": (command.title or "").strip(),
            "tags": list(draft.tags),
            "mode": command.mode,
        }
        digest = self._hash(source)
        identity = self._key(actor, "url", key)
        async with self.uows() as uow:
            await self._authorize(uow, actor)
            existing = await self._existing(uow, identity, digest)
            if existing:
                return await self._response(uow, actor, existing)
            assert actor.user_id is not None
            slug = hashlib.sha256(identity.encode()).hexdigest()
            resource = await uow.repositories.resources.create(
                owner_id=actor.user_id,
                kind=ResourceKind.BOOKMARK
                if command.mode == "bookmark_only"
                else ResourceKind.WEBPAGE,
                slug=f"import-{slug}",
                draft=draft,
            )
            payload = self._payload(resource, source, digest)
            if command.mode == "bookmark_and_knowledge":
                bookmark = await uow.repositories.resources.create(
                    owner_id=actor.user_id,
                    kind=ResourceKind.BOOKMARK,
                    slug=f"bookmark-{slug}",
                    draft=RevisionDraft(
                        title=draft.title,
                        url=url,
                        tags=draft.tags,
                        linked_source_id=resource.id,
                    ),
                )
                payload["bookmark_id"] = str(bookmark.id)
            job = (
                await uow.repositories.jobs.enqueue(
                    JobSpec(
                        kind="knowledge.bookmark"
                        if command.mode == "bookmark_only"
                        else "knowledge.ingest",
                        idempotency_key=identity,
                        actor_id=actor.user_id,
                        auth_session_id=actor.auth_session_id,
                        resource_id=resource.id,
                        payload=payload,
                    )
                )
            ).record
            return await self._response(uow, actor, job)

    async def refresh(
        self, actor: ActorContext, resource_id: UUID, *, key: str, expected_version: int
    ) -> dict[str, Any]:
        await self.authorize(actor)
        identity = self._key(actor, "refresh", key)
        digest = self._hash({"resource_id": str(resource_id), "expected_version": expected_version})
        async with self.uows() as uow:
            await self._authorize(uow, actor)
            existing = await self._existing(uow, identity, digest)
            if existing:
                return await self._response(uow, actor, existing)
            resource = await self.resources._resource(uow, actor, resource_id)
            if resource.current_revision_id is None:
                raise InvalidInputError("资源没有当前版本")
            revision = await uow.repositories.resources.revision(
                resource.id, resource.current_revision_id
            )
            if resource.version != expected_version:
                raise OptimisticLockError("资源版本已变化")
            if resource.kind is not ResourceKind.WEBPAGE or revision is None or not revision.url:
                raise InvalidInputError("仅网页资料支持重新抓取")
            source = {
                "kind": "url",
                "url": validate_url(revision.url),
                "title": revision.title,
                "tags": revision.tags,
                "mode": "knowledge_only",
            }
            job = (
                await uow.repositories.jobs.enqueue(
                    JobSpec(
                        kind="knowledge.ingest",
                        idempotency_key=identity,
                        actor_id=actor.user_id,
                        auth_session_id=actor.auth_session_id,
                        resource_id=resource.id,
                        payload=self._payload(resource, source, digest),
                    )
                )
            ).record
            return await self._response(uow, actor, job)

    async def _input(
        self, uow: UnitOfWork, actor: ActorContext, job_id: UUID, token: UUID
    ) -> tuple[Job, Resource]:
        await self._authorize(uow, actor)
        probe = await uow.repositories.jobs.get_or_raise(job_id)
        if (
            probe.actor_id != actor.user_id
            or probe.auth_session_id != actor.auth_session_id
            or probe.resource_id is None
            or probe.kind not in ("knowledge.ingest", "knowledge.bookmark")
        ):
            raise NotFoundError("导入任务不存在")
        resource = await self.resources._resource(uow, actor, probe.resource_id)
        job = await uow.repositories.jobs.require_lease(job_id, token)
        payload = job.payload or {}
        if job.kind == "knowledge.ingest" and (
            self.knowledge is None
            or (payload.get("schema_version"), payload.get("provider"), payload.get("model"))
            != (1, self.knowledge.embedder.provider, self.knowledge.embedder.model)
        ):
            raise ConflictError("嵌入服务配置与任务输入不一致")
        if (resource.version, resource.acl_version, str(resource.current_revision_id)) != (
            payload.get("resource_version"),
            payload.get("acl_version"),
            payload.get("revision_id"),
        ) or not resource.is_active:
            raise OptimisticLockError("导入内容或权限已变化")
        return job, resource

    async def execute(self, actor: ActorContext, job_id: UUID, token: UUID) -> None:
        async with self.uows() as uow:
            await self._authorize(uow, actor)
            probe = await uow.repositories.jobs.require_lease(job_id, token)
            existing_index = "import" not in (probe.payload or {})
        if existing_index:
            if self.knowledge is None:
                raise ConfigurationError("索引需要配置嵌入服务")
            # 公开索引可以绑定旧的公开 revision，继续由原索引服务检查完整投影资格。
            await self.knowledge.build_index(actor, job_id, token)
            return
        async with self.uows() as uow:
            job, resource = await self._input(uow, actor, job_id, token)
            if job.kind == "knowledge.bookmark":
                await uow.repositories.jobs.finish(
                    job_id, token, result={"resource_id": str(resource.id)}
                )
                await self._authorize(uow, actor)
                return
            payload = dict(job.payload or {})
            source = payload.get("import")
            prepared = payload.get("prepared", False)
            if source and not prepared:
                job.phase = JobPhase.PARSING if source["kind"] == "file" else JobPhase.FETCHING
                job.version += 1
                await uow.session.flush()
        if source and not prepared:
            if self.knowledge is None:
                raise ConfigurationError("知识导入需要配置嵌入服务")
            if source["kind"] == "file":
                async with self.uows() as uow:
                    await self._input(uow, actor, job_id, token)
                    file = await uow.repositories.files.get_for_update_or_raise(
                        UUID(source["file_id"])
                    )
                    if (
                        file.owner_id != actor.user_id
                        or file.resource_id != resource.id
                        or file.deleted_at is not None
                    ):
                        raise NotFoundError("导入文件不存在")
                    if file.status not in (FileObjectStatus.STAGED, FileObjectStatus.READY):
                        raise NotFoundError("导入文件不可读")
                    file_id, object_key, sha, size, mime = (
                        file.id,
                        file.object_key,
                        file.sha256,
                        file.byte_size,
                        file.media_type,
                    )
                await self.storage.store.promote(object_key, sha, size)
                async with self.uows() as uow:
                    await self._input(uow, actor, job_id, token)
                    await uow.repositories.files.ready(file_id)
                    await self._authorize(uow, actor)
                data = await self.storage.read_private(actor, file_id)
                parts = await asyncio.to_thread(extract, data, mime)
                body, fetched_title, fetched_url = "\n\n".join(p.text for p in parts), None, None
            else:
                page = await self.knowledge.fetcher.fetch(source["url"])
                fetched_url = validate_url(page.url)
                if not page.text.strip() or len(page.text) > 2_000_000:
                    raise InvalidInputError("网页正文为空或过大")
                body, fetched_title = page.text, page.title[:300].strip() or fetched_url[:300]
            async with self.uows() as uow:
                job, resource = await self._input(uow, actor, job_id, token)
                assert resource.current_revision_id is not None
                previous = await uow.repositories.resources.revision(
                    resource.id, resource.current_revision_id
                )
                assert previous is not None and actor.user_id is not None
                resource = await uow.repositories.resources.revise(
                    resource.id,
                    expected_version=resource.version,
                    created_by=actor.user_id,
                    draft=RevisionDraft(
                        title=source.get("title") or fetched_title or previous.title,
                        body_text=body,
                        url=fetched_url or previous.url,
                        tags=tuple(previous.tags),
                        private_note=previous.private_note,
                        linked_source_id=previous.linked_source_id,
                        content_format=previous.content_format,
                        file_object_key=previous.file_object_key,
                        file_sha256=previous.file_sha256,
                        media_type=previous.media_type,
                        byte_size=previous.byte_size,
                    ),
                )
                job.payload = {
                    **payload,
                    "prepared": True,
                    "revision_id": str(resource.current_revision_id),
                    "resource_version": resource.version,
                    "acl_version": resource.acl_version,
                }
                job.phase, job.version = JobPhase.EMBEDDING, job.version + 1
                await uow.session.flush()
                await self._authorize(uow, actor)
                await uow.repositories.jobs.require_lease(job_id, token)
        if self.knowledge is None:
            raise ConfigurationError("知识导入需要配置嵌入服务")
        await self.knowledge.build_index(actor, job_id, token)

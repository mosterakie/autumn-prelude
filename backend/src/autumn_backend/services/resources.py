"""私人资源 CRUD：当前权限、不可变版本、幂等动作和删除同一事务。"""

import hashlib
import json
import re
from dataclasses import asdict
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from autumn_backend.db.enums import ActionStatus, ActionType, AuditResult, ResourceKind
from autumn_backend.db.models import Action, FileObject, Job, Resource, ResourceVersion
from autumn_backend.db.session import UnitOfWork, UnitOfWorkFactory
from autumn_backend.errors import (
    ConflictError,
    IdempotencyConflictError,
    InvalidInputError,
    NotFoundError,
    OptimisticLockError,
)
from autumn_backend.policies import ActorContext
from autumn_backend.policies.facts import Operation
from autumn_backend.repositories.audit import AuditMetadata
from autumn_backend.repositories.jobs import JobSpec
from autumn_backend.repositories.resources import RevisionDraft
from autumn_backend.services.access import lock_authentication, publication_facts, require_allowed
from autumn_backend.services.storage import StorageService


class ResourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CreateResource(ResourceInput):
    kind: Literal["article", "bookmark"]
    title: str = Field(min_length=1, max_length=500)
    body_text: str | None = Field(default=None, max_length=200000)
    url: str | None = Field(default=None, max_length=2048)
    private_note: str | None = Field(default=None, max_length=64000)
    tags: tuple[str, ...] = Field(default=(), max_length=20)
    slug: str | None = Field(default=None, min_length=1, max_length=160)


class EditResource(ResourceInput):
    expected_version: int = Field(ge=0)
    title: str | None = Field(default=None, max_length=500)
    body_text: str | None = Field(default=None, max_length=200000)
    url: str | None = Field(default=None, max_length=2048)
    private_note: str | None = Field(default=None, max_length=64000)
    tags: tuple[str, ...] | None = Field(default=None, max_length=20)


class DeleteResource(ResourceInput):
    expected_version: int = Field(ge=0)
    expected_acl_version: int = Field(ge=0)


def revision_dto(revision: ResourceVersion) -> dict[str, Any]:
    # 文件只返回受控元数据，不把对象存储键作为公共下载路径。
    return {
        "id": revision.id,
        "revision_no": revision.revision_no,
        "title": revision.title,
        "body_text": revision.body_text,
        "url": revision.url,
        "private_note": revision.private_note,
        "tags": revision.tags,
        "created_at": revision.created_at,
        "file": {
            "media_type": revision.media_type,
            "byte_size": revision.byte_size,
            "sha256": revision.file_sha256,
        }
        if revision.file_object_key
        else None,
    }


def validate_draft(draft: RevisionDraft, kind: ResourceKind) -> RevisionDraft:
    if not draft.title or not draft.title.strip() or len(draft.title) > 500:
        raise InvalidInputError("标题无效")
    if (
        draft.tags is None
        or len(draft.tags) > 20
        or any(not tag.strip() or len(tag) > 40 for tag in draft.tags)
    ):
        raise InvalidInputError("标签无效")
    if kind is ResourceKind.BOOKMARK and not draft.url:
        raise InvalidInputError("收藏必须包含 URL")
    if draft.url:
        parsed = urlsplit(draft.url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise InvalidInputError("URL 无效")
    return RevisionDraft(
        **{
            **asdict(draft),
            "title": draft.title.strip(),
            "tags": tuple(sorted({tag.strip() for tag in draft.tags})),
        }
    )


class ResourceService:
    def __init__(self, uows: UnitOfWorkFactory, storage: StorageService | None = None) -> None:
        self.uows = uows
        self.storage = storage

    async def file(
        self, actor: ActorContext, resource_id: UUID, revision_id: UUID
    ) -> tuple[bytes, str]:
        async with self.uows() as uow:
            await self._resource(uow, actor, resource_id)
            revision = await uow.repositories.resources.revision(resource_id, revision_id)
            if revision is None or not revision.file_object_key or not revision.media_type:
                raise NotFoundError("原文件不存在")
            file = await uow.repositories.files.by_key(revision.file_object_key)
            if file is None or file.resource_id != resource_id:
                raise NotFoundError("原文件不存在")
            identifier, mime = file.id, revision.media_type
        if self.storage is None:
            raise ConflictError("文件存储未配置")
        content = await self.storage.read_private(actor, identifier)
        return content, mime

    async def _authorize(
        self, uow: UnitOfWork, actor: ActorContext, resource: Resource | None = None
    ) -> None:
        require_allowed(
            actor,
            await publication_facts(
                uow,
                Operation.READ_PRIVATE_RESOURCE
                if resource is not None
                else Operation.CREATE_RESOURCE,
                await lock_authentication(uow, actor),
                resource,
            ),
        )

    async def _resource(self, uow: UnitOfWork, actor: ActorContext, resource_id: UUID) -> Resource:
        # User/session 排在 Resource 锁之前。
        auth = await lock_authentication(uow, actor)
        resource = await uow.repositories.resources.get_for_update(resource_id)
        require_allowed(
            actor, await publication_facts(uow, Operation.READ_PRIVATE_RESOURCE, auth, resource)
        )
        if resource is None:
            raise NotFoundError("资源不存在")
        return resource

    async def _dto(self, uow: UnitOfWork, resource: Resource) -> dict[str, Any]:
        current = (
            await uow.repositories.resources.revision(resource.id, resource.current_revision_id)
            if resource.current_revision_id
            else None
        )
        publication = await uow.repositories.publications.current_for_resource(resource.id)
        jobs = (
            (
                await uow.session.execute(
                    select(Job)
                    .where(
                        Job.resource_id == resource.id,
                        Job.actor_id == resource.owner_id,
                        Job.status.in_(("queued", "running", "waiting_auth", "cancelling")),
                    )
                    .order_by(Job.created_at.desc(), Job.id.desc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        return {
            "id": resource.id,
            "kind": resource.kind.value,
            "slug": resource.slug,
            "version": resource.version,
            "acl_version": resource.acl_version,
            "created_at": resource.created_at,
            "archived_at": resource.archived_at,
            "current_revision": revision_dto(current) if current else None,
            "publication": {
                "id": publication.id,
                "revision_id": publication.revision_id,
                "publication_no": publication.publication_no,
                "public_fields": publication.public_fields,
                "ai_enabled": publication.ai_enabled,
                "raw_download_enabled": publication.raw_download_enabled,
                "published_at": publication.created_at,
            }
            if publication
            else None,
            "processing_jobs": [
                {
                    "id": job.id,
                    "kind": job.kind,
                    "status": job.status.value,
                    "phase": job.phase.value if job.phase else None,
                    "progress": job.progress,
                    "resource_id": resource.id,
                    "error": {"code": job.error_code} if job.error_code else None,
                }
                for job in jobs
            ],
        }

    async def list(
        self, actor: ActorContext, *, kind: str | None, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        try:
            selected = ResourceKind(kind) if kind else None
        except ValueError as error:
            raise InvalidInputError("资源类型无效") from error
        async with self.uows() as uow:
            await self._authorize(uow, actor)
            assert actor.user_id is not None
            page = await uow.repositories.resources.for_owner(
                actor.user_id, kind=selected, limit=limit, cursor=cursor
            )
            return {
                "items": [await self._dto(uow, resource) for resource in page.items],
                "next_cursor": page.next_cursor,
            }

    async def read(self, actor: ActorContext, resource_id: UUID) -> dict[str, Any]:
        async with self.uows() as uow:
            return await self._dto(uow, await self._resource(uow, actor, resource_id))

    async def versions(
        self, actor: ActorContext, resource_id: UUID, *, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        async with self.uows() as uow:
            await self._resource(uow, actor, resource_id)
            page = await uow.repositories.resources.revisions(
                resource_id, limit=limit, cursor=cursor
            )
            return {
                "items": [revision_dto(revision) for revision in page.items],
                "next_cursor": page.next_cursor,
            }

    async def _replay(
        self,
        uow: UnitOfWork,
        actor: ActorContext,
        action: Action | None,
        operation: ActionType,
        parameters: dict[str, Any],
        digest: str,
    ) -> dict[str, Any] | None:
        if action is None:
            return None
        if (
            action.type is not operation
            or action.parameters_hash != digest
            or action.parameters != parameters
        ):
            raise IdempotencyConflictError("资源请求身份已用于不同内容")
        if (
            action.status is not ActionStatus.SUCCEEDED
            or action.result is None
            or action.target_resource_id is None
        ):
            raise ConflictError("资源操作状态异常")
        resource = await uow.repositories.resources.get_for_update(action.target_resource_id)
        if resource is None or resource.owner_id != actor.user_id:
            raise NotFoundError("资源不存在")
        if resource.deleted_at is not None:
            return {
                "id": resource.id,
                "deleted": True,
                "version": resource.version,
                "acl_version": resource.acl_version,
            }
        return await self._dto(uow, resource)

    async def mutate(
        self,
        actor: ActorContext,
        *,
        key: str,
        command: CreateResource | EditResource | DeleteResource,
        resource_id: UUID | None = None,
    ) -> dict[str, Any]:
        if not key.strip() or len(key) > 128:
            raise InvalidInputError("幂等键无效")
        operation = (
            ActionType.CREATE_RESOURCE
            if isinstance(command, CreateResource)
            else (
                ActionType.UPDATE_RESOURCE
                if isinstance(command, EditResource)
                else ActionType.DELETE
            )
        )
        parameters = {
            "schema_version": 2,
            "operation": operation.value,
            "resource_id": str(resource_id) if resource_id else None,
            "input": command.model_dump(mode="json", exclude_unset=True),
        }
        digest = hashlib.sha256(
            json.dumps(
                parameters, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()
        async with self.uows() as uow:
            await self._authorize(uow, actor)
            assert actor.user_id is not None and actor.auth_session_id is not None
            replay = await self._replay(
                uow,
                actor,
                await uow.repositories.actions.by_actor_key(actor.user_id, key),
                operation,
                parameters,
                digest,
            )
            if replay is not None:
                return replay
            if isinstance(command, CreateResource):
                if resource_id is not None:
                    raise InvalidInputError("创建资源不接受目标 ID")
                slug = command.slug or uuid4().hex
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,159}", slug):
                    raise InvalidInputError("slug 无效")
                draft = validate_draft(
                    RevisionDraft(
                        title=command.title,
                        body_text=command.body_text,
                        url=command.url,
                        private_note=command.private_note,
                        tags=command.tags,
                    ),
                    ResourceKind(command.kind),
                )
                resource = await uow.repositories.resources.create(
                    owner_id=actor.user_id, kind=ResourceKind(command.kind), slug=slug, draft=draft
                )
            else:
                if resource_id is None:
                    raise InvalidInputError("缺少目标资源")
                resource = await self._resource(uow, actor, resource_id)
                if resource.version != command.expected_version:
                    raise OptimisticLockError("原稿已变化")
                if isinstance(command, EditResource):
                    if resource.kind not in {ResourceKind.ARTICLE, ResourceKind.BOOKMARK}:
                        raise InvalidInputError("文件和网页资料需要专用入库/刷新流程")
                    assert resource.current_revision_id is not None
                    revision = await uow.repositories.resources.revision(
                        resource.id, resource.current_revision_id
                    )
                    assert revision is not None
                    updates = command.model_dump(exclude_unset=True)
                    updates.pop("expected_version")
                    if not updates:
                        raise InvalidInputError("修改不能为空")
                    draft = validate_draft(
                        RevisionDraft(
                            **{
                                field: updates.get(field, getattr(revision, field))
                                for field in ("title", "body_text", "url", "private_note", "tags")
                            }
                        ),
                        resource.kind,
                    )
                    resource = await uow.repositories.resources.revise(
                        resource.id,
                        expected_version=command.expected_version,
                        draft=draft,
                        created_by=actor.user_id,
                    )
                else:
                    resource = await uow.repositories.resources.soft_delete(
                        resource.id,
                        expected_version=command.expected_version,
                        expected_acl_version=command.expected_acl_version,
                    )
                    # 先阻断 DB 访问与检索，物理文件由 storage.delete 异步收敛。
                    files = (
                        (
                            await uow.session.execute(
                                select(FileObject)
                                .where(FileObject.resource_id == resource.id)
                                .order_by(FileObject.id)
                            )
                        )
                        .scalars()
                        .all()
                    )
                    for file in files:
                        await uow.repositories.files.pending_delete(file.id)
                        await uow.repositories.jobs.enqueue(
                            JobSpec(
                                kind="storage.delete",
                                idempotency_key=f"storage.delete:{file.id}",
                                payload={"file_id": str(file.id)},
                                actor_id=actor.user_id,
                                auth_session_id=actor.auth_session_id,
                            )
                        )
                    await uow.repositories.jobs.enqueue(
                        JobSpec(
                            kind="knowledge.resource_cleanup",
                            idempotency_key=f"knowledge.resource_cleanup:{resource.id}:{resource.acl_version}",
                            payload={
                                "resource_id": str(resource.id),
                                "acl_version": resource.acl_version,
                            },
                            actor_id=actor.user_id,
                            resource_id=resource.id,
                            auth_session_id=actor.auth_session_id,
                        )
                    )
            action = await uow.repositories.actions.begin_explicit_content(
                actor_id=actor.user_id,
                auth_session_id=actor.auth_session_id,
                action_type=operation,
                resource_id=resource.id,
                key=key,
                parameters=parameters,
                digest=digest,
                expected_version=getattr(command, "expected_version", None),
                expected_acl_version=getattr(command, "expected_acl_version", None),
            )
            await uow.repositories.actions.succeed_explicit(
                action.id,
                {
                    "resource_id": str(resource.id),
                    "resource_version": resource.version,
                    "acl_version": resource.acl_version,
                },
            )
            await uow.repositories.audit_events.record(
                event_type=f"resource.{operation.value}",
                result=AuditResult.SUCCEEDED,
                actor_id=actor.user_id,
                action_id=action.id,
                resource_id=resource.id,
                after_version=resource.version,
                metadata=AuditMetadata(
                    changed_fields=("current_revision_id",)
                    if not isinstance(command, DeleteResource)
                    else ("deleted_at",)
                ),
            )
            await self._authorize(uow, actor)
            return (
                {
                    "id": resource.id,
                    "deleted": True,
                    "version": resource.version,
                    "acl_version": resource.acl_version,
                }
                if isinstance(command, DeleteResource)
                else await self._dto(uow, resource)
            )

"""公开读只使用当前投影；下载前后复核撤回与文件逻辑状态。"""

import hashlib
from typing import Any
from uuid import UUID

from autumn_backend.db.enums import FileObjectStatus, ResourceKind
from autumn_backend.db.models import Publication, Resource
from autumn_backend.db.session import UnitOfWork, UnitOfWorkFactory
from autumn_backend.errors import ConflictError, NotFoundError
from autumn_backend.policies import ActorContext, ActorRole
from autumn_backend.policies.facts import Operation, PolicyFacts, PublicationFacts, ResourceFacts
from autumn_backend.services.access import require_allowed
from autumn_backend.storage.local import LocalObjectStore


def public_dto(resource: Resource, publication: Publication) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": resource.id,
        "kind": resource.kind.value,
        "slug": resource.slug,
        "publication_id": publication.id,
        "publication_no": publication.publication_no,
        "title": publication.public_title if "title" in publication.public_fields else "公开资料",
        "tags": publication.public_tags if "tags" in publication.public_fields else [],
        "published_at": publication.created_at,
        "ai_enabled": publication.ai_enabled,
        "raw_download_enabled": publication.raw_download_enabled,
        "public_fields": list(publication.public_fields),
        "revision_id": publication.revision_id,
    }
    for name in ("body", "note", "url"):
        if name in publication.public_fields:
            data[name] = getattr(publication, f"public_{name}")
    return data


class PublicService:
    def __init__(self, uows: UnitOfWorkFactory, store: LocalObjectStore | None = None) -> None:
        self.uows, self.store = uows, store

    async def notes(self, *, tag: str | None, limit: int, cursor: str | None) -> dict[str, Any]:
        return await self.list(kind=ResourceKind.ARTICLE, tag=tag, limit=limit, cursor=cursor)

    async def bookmarks(self, *, tag: str | None, limit: int, cursor: str | None) -> dict[str, Any]:
        return await self.list(kind=ResourceKind.BOOKMARK, tag=tag, limit=limit, cursor=cursor)

    async def _current(self, uow: UnitOfWork, publication_id: UUID) -> tuple[Resource, Publication]:
        probe = await uow.repositories.publications.get(publication_id)
        if probe is None:
            raise NotFoundError("公开内容不存在")
        resource = await uow.repositories.resources.get_for_update(probe.resource_id)
        current = await uow.repositories.publications.current_for_resource(probe.resource_id)
        now = await uow.repositories.users.database_time()
        if (
            resource is None
            or resource.current_revision_id is None
            or current is None
            or current.id != publication_id
        ):
            raise NotFoundError("公开内容不存在")
        require_allowed(
            ActorContext(
                user_id=None,
                role=ActorRole.ANONYMOUS,
                auth_session_id=None,
                step_up_expires_at=None,
                capabilities=frozenset(),
                scope_epoch=0,
            ),
            PolicyFacts(
                operation=Operation.READ_PUBLIC_RESOURCE,
                now=now,
                current_scope_epoch=await uow.repositories.settings.get_acl_epoch(),
                resource=ResourceFacts(
                    resource_id=resource.id,
                    owner_id=resource.owner_id,
                    current_revision_id=resource.current_revision_id,
                    acl_version=resource.acl_version,
                    is_deleted=resource.deleted_at is not None,
                    is_archived=resource.archived_at is not None,
                    expires_at=resource.expires_at,
                    publication=PublicationFacts(
                        publication_id=current.id,
                        resource_id=resource.id,
                        revision_id=current.revision_id,
                        is_current=True,
                        revoked_at=current.revoked_at,
                        ai_enabled=current.ai_enabled,
                        raw_download_enabled=current.raw_download_enabled,
                    ),
                ),
            ),
        )
        return resource, current

    async def by_publication(self, publication_id: UUID) -> dict[str, Any]:
        async with self.uows() as uow:
            resource, publication = await self._current(uow, publication_id)
            return public_dto(resource, publication)

    async def note(self, slug: str) -> dict[str, Any]:
        async with self.uows() as uow:
            probe = await uow.repositories.publications.public_by_slug(slug)
            if probe is None:
                raise NotFoundError("公开文章不存在")
            resource, publication = await self._current(uow, probe.id)
            if resource.kind is not ResourceKind.ARTICLE:
                raise NotFoundError("公开文章不存在")
            return public_dto(resource, publication)

    async def list(
        self, *, kind: ResourceKind, tag: str | None, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        async with self.uows() as uow:
            page = await uow.repositories.publications.public_page(
                kind=kind, tag=tag, limit=limit, cursor=cursor
            )
            allowed = {}
            for candidate in sorted(page.items, key=lambda item: item.resource_id.int):
                try:
                    resource, publication = await self._current(uow, candidate.id)
                    allowed[candidate.id] = public_dto(resource, publication)
                except NotFoundError:
                    continue
            return {
                "items": [allowed[item.id] for item in page.items if item.id in allowed],
                "next_cursor": page.next_cursor,
            }

    async def _file_snapshot(
        self, uow: UnitOfWork, publication_id: UUID
    ) -> tuple[UUID, int, str, str, int, str]:
        resource, publication = await self._current(uow, publication_id)
        if not publication.raw_download_enabled:
            raise NotFoundError("公开文件不存在")
        revision = await uow.repositories.resources.revision(resource.id, publication.revision_id)
        if revision is None or revision.file_object_key is None:
            raise NotFoundError("公开文件不存在")
        file = await uow.repositories.files.by_key(revision.file_object_key)
        if (
            file is None
            or file.resource_id != resource.id
            or file.status is not FileObjectStatus.READY
            or file.deleted_at is not None
            or file.sha256 != revision.file_sha256
            or file.byte_size != revision.byte_size
            or file.media_type != revision.media_type
        ):
            raise NotFoundError("公开文件不存在")
        return (
            file.id,
            resource.acl_version,
            file.object_key,
            file.sha256,
            file.byte_size,
            file.media_type,
        )

    async def file(self, publication_id: UUID) -> tuple[bytes, str]:
        async with self.uows() as uow:
            snapshot = await self._file_snapshot(uow, publication_id)
        if self.store is None:
            raise ConflictError("公开存储未配置")
        try:
            content = await self.store.read(snapshot[2])
        except FileNotFoundError as error:
            raise NotFoundError("公开文件不存在") from error
        if len(content) != snapshot[4] or hashlib.sha256(content).hexdigest() != snapshot[3]:
            raise ConflictError("文件校验失败")
        async with self.uows() as uow:
            if await self._file_snapshot(uow, publication_id) != snapshot:
                raise NotFoundError("公开文件不存在")
        return content, snapshot[5]

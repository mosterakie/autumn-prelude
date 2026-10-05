"""私人原稿修订与资源可见性生命周期；每次修订追加新版本。"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select

from autumn_backend.db.enums import ContentFormat, ResourceKind
from autumn_backend.db.models import Resource, ResourceVersion
from autumn_backend.errors import ConflictError, OptimisticLockError
from autumn_backend.repositories.base import VersionedRepository
from autumn_backend.repositories.constraints import database_errors
from autumn_backend.repositories.pagination import Page, fetch_page
from autumn_backend.repositories.publications import PublicationRepository


@dataclass(frozen=True, slots=True)
class RevisionDraft:
    title: str | None = None
    body_text: str | None = None
    url: str | None = None
    private_note: str | None = None
    tags: tuple[str, ...] = ()
    content_format: ContentFormat = ContentFormat.MARKDOWN
    linked_source_id: UUID | None = None
    file_object_key: str | None = None
    file_sha256: str | None = None
    media_type: str | None = None
    byte_size: int | None = None


class ResourceRepository(VersionedRepository[Resource]):
    model = Resource
    mutable_fields = frozenset({"current_revision_id", "slug", "retention_policy_id", "expires_at"})

    async def for_owner(
        self, owner_id: UUID, *, limit: int = 20, cursor: str | None = None
    ) -> Page[Resource]:
        return await fetch_page(
            self.session,
            Resource,
            select(Resource).where(Resource.owner_id == owner_id, Resource.deleted_at.is_(None)),
            limit=limit,
            cursor=cursor,
        )

    async def create(
        self, *, owner_id: UUID, kind: ResourceKind, slug: str, draft: RevisionDraft
    ) -> Resource:
        resource = Resource(owner_id=owner_id, kind=kind, slug=slug)
        with database_errors():
            self.session.add(resource)
            await self.session.flush()
            revision = await self._append(resource, draft, owner_id)
            resource.current_revision_id = revision.id
            await self.session.flush()
        return resource

    async def _append(
        self, resource: Resource, draft: RevisionDraft, created_by: UUID
    ) -> ResourceVersion:
        number = (
            await self.session.execute(
                select(func.coalesce(func.max(ResourceVersion.revision_no), 0) + 1).where(
                    ResourceVersion.resource_id == resource.id
                )
            )
        ).scalar_one()
        revision = ResourceVersion(
            resource_id=resource.id,
            revision_no=number,
            created_by=created_by,
            title=draft.title,
            body_text=draft.body_text,
            url=draft.url,
            private_note=draft.private_note,
            tags=list(draft.tags),
            content_format=draft.content_format,
            linked_source_id=draft.linked_source_id,
            file_object_key=draft.file_object_key,
            file_sha256=draft.file_sha256,
            media_type=draft.media_type,
            byte_size=draft.byte_size,
        )
        self.session.add(revision)
        await self.session.flush()
        return revision

    async def revise(
        self, resource_id: UUID, *, expected_version: int, draft: RevisionDraft, created_by: UUID
    ) -> Resource:
        resource = await self.get_for_update_or_raise(resource_id)
        if resource.version != expected_version:
            raise OptimisticLockError("原稿已变化")
        if resource.deleted_at is not None or resource.owner_id != created_by:
            raise ConflictError("资源已删除或归属不匹配")
        with database_errors():
            revision = await self._append(resource, draft, created_by)
        return await self._update_versioned(
            resource_id, expected_version, {"current_revision_id": revision.id}
        )

    async def _lifecycle(
        self,
        resource_id: UUID,
        *,
        expected_version: int,
        expected_acl_version: int,
        deleted: bool | None = None,
        archived: bool | None = None,
    ) -> Resource:
        resource = await self.get_for_update_or_raise(resource_id)
        if resource.version != expected_version or resource.acl_version != expected_acl_version:
            raise OptimisticLockError("内容或公开范围已变化")
        if archived is not None and resource.deleted_at is not None:
            raise ConflictError("请先恢复已删除的资源")
        attribute = "deleted_at" if deleted is not None else "archived_at"
        enabled = deleted if deleted is not None else archived
        if (getattr(resource, attribute) is not None) == enabled:
            return resource
        publications = PublicationRepository(self.session)
        # 恢复或取消归档只恢复私人内容，所有旧 publication 保持撤回。
        await publications._revoke_current(resource_id)
        if deleted is not None:
            resource.deleted_at = (
                (await self.session.execute(select(func.clock_timestamp()))).scalar_one()
                if deleted
                else None
            )
        else:
            resource.archived_at = (
                (await self.session.execute(select(func.clock_timestamp()))).scalar_one()
                if archived
                else None
            )
            resource.version += 1
        with database_errors():
            await self.session.flush()
        await publications._visibility_changed(resource_id)
        await self.session.refresh(resource)
        return resource

    async def soft_delete(
        self, resource_id: UUID, *, expected_version: int, expected_acl_version: int
    ) -> Resource:
        return await self._lifecycle(
            resource_id,
            expected_version=expected_version,
            expected_acl_version=expected_acl_version,
            deleted=True,
        )

    async def restore(
        self, resource_id: UUID, *, expected_version: int, expected_acl_version: int
    ) -> Resource:
        return await self._lifecycle(
            resource_id,
            expected_version=expected_version,
            expected_acl_version=expected_acl_version,
            deleted=False,
        )

    async def set_archived(
        self, resource_id: UUID, *, expected_version: int, expected_acl_version: int, archived: bool
    ) -> Resource:
        return await self._lifecycle(
            resource_id,
            expected_version=expected_version,
            expected_acl_version=expected_acl_version,
            archived=archived,
        )

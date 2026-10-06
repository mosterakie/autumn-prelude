"""发布/撤回在资源锁内串行化，只改变可见性版本。"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, or_, select, update

from autumn_backend.db.enums import ResourceKind
from autumn_backend.db.models import Publication, Resource, ResourceVersion
from autumn_backend.db.models.content import PUBLIC_FIELD_NAMES
from autumn_backend.errors import ConflictError, InvalidInputError, OptimisticLockError
from autumn_backend.repositories.base import ControlledMutableRepository, UUIDRepository
from autumn_backend.repositories.constraints import database_errors
from autumn_backend.repositories.identity import SettingRepository
from autumn_backend.repositories.pagination import Page, fetch_page


class ResourceLockRepository(UUIDRepository[Resource]):
    model = Resource


@dataclass(frozen=True, slots=True)
class PublicationProjection:
    fields: frozenset[str]
    ai_enabled: bool = False
    raw_download_enabled: bool = False


class PublicationRepository(ControlledMutableRepository[Publication]):
    model = Publication

    async def public_page(
        self, *, kind: ResourceKind, tag: str | None, limit: int, cursor: str | None
    ) -> Page[Publication]:
        query = (
            select(Publication)
            .join(Resource, Resource.id == Publication.resource_id)
            .where(
                Resource.kind == kind,
                Resource.deleted_at.is_(None),
                Resource.archived_at.is_(None),
                or_(Resource.expires_at.is_(None), Resource.expires_at > func.clock_timestamp()),
                Publication.revoked_at.is_(None),
            )
        )
        if tag is not None:
            query = query.where(Publication.public_tags.contains([tag]))
        return await fetch_page(self.session, Publication, query, limit=limit, cursor=cursor)

    async def current_for_resource(self, resource_id: UUID) -> Publication | None:
        """调用方先锁 Resource；此方法只返回未撤回投影，公开读取仍走可见性过滤。"""
        return (
            await self.session.execute(
                select(Publication)
                .where(Publication.resource_id == resource_id, Publication.revoked_at.is_(None))
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    async def _resource(
        self, resource_id: UUID, expected_version: int | None, expected_acl_version: int
    ) -> Resource:
        resource = await ResourceLockRepository(self.session).get_for_update_or_raise(resource_id)
        if (
            expected_version is not None and resource.version != expected_version
        ) or resource.acl_version != expected_acl_version:
            raise OptimisticLockError("内容或公开范围已变化，请重新预览")
        return resource

    async def publish_under_resource_lock(
        self,
        *,
        resource_id: UUID,
        revision_id: UUID,
        expected_version: int,
        expected_acl_version: int,
        published_by: UUID,
        projection: PublicationProjection,
    ) -> Publication:
        resource = await self._resource(resource_id, expected_version, expected_acl_version)
        if not resource.is_active or resource.owner_id != published_by:
            raise ConflictError("资源不可发布或发布者归属不匹配")
        revision = await self.session.get(ResourceVersion, revision_id)
        if revision is None or revision.resource_id != resource_id:
            raise ConflictError("版本不属于当前资源")
        if not projection.fields or not projection.fields <= set(PUBLIC_FIELD_NAMES):
            raise InvalidInputError("公开字段必须来自白名单")
        source = dict(
            title=revision.title,
            body=revision.body_text,
            note=revision.private_note,
            url=revision.url,
            tags=revision.tags,
        )
        values = {f"public_{field}": source[field] for field in projection.fields}
        if any(
            source[field] is None or (field == "tags" and not source[field])
            for field in projection.fields
        ):
            raise InvalidInputError("选择的公开字段在当前版本中没有值")
        if projection.raw_download_enabled and revision.file_object_key is None:
            raise InvalidInputError("当前版本没有可供下载的原件")
        await self._revoke_current(resource_id)
        number = (
            await self.session.execute(
                select(func.coalesce(func.max(Publication.publication_no), 0) + 1).where(
                    Publication.resource_id == resource_id
                )
            )
        ).scalar_one()
        publication = Publication(
            resource_id=resource_id,
            revision_id=revision_id,
            publication_no=number,
            published_by=published_by,
            public_fields=sorted(projection.fields),
            ai_enabled=projection.ai_enabled,
            raw_download_enabled=projection.raw_download_enabled,
            **values,
        )
        with database_errors():
            self.session.add(publication)
            await self.session.flush()
        await self._visibility_changed(resource_id)
        return publication

    async def _revoke_current(self, resource_id: UUID) -> bool:
        result = await self.session.execute(
            update(Publication)
            .where(Publication.resource_id == resource_id, Publication.revoked_at.is_(None))
            .values(revoked_at=func.clock_timestamp(), version=Publication.version + 1)
            .returning(Publication.id)
            .execution_options(synchronize_session=False)
        )
        return result.scalar_one_or_none() is not None

    async def _visibility_changed(self, resource_id: UUID) -> None:
        await self.session.execute(
            update(Resource)
            .where(Resource.id == resource_id)
            .values(acl_version=Resource.acl_version + 1)
            .execution_options(synchronize_session=False)
        )
        await SettingRepository(self.session).bump_acl_epoch()

    async def revoke(
        self, resource_id: UUID, *, expected_acl_version: int, expected_version: int | None = None
    ) -> bool:
        await self._resource(resource_id, expected_version, expected_acl_version)
        changed = await self._revoke_current(resource_id)
        if changed:
            await self._visibility_changed(resource_id)
        return changed

    async def public_by_slug(self, slug: str) -> Publication | None:
        return (
            await self.session.execute(
                select(Publication)
                .join(Resource, Resource.id == Publication.resource_id)
                .where(
                    Resource.slug == slug,
                    Resource.deleted_at.is_(None),
                    Resource.archived_at.is_(None),
                    or_(
                        Resource.expires_at.is_(None), Resource.expires_at > func.clock_timestamp()
                    ),
                    Publication.revoked_at.is_(None),
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

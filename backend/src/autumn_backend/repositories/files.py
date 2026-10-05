from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from autumn_backend.db.enums import FileObjectStatus as Status
from autumn_backend.db.models import FileObject
from autumn_backend.errors import ConflictError, IdempotencyConflictError
from autumn_backend.repositories.base import ControlledMutableRepository
from autumn_backend.repositories.constraints import database_errors
from autumn_backend.repositories.result import Creation


class FileRepository(ControlledMutableRepository[FileObject]):
    model = FileObject

    async def by_key(self, key: str) -> FileObject | None:
        return (
            await self.session.execute(select(FileObject).where(FileObject.object_key == key))
        ).scalar_one_or_none()

    async def stage(
        self, *, object_id: UUID, owner_id: UUID, key: str, digest: str, media_type: str, size: int
    ) -> Creation[FileObject]:
        with database_errors():
            record = (
                await self.session.execute(
                    insert(FileObject)
                    .values(
                        id=object_id,
                        owner_id=owner_id,
                        object_key=key,
                        sha256=digest,
                        media_type=media_type,
                        byte_size=size,
                        status=Status.STAGED,
                    )
                    .on_conflict_do_nothing(index_elements=[FileObject.id])
                    .returning(FileObject)
                )
            ).scalar_one_or_none()
        if record is not None:
            return Creation(record, True)
        record = await self.get_for_update_or_raise(object_id)
        if (
            record.owner_id,
            record.object_key,
            record.sha256,
            record.media_type,
            record.byte_size,
        ) != (owner_id, key, digest, media_type, size):
            raise IdempotencyConflictError("上传标识已用于不同文件")
        if record.deleted_at is not None or record.status is Status.PENDING_DELETE:
            raise ConflictError("文件已进入删除流程")
        return Creation(record, False)

    async def ready(self, object_id: UUID) -> FileObject:
        record = await self.get_for_update_or_raise(object_id)
        if record.status is Status.READY:
            return record
        return await self._transition(
            object_id, Status.STAGED, Status.READY, {"finalized_at": func.clock_timestamp()}
        )

    async def pending_delete(self, object_id: UUID) -> FileObject:
        record = await self.get_for_update_or_raise(object_id)
        if record.status is Status.PENDING_DELETE or record.deleted_at is not None:
            return record
        return await self._transition(
            object_id, record.status, Status.PENDING_DELETE, {"finalized_at": None}
        )

    async def deleted(self, object_id: UUID) -> FileObject:
        record = await self.get_for_update_or_raise(object_id)
        if record.deleted_at is not None:
            return record
        return await self._transition(
            object_id,
            Status.PENDING_DELETE,
            Status.PENDING_DELETE,
            {"deleted_at": func.clock_timestamp()},
        )

    async def attach(self, object_id: UUID, resource_id: UUID) -> None:
        record = await self.get_for_update_or_raise(object_id)
        if (
            record.resource_id not in (None, resource_id)
            or record.status is not Status.READY
            or record.deleted_at is not None
        ):
            raise ConflictError("文件不可绑定此资源")
        await self.session.execute(
            update(FileObject).where(FileObject.id == object_id).values(resource_id=resource_id)
        )

    transition_fields = frozenset({"finalized_at", "deleted_at"})

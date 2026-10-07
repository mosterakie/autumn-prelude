"""E5 多个短 UoW 编排文件 I/O；对象 key 从登记到转正保持不变。"""

import hashlib
import io
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePath
from uuid import UUID, uuid5

from autumn_backend.db.enums import FileObjectStatus
from autumn_backend.db.models import FileObject
from autumn_backend.db.session import UnitOfWork, UnitOfWorkFactory
from autumn_backend.errors import (
    ConflictError,
    IdempotencyConflictError,
    InvalidInputError,
    NotFoundError,
)
from autumn_backend.jobs.queue import enqueue
from autumn_backend.policies import ActorContext
from autumn_backend.policies.facts import Operation, PolicyFacts
from autumn_backend.repositories.jobs import JobSpec
from autumn_backend.services.access import lock_authentication, publication_facts, require_allowed
from autumn_backend.storage.local import LocalObjectStore

_NAMESPACE = UUID("c7e38961-4d38-5d37-871f-06e1bc5b92f5")
MAX_FILE_BYTES = 20 * 1024 * 1024
PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def inspect_upload(filename: str, data: bytes, declared_type: str) -> str:
    if not data or len(data) > MAX_FILE_BYTES:
        raise InvalidInputError("文件必须非空且不超过 20 MiB")
    suffix = PurePath(filename).suffix.lower()
    if suffix == ".pdf" and data.startswith(b"%PDF-"):
        media_type = PDF
    elif suffix == ".docx":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = set(archive.namelist())
                if (
                    not {"[Content_Types].xml", "word/document.xml"} <= names
                    or len(names) > 2000
                    or sum(item.file_size for item in archive.infolist()) > 50 * 1024 * 1024
                ):
                    raise InvalidInputError("DOCX 结构或展开大小无效")
        except zipfile.BadZipFile as error:
            raise InvalidInputError("DOCX 文件无效") from error
        media_type = DOCX
    else:
        raise InvalidInputError("首版只支持 PDF、DOCX；不支持旧版 DOC")
    if declared_type not in (media_type, "application/octet-stream"):
        raise InvalidInputError("文件类型声明与内容不一致")
    return media_type


@dataclass(frozen=True, slots=True)
class FileDTO:
    id: UUID
    object_key: str
    sha256: str
    media_type: str
    byte_size: int
    status: FileObjectStatus


def file_dto(record: FileObject) -> FileDTO:
    return FileDTO(
        record.id,
        record.object_key,
        record.sha256,
        record.media_type,
        record.byte_size,
        record.status,
    )


class StorageService:
    def __init__(self, uows: UnitOfWorkFactory, store: LocalObjectStore) -> None:
        self._uows, self.store = uows, store

    async def _authorize(self, uow: UnitOfWork, actor: ActorContext) -> None:
        auth = await lock_authentication(uow, actor)
        require_allowed(
            actor,
            PolicyFacts(
                operation=Operation.INGEST_RESOURCE,
                authentication=auth,
                now=await uow.repositories.users.database_time(),
                current_scope_epoch=await uow.repositories.settings.get_acl_epoch(),
            ),
        )

    async def upload(
        self,
        actor: ActorContext,
        *,
        idempotency_key: str,
        filename: str,
        data: bytes,
        media_type: str,
        queue_finalize: bool = True,
    ) -> FileDTO:
        if not idempotency_key.strip() or len(idempotency_key) > 128:
            raise InvalidInputError("上传幂等标识无效")
        actual_type = inspect_upload(filename, data, media_type)
        digest = hashlib.sha256(data).hexdigest()
        object_id = uuid5(_NAMESPACE, f"{actor.user_id}:{idempotency_key}")
        async with self._uows() as uow:
            await self._authorize(uow, actor)
            existing = await uow.repositories.files.get_for_update(object_id)
            if existing is not None:
                if (existing.sha256, existing.media_type, existing.byte_size) != (
                    digest,
                    actual_type,
                    len(data),
                ):
                    raise IdempotencyConflictError("上传标识已用于不同文件")
                if (
                    existing.deleted_at is not None
                    or existing.status is FileObjectStatus.PENDING_DELETE
                ):
                    raise ConflictError("文件已进入删除流程")
                if existing.status is FileObjectStatus.READY:
                    return file_dto(existing)
        assert actor.user_id is not None
        key = f"objects/{object_id.hex}.{'pdf' if actual_type == PDF else 'docx'}"
        # 与目标文件区分；验证/数据库故障留下的 staging 由孤儿回收收敛。
        await self.store.stage(key, data)
        async with self._uows() as uow:
            await self._authorize(uow, actor)
            creation = await uow.repositories.files.stage(
                object_id=object_id,
                owner_id=actor.user_id,
                key=key,
                digest=digest,
                media_type=actual_type,
                size=len(data),
            )
            if queue_finalize and creation.record.status is FileObjectStatus.STAGED:
                await enqueue(
                    uow,
                    JobSpec(
                        kind="storage.finalize",
                        idempotency_key=f"storage.finalize:{object_id}",
                        payload={"file_id": str(object_id)},
                        actor_id=actor.user_id,
                        auth_session_id=actor.auth_session_id,
                    ),
                )
            return file_dto(creation.record)

    async def _job_file(
        self, uow: UnitOfWork, actor: ActorContext, job_id: UUID, token: UUID, kind: str
    ) -> FileObject:
        await self._authorize(uow, actor)
        job = await uow.repositories.jobs.require_lease(job_id, token)
        if (
            job.actor_id != actor.user_id
            or job.auth_session_id != actor.auth_session_id
            or job.kind != kind
        ):
            raise NotFoundError("文件任务不存在")
        value = job.payload.get("file_id") if job.payload is not None else None
        if not isinstance(value, str):
            raise ConflictError("文件任务载荷无效")
        try:
            file_id = UUID(value)
        except ValueError as error:
            raise ConflictError("文件任务载荷无效") from error
        record = await uow.repositories.files.get_for_update_or_raise(file_id)
        if record.owner_id != actor.user_id:
            raise NotFoundError("文件不存在")
        return record

    async def finalize(self, actor: ActorContext, job_id: UUID, token: UUID) -> FileDTO:
        async with self._uows() as uow:
            record = await self._job_file(uow, actor, job_id, token, "storage.finalize")
            if (
                record.status not in (FileObjectStatus.STAGED, FileObjectStatus.READY)
                or record.deleted_at is not None
            ):
                raise ConflictError("文件不能转正")
            snapshot = file_dto(record)
        await self.store.promote(snapshot.object_key, snapshot.sha256, snapshot.byte_size)
        async with self._uows() as uow:
            record = await self._job_file(uow, actor, job_id, token, "storage.finalize")
            record = await uow.repositories.files.ready(record.id)
            await self._authorize(uow, actor)
            await uow.repositories.jobs.finish(job_id, token, result={"file_id": str(record.id)})
            return file_dto(record)

    async def delete(
        self,
        actor: ActorContext,
        file_id: UUID,
        *,
        resource_version: int | None = None,
        acl_version: int | None = None,
    ) -> FileDTO:
        async with self._uows() as uow:
            await self._authorize(uow, actor)
            probe = await uow.repositories.files.get_or_raise(file_id)
            if probe.owner_id != actor.user_id:
                raise NotFoundError("文件不存在")
            if probe.resource_id is not None:
                if resource_version is None or acl_version is None:
                    raise InvalidInputError("关联资源删除需要双版本")
                await uow.repositories.resources.soft_delete(
                    probe.resource_id,
                    expected_version=resource_version,
                    expected_acl_version=acl_version,
                )
            record = await uow.repositories.files.pending_delete(file_id)
            if record.deleted_at is None:
                await enqueue(
                    uow,
                    JobSpec(
                        kind="storage.delete",
                        idempotency_key=f"storage.delete:{file_id}",
                        payload={"file_id": str(file_id)},
                        actor_id=actor.user_id,
                        auth_session_id=actor.auth_session_id,
                    ),
                )
            return file_dto(record)

    async def run_delete(self, actor: ActorContext, job_id: UUID, token: UUID) -> FileDTO:
        async with self._uows() as uow:
            record = await self._job_file(uow, actor, job_id, token, "storage.delete")
            if record.status is not FileObjectStatus.PENDING_DELETE:
                raise ConflictError("文件未进入删除流程")
            snapshot = file_dto(record)
        await self.store.delete(snapshot.object_key)
        async with self._uows() as uow:
            record = await self._job_file(uow, actor, job_id, token, "storage.delete")
            record = await uow.repositories.files.deleted(record.id)
            await self._authorize(uow, actor)
            await uow.repositories.jobs.finish(job_id, token, result={"file_id": str(record.id)})
            return file_dto(record)

    async def read_private(self, actor: ActorContext, file_id: UUID) -> bytes:
        async with self._uows() as uow:
            await self._authorize(uow, actor)
            await self._check_resource(uow, actor, file_id)
            record = await uow.repositories.files.get_for_update_or_raise(file_id)
            if (
                record.owner_id != actor.user_id
                or record.status is not FileObjectStatus.READY
                or record.deleted_at is not None
            ):
                raise NotFoundError("文件不存在")
            snapshot = file_dto(record)
        data = await self.store.read(snapshot.object_key)
        async with self._uows() as uow:
            await self._authorize(uow, actor)
            await self._check_resource(uow, actor, file_id)
            record = await uow.repositories.files.get_for_update_or_raise(file_id)
            if record.status is not FileObjectStatus.READY or record.deleted_at is not None:
                raise NotFoundError("文件不存在")
        if len(data) != snapshot.byte_size or hashlib.sha256(data).hexdigest() != snapshot.sha256:
            raise ConflictError("文件校验失败")
        return data

    async def _check_resource(self, uow: UnitOfWork, actor: ActorContext, file_id: UUID) -> None:
        file = await uow.repositories.files.get_or_raise(file_id)
        if file.owner_id != actor.user_id:
            raise NotFoundError("文件不存在")
        if file.resource_id is not None:
            resource = await uow.repositories.resources.get_for_update_or_raise(file.resource_id)
            auth = await lock_authentication(uow, actor)
            require_allowed(
                actor, await publication_facts(uow, Operation.READ_PRIVATE_RESOURCE, auth, resource)
            )

    async def cleanup_orphans(self, *, now: datetime) -> tuple[str, ...]:
        if now.tzinfo is None:
            raise InvalidInputError("回收时钟需要时区")
        before = (now.astimezone(UTC) - timedelta(days=1)).timestamp()
        candidates = await self.store.orphan_candidates(before)
        removed = []
        for key in candidates:
            async with self._uows() as uow:
                registered = await uow.repositories.files.by_key(key)
            if registered is None:
                await self.store.remove_orphan(key, before)
                removed.append(key)
        return tuple(removed)

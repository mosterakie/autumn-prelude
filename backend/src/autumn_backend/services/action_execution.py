"""人工确认动作的固定数据库写入器；仅由 E8 联合提交闸门调用。"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from autumn_backend.db.enums import MemoryKind
from autumn_backend.db.models import Action, FileObject, Setting
from autumn_backend.db.session import UnitOfWork, UnitOfWorkFactory
from autumn_backend.errors import OptimisticLockError
from autumn_backend.jobs.queue import LeasedJob, enqueue
from autumn_backend.repositories.jobs import JobSpec
from autumn_backend.repositories.publications import PublicationProjection
from autumn_backend.services.actions import (
    Command,
    MemoryPreview,
    ResourceCreatePreview,
    ResourcePreview,
)
from autumn_backend.services.execution import ExecutionService
from autumn_backend.services.resources import ResourceService
from autumn_backend.services.tasks import TaskService


class ActionExecutionService:
    def __init__(self, uows: UnitOfWorkFactory) -> None:
        self.uows = uows

    async def execute(self, job: LeasedJob) -> None:
        actor = await TaskService(self.uows).actor(job)

        async def write(uow: UnitOfWork, action: Action, command: Command) -> dict[str, Any]:
            if isinstance(command, ResourceCreatePreview):
                resource = await ResourceService(self.uows).create_in_uow(
                    uow, actor, command.resource
                )
                return {
                    "resource_id": str(resource.id),
                    "resource_version": resource.version,
                    "acl_version": resource.acl_version,
                    "changed": True,
                }
            return await self._write(uow, action, command)

        await ExecutionService(self.uows).commit_action_result(
            actor,
            job.id,
            job.token,
            write,
        )

    @staticmethod
    async def _write(uow: UnitOfWork, action: Action, command: Command) -> dict[str, Any]:
        if isinstance(command, ResourceCreatePreview):
            raise ValueError("资源创建须使用当前执行身份")
        if isinstance(command, ResourcePreview):
            publication_id = None
            changed = True
            if command.kind == "publish":
                assert command.revision_id is not None
                publication = await uow.repositories.publications.publish_under_resource_lock(
                    resource_id=command.target_id,
                    revision_id=command.revision_id,
                    expected_version=command.expected_version,
                    expected_acl_version=command.expected_acl_version,
                    published_by=action.actor_id,
                    projection=PublicationProjection(
                        frozenset(command.public_fields),
                        command.ai_enabled,
                        command.raw_download_enabled,
                    ),
                )
                publication_id = publication.id
            elif command.kind == "revoke":
                changed = await uow.repositories.publications.revoke(
                    command.target_id,
                    expected_version=command.expected_version,
                    expected_acl_version=command.expected_acl_version,
                )
            else:
                await uow.repositories.resources.soft_delete(
                    command.target_id,
                    expected_version=command.expected_version,
                    expected_acl_version=command.expected_acl_version,
                )
                files = (
                    await uow.session.scalars(
                        select(FileObject)
                        .where(
                            FileObject.resource_id == command.target_id,
                            FileObject.deleted_at.is_(None),
                        )
                        .order_by(FileObject.id)
                        .with_for_update()
                    )
                ).all()
                for file in files:
                    await uow.repositories.files.pending_delete(file.id)
                    await enqueue(
                        uow,
                        JobSpec(
                            kind="storage.delete",
                            idempotency_key=f"storage.delete:{file.id}",
                            actor_id=action.actor_id,
                            auth_session_id=action.auth_session_id,
                            payload={"file_id": str(file.id)},
                        ),
                    )
            resource = await uow.repositories.resources.get_for_update_or_raise(command.target_id)
            if changed:
                await enqueue(
                    uow,
                    JobSpec(
                        kind="knowledge.cleanup"
                        if command.kind == "delete"
                        else "knowledge.publication_sync",
                        idempotency_key=f"action.index:{action.id}",
                        actor_id=action.actor_id,
                        auth_session_id=action.auth_session_id,
                        resource_id=resource.id,
                        payload={
                            "publication_id": str(publication_id) if publication_id else None,
                            "acl_version": resource.acl_version,
                        },
                    ),
                )
            result: dict[str, Any] = {
                "resource_id": str(resource.id),
                "resource_version": resource.version,
                "acl_version": resource.acl_version,
                "changed": changed,
                "scope_epoch": await uow.repositories.settings.get_acl_epoch(),
            }
            if publication_id:
                result["publication_id"] = str(publication_id)
            return result
        if isinstance(command, MemoryPreview):
            if command.kind == "create_memory":
                assert command.content_text is not None
                memory = await uow.repositories.memories.create(
                    user_id=action.actor_id,
                    kind=MemoryKind(command.memory_kind),
                    content_text=command.content_text,
                    origin_run_id=command.origin_run_id,
                )
            else:
                assert command.target_id is not None and command.expected_version is not None
                if command.kind == "update_memory":
                    assert command.content_text is not None
                    memory = await uow.repositories.memories.revise(
                        command.target_id,
                        expected_version=command.expected_version,
                        content_text=command.content_text,
                        kind=MemoryKind(command.memory_kind),
                    )
                else:
                    memory = await uow.repositories.memories.soft_delete(
                        command.target_id, expected_version=command.expected_version
                    )
            memory.confirmed_at = action.confirmed_at
            await uow.session.flush()
            return {
                "memory_id": str(memory.id),
                "changed": True,
                "scope_epoch": await uow.repositories.settings.bump_acl_epoch(),
            }
        # _target 已验证 ai_limits 白名单结构并加行锁。缺失行也使用 ON CONFLICT 防护。
        setting = await uow.repositories.settings.get("ai_limits")
        if setting is None:
            created = (
                await uow.session.execute(
                    insert(Setting)
                    .values(
                        key="ai_limits",
                        schema_version=1,
                        value=command.value,
                        version=1,
                        updated_by=action.actor_id,
                    )
                    .on_conflict_do_nothing(index_elements=[Setting.key])
                    .returning(Setting)
                )
            ).scalar_one_or_none()
            if created is None:
                raise OptimisticLockError("设置已变化")
        else:
            if setting.version != command.expected_version:
                raise OptimisticLockError("设置已变化")
            setting.value, setting.updated_by = command.value, action.actor_id
            setting.version += 1
            await uow.session.flush()
        return {"settings_key": "ai_limits", "changed": True}

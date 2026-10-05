"""服务端验证动作语义，模型只能提议，持久确认入口由 HTTP 用户操作调用。"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator
from sqlalchemy import select

from autumn_backend.db.enums import (
    ActionStatus,
    ActionType,
    AuditResult,
    MessageRole,
    RunEventType,
    RunStatus,
)
from autumn_backend.db.models import Action, Message, ResourceVersion, Setting
from autumn_backend.db.models.content import PUBLIC_FIELD_NAMES
from autumn_backend.db.session import UnitOfWork, UnitOfWorkFactory
from autumn_backend.errors import (
    ConcurrencyLimitError,
    ConflictError,
    InvalidInputError,
    NotFoundError,
    OptimisticLockError,
)
from autumn_backend.jobs.queue import enqueue
from autumn_backend.policies import ActorContext
from autumn_backend.policies.facts import Operation, PolicyFacts, TargetFacts, TargetKind
from autumn_backend.repositories.audit import AuditMetadata
from autumn_backend.repositories.jobs import JobSpec
from autumn_backend.services.access import lock_authentication, publication_facts, require_allowed
from autumn_backend.services.ai_limits import read_ai_limits
from autumn_backend.services.context import advance_context_generation, run_facts
from autumn_backend.services.quota import QuotaService


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ResourcePreview(Payload):
    target_type: Literal["resource"] = "resource"
    kind: Literal["publish", "revoke", "delete"]
    target_id: UUID
    expected_version: int = Field(ge=0)
    expected_acl_version: int = Field(ge=0)
    revision_id: UUID | None = None
    public_fields: tuple[str, ...] = ()
    ai_enabled: bool = False
    raw_download_enabled: bool = False

    @model_validator(mode="after")
    def valid_kind(self) -> "ResourcePreview":
        if self.kind == "publish":
            if (
                self.revision_id is None
                or not self.public_fields
                or not set(self.public_fields) <= set(PUBLIC_FIELD_NAMES)
            ):
                raise ValueError("发布须指定原稿与白名单公开字段")
            if self.public_fields != tuple(sorted(set(self.public_fields))):
                raise ValueError("公开字段须排序且不可重复")
        elif (
            self.revision_id is not None
            or self.public_fields
            or self.ai_enabled
            or self.raw_download_enabled
        ):
            raise ValueError("撤回/删除不能夹带发布参数")
        return self


class SettingsPreview(Payload):
    target_type: Literal["settings"] = "settings"
    kind: Literal["update_settings"] = "update_settings"
    target_id: Literal["ai_limits"] = "ai_limits"
    expected_version: int = Field(ge=0)
    value: dict[str, Any]


class MemoryPreview(Payload):
    target_type: Literal["memory"] = "memory"
    kind: Literal["create_memory", "update_memory", "delete_memory"]
    target_id: UUID | None = None
    expected_version: int | None = Field(default=None, ge=0)
    memory_kind: Literal["preference", "fact"] = "fact"
    content_text: str | None = Field(default=None, min_length=1, max_length=8000)
    origin_run_id: UUID | None = None

    @model_validator(mode="after")
    def valid_kind(self) -> "MemoryPreview":
        if self.kind == "create_memory":
            if (
                self.target_id is not None
                or self.expected_version is not None
                or self.content_text is None
                or not self.content_text.strip()
            ):
                raise ValueError("新增记忆参数无效")
        elif self.target_id is None or self.expected_version is None:
            raise ValueError("修改/删除记忆须指定对象与版本")
        if self.kind == "update_memory" and (
            self.content_text is None or not self.content_text.strip()
        ):
            raise ValueError("记忆内容不能为空")
        if self.kind == "delete_memory" and self.content_text is not None:
            raise ValueError("删除记忆不能夹带正文")
        if self.kind != "create_memory" and self.origin_run_id is not None:
            raise ValueError("修改/删除不改变来源")
        return self


Command = ResourcePreview | SettingsPreview | MemoryPreview
_COMMANDS: TypeAdapter[Command] = TypeAdapter(
    Annotated[Command, Field(discriminator="target_type")]
)


@dataclass(frozen=True, slots=True)
class ActionDTO:
    id: UUID
    type: ActionType
    status: ActionStatus
    version: int
    parameters_hash: str
    parameters: dict[str, Any]
    expires_at: datetime
    run_id: UUID | None


def action_dto(action: Action) -> ActionDTO:
    return ActionDTO(
        action.id,
        action.type,
        action.status,
        action.version,
        action.parameters_hash,
        json.loads(json.dumps(action.parameters)),
        action.expires_at,
        action.run_id,
    )


def stored_command(action: Action) -> Command:
    if action.parameters.get("schema_version") != 1:
        raise ConflictError("动作参数版本不支持")
    try:
        command = _COMMANDS.validate_json(json.dumps(action.parameters["command"]))
    except (KeyError, ValidationError) as error:
        raise ConflictError("动作参数损坏") from error
    if (
        ActionType(command.kind) is not action.type
        or (command.target_id if isinstance(command, ResourcePreview) else None)
        != action.target_resource_id
    ):
        raise ConflictError("动作目标与参数不一致")
    expected = command.expected_version
    acl = command.expected_acl_version if isinstance(command, ResourcePreview) else None
    digest = hashlib.sha256(
        json.dumps(
            action.parameters, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()
    if (expected, acl, digest) != (
        action.expected_version,
        action.expected_acl_version,
        action.parameters_hash,
    ):
        raise ConflictError("动作参数摘要或版本不一致")
    return command


class ActionService:
    def __init__(self, uows: UnitOfWorkFactory) -> None:
        self._uows = uows

    async def _target(self, uow: UnitOfWork, actor: ActorContext, command: Command) -> None:
        auth = await lock_authentication(uow, actor)
        if isinstance(command, ResourcePreview):
            resource = await uow.repositories.resources.get_for_update_or_raise(command.target_id)
            require_allowed(
                actor, await publication_facts(uow, Operation.UPDATE_RESOURCE, auth, resource)
            )
            if (resource.version, resource.acl_version) != (
                command.expected_version,
                command.expected_acl_version,
            ):
                raise OptimisticLockError("内容或公开范围已变化，请重新预览")
            if command.kind == "publish":
                revision = await uow.session.get(ResourceVersion, command.revision_id)
                if (
                    revision is None
                    or revision.resource_id != resource.id
                    or not resource.is_active
                ):
                    raise NotFoundError("可发布原稿不存在")
                values = {
                    "title": revision.title,
                    "body": revision.body_text,
                    "note": revision.private_note,
                    "url": revision.url,
                    "tags": revision.tags,
                }
                if any(not values[field] for field in command.public_fields) or (
                    command.raw_download_enabled
                    and (revision.file_object_key is None or resource.kind.value != "document")
                ):
                    raise InvalidInputError("所选公开字段或原文件不可用")
            return
        facts = PolicyFacts(
            operation=Operation.MANAGE_SETTINGS
            if isinstance(command, SettingsPreview)
            else Operation.CREATE_MEMORY,
            authentication=auth,
            now=await uow.repositories.users.database_time(),
            current_scope_epoch=await uow.repositories.settings.get_acl_epoch(),
        )
        require_allowed(actor, facts)
        if isinstance(command, SettingsPreview):
            try:
                read_ai_limits(
                    Setting(
                        key="ai_limits",
                        schema_version=1,
                        version=command.expected_version,
                        value=command.value,
                    )
                )
            except Exception as error:
                raise InvalidInputError("AI 限额设置无效") from error
            setting = await uow.session.scalar(
                select(Setting)
                .where(Setting.key == command.target_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (setting.version if setting else 0) != command.expected_version:
                raise OptimisticLockError("设置已变化，请重新预览")
        else:
            if command.target_id is not None:
                memory = await uow.repositories.memories.get_for_update_or_raise(command.target_id)
                if (
                    memory.user_id != actor.user_id
                    or memory.deleted_at is not None
                    or (memory.expires_at is not None and memory.expires_at <= facts.now)
                ):
                    raise NotFoundError("记忆不存在")
                if memory.version != command.expected_version:
                    raise OptimisticLockError("记忆已变化")
            if command.origin_run_id is not None:
                await run_facts(
                    uow, actor, command.origin_run_id, Operation.READ_RUN, require_context=False
                )

    async def _audit(
        self, uow: UnitOfWork, actor: ActorContext, action: Action, event: str
    ) -> None:
        await uow.repositories.audit_events.record(
            event_type=f"action.{event}",
            result=AuditResult.SUCCEEDED,
            actor_id=actor.user_id,
            action_id=action.id,
            resource_id=action.target_resource_id,
            after_version=action.version,
            metadata=AuditMetadata(after_status=action.status.value, run_id=action.run_id),
        )

    async def preview(
        self,
        actor: ActorContext,
        command: Command,
        *,
        idempotency_key: str,
        run_id: UUID | None = None,
        authorization_message_id: UUID | None = None,
    ) -> ActionDTO:
        if (
            not idempotency_key.strip()
            or len(idempotency_key) > 128
            or not isinstance(command, (ResourcePreview, SettingsPreview, MemoryPreview))
        ):
            raise InvalidInputError("动作或幂等标识无效")
        parameters = {"schema_version": 1, "command": command.model_dump(mode="json")}
        command = _COMMANDS.validate_json(json.dumps(parameters["command"]))
        digest = hashlib.sha256(
            json.dumps(
                parameters, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()
        async with self._uows() as uow:
            await lock_authentication(uow, actor)
            run = None
            if run_id is not None:
                _, _, run = await run_facts(
                    uow,
                    actor,
                    run_id,
                    Operation.CONTINUE_RUN,
                    extra_resource_ids=(command.target_id,)
                    if isinstance(command, ResourcePreview)
                    else (),
                )
                if authorization_message_id is None:
                    raise InvalidInputError("Agent 提议必须绑定用户授权消息")
            if authorization_message_id is not None:
                message = await uow.session.get(Message, authorization_message_id)
                if (
                    run is None
                    or message is None
                    or message.conversation_id != run.conversation_id
                    or message.role is not MessageRole.USER
                ):
                    raise NotFoundError("授权消息不存在")
            await self._target(uow, actor, command)
            assert actor.user_id is not None and actor.auth_session_id is not None
            creation = await uow.repositories.actions.propose(
                actor_id=actor.user_id,
                auth_session_id=actor.auth_session_id,
                run_id=run_id,
                authorization_message_id=authorization_message_id,
                action_type=ActionType(command.kind),
                target_resource_id=command.target_id
                if isinstance(command, ResourcePreview)
                else None,
                expected_version=command.expected_version,
                expected_acl_version=command.expected_acl_version
                if isinstance(command, ResourcePreview)
                else None,
                idempotency_key=idempotency_key,
                parameters=parameters,
                parameters_hash=digest,
            )
            if creation.created:
                if run is not None:
                    if run.status not in (RunStatus.QUEUED, RunStatus.RUNNING):
                        raise ConflictError("当前运行不能提出新的动作")
                    run.status = RunStatus.WAITING_APPROVAL
                    run.version += 1
                    await advance_context_generation(uow, run, preserve_sources=True)
                    await uow.session.flush()
                    await uow.repositories.run_events.emit(
                        run.id, RunEventType.ACTION_PROPOSED, {"action_id": str(creation.record.id)}
                    )
                    await uow.repositories.run_events.emit(
                        run.id, RunEventType.RUN_STATUS, {"status": run.status.value}
                    )
                await self._audit(uow, actor, creation.record, "proposed")
            return action_dto(creation.record)

    async def _action(
        self, uow: UnitOfWork, actor: ActorContext, action_id: UUID, operation: Operation
    ) -> Action:
        auth = await lock_authentication(uow, actor)
        probe = await uow.repositories.actions.get(action_id)
        if probe is None or probe.actor_id != actor.user_id:
            raise NotFoundError("动作不存在")
        resource_ids = {probe.target_resource_id} if probe.target_resource_id is not None else set()
        if probe.run_id is not None:
            await run_facts(uow, actor, probe.run_id, Operation.READ_RUN, require_context=False)
            resource_ids.update(
                source.resource_id
                for source in await uow.repositories.knowledge.sources(probe.run_id)
                if source.resource_id is not None
            )
        for resource_id in sorted(resource_ids):
            await uow.repositories.resources.get_for_update(resource_id)
        action = await uow.repositories.actions.get_for_update_or_raise(action_id)
        require_allowed(
            actor,
            PolicyFacts(
                operation=operation,
                authentication=auth,
                now=await uow.repositories.users.database_time(),
                current_scope_epoch=await uow.repositories.settings.get_acl_epoch(),
                target=TargetFacts(
                    object_id=action.id,
                    kind=TargetKind.ACTION,
                    owner_id=action.actor_id,
                    expires_at=action.expires_at,
                ),
            ),
        )
        return action

    async def read(self, actor: ActorContext, action_id: UUID) -> ActionDTO:
        async with self._uows() as uow:
            return action_dto(await self._action(uow, actor, action_id, Operation.READ_ACTION))

    async def confirm(
        self,
        actor: ActorContext,
        action_id: UUID,
        *,
        expected_action_version: int,
        parameters_hash: str,
    ) -> ActionDTO:
        async with self._uows() as uow:
            action = await self._action(uow, actor, action_id, Operation.EXECUTE_ACTION)
            if parameters_hash != action.parameters_hash:
                raise OptimisticLockError("确认摘要与预览不一致")
            now = await uow.repositories.users.database_time()
            if action.expires_at <= now and action.status in (
                ActionStatus.AWAITING_CONFIRMATION,
                ActionStatus.READY,
            ):
                action = await uow.repositories.actions.cancel(
                    action.id, action.version, expired=True
                )
                await self._cancel_waiting_run(uow, actor, action)
                await self._audit(uow, actor, action, "expired")
                return action_dto(action)
            await self._target(uow, actor, stored_command(action))
            if action.status is ActionStatus.READY:
                return action_dto(action)
            generation = None
            if action.run_id is not None:
                _, _, run = await run_facts(uow, actor, action.run_id, Operation.RESUME_RUN)
                if run.status is not RunStatus.WAITING_APPROVAL:
                    raise ConflictError("运行已离开确认等待")
                limits = read_ai_limits(await uow.repositories.settings.get("ai_limits")).for_role(
                    actor.role
                )
                if await uow.repositories.runs.executing_count(run.user_id) >= limits.concurrency:
                    raise ConcurrencyLimitError("账号执行名额已满")
                run.status = RunStatus.QUEUED
                run.auth_session_id = actor.auth_session_id
                run.version += 1
                await advance_context_generation(uow, run, preserve_sources=True)
                generation = run.execution_generation
                await uow.session.flush()
                await uow.repositories.run_events.emit(
                    run.id, RunEventType.RUN_STATUS, {"status": run.status.value}
                )
            assert actor.auth_session_id is not None
            action = await uow.repositories.actions.confirm(
                action.id, expected_action_version, actor.auth_session_id
            )
            await enqueue(
                uow,
                JobSpec(
                    kind="action.execute",
                    idempotency_key=f"action.execute:{action.id}",
                    actor_id=actor.user_id,
                    auth_session_id=actor.auth_session_id,
                    run_id=action.run_id,
                    resource_id=action.target_resource_id,
                    payload={
                        "schema_version": 1,
                        "action_id": str(action.id),
                        "parameters_hash": action.parameters_hash,
                        "execution_generation": generation,
                    },
                ),
            )
            await self._audit(uow, actor, action, "confirmed")
            await self._action(uow, actor, action_id, Operation.EXECUTE_ACTION)
            return action_dto(action)

    async def _cancel_waiting_run(
        self, uow: UnitOfWork, actor: ActorContext, action: Action
    ) -> None:
        if action.run_id is None:
            return
        _, _, run = await run_facts(
            uow, actor, action.run_id, Operation.READ_RUN, require_context=False
        )
        if run.status in (RunStatus.WAITING_APPROVAL, RunStatus.QUEUED):
            run.status = RunStatus.CANCELLED
            run.finished_at = await uow.repositories.users.database_time()
            run.version += 1
            await advance_context_generation(uow, run, preserve_sources=False)
            await uow.session.flush()
            if not await uow.repositories.provider_calls.model_was_dispatched(run.id):
                await QuotaService(self._uows).release_before_dispatch_in_uow(uow, run.id)
            await uow.repositories.run_events.emit(
                run.id, RunEventType.DONE, {"status": run.status.value}
            )

    async def cancel(
        self, actor: ActorContext, action_id: UUID, *, expected_action_version: int
    ) -> ActionDTO:
        async with self._uows() as uow:
            action = await self._action(uow, actor, action_id, Operation.CANCEL_ACTION)
            if action.status in (ActionStatus.CANCELLED, ActionStatus.EXPIRED):
                return action_dto(action)
            action = await uow.repositories.actions.cancel(action.id, expected_action_version)
            await self._cancel_waiting_run(uow, actor, action)
            await self._audit(uow, actor, action, "cancelled")
            return action_dto(action)

"""明确指定已有版本的发布/撤回：鉴权、修改、Action、审计与作业同一短事务。"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from autumn_backend.db.enums import ActionStatus, ActionType, AuditResult, JobStatus, ResourceKind
from autumn_backend.db.models import Action, Publication, Resource
from autumn_backend.db.models.content import PUBLIC_FIELD_NAMES
from autumn_backend.db.session import UnitOfWork, UnitOfWorkFactory
from autumn_backend.errors import ConflictError, InvalidInputError
from autumn_backend.jobs.queue import enqueue
from autumn_backend.policies import ActorContext
from autumn_backend.policies.facts import Operation
from autumn_backend.repositories.audit import AuditMetadata
from autumn_backend.repositories.jobs import JobSpec
from autumn_backend.repositories.publications import PublicationProjection
from autumn_backend.services.access import lock_authentication, publication_facts, require_allowed

# 输入沿用前端原稿字段名，存储仍用公开投影字段名。身份字段不在这个映射中。
PUBLIC_FIELD_ALIASES = {"body_text": "body", "private_note": "note"}
PUBLIC_FIELDS_BY_KIND = dict.fromkeys(ResourceKind, frozenset(PUBLIC_FIELD_NAMES))


def _validate_request(resource_id: UUID, version: int, acl_version: int, key: str) -> None:
    if not isinstance(resource_id, UUID):
        raise InvalidInputError("资源 ID 必须是 UUID")
    if any(type(value) is not int or value < 0 for value in (version, acl_version)):
        raise InvalidInputError("内容版本与公开范围版本必须是非负整数")
    if not isinstance(key, str) or not key.strip() or len(key) > 128:
        raise InvalidInputError("幂等键不能为空或超过 128 字符")


@dataclass(frozen=True, slots=True, kw_only=True)
class PublishCommand:
    resource_id: UUID
    revision_id: UUID
    expected_version: int
    expected_acl_version: int
    idempotency_key: str
    public_fields: frozenset[str]
    ai_enabled: bool = False
    raw_download_enabled: bool = False

    def __post_init__(self) -> None:
        _validate_request(
            self.resource_id, self.expected_version, self.expected_acl_version, self.idempotency_key
        )
        if not isinstance(self.revision_id, UUID):
            raise InvalidInputError("原稿版本 ID 必须是 UUID")
        if (
            not isinstance(self.public_fields, frozenset)
            or not self.public_fields
            or any(not isinstance(field, str) for field in self.public_fields)
        ):
            raise InvalidInputError("公开字段必须是非空不可变集合")
        if type(self.ai_enabled) is not bool or type(self.raw_download_enabled) is not bool:
            raise InvalidInputError("公开权限开关必须是布尔值")


@dataclass(frozen=True, slots=True, kw_only=True)
class RevokeCommand:
    resource_id: UUID
    expected_version: int
    expected_acl_version: int
    idempotency_key: str

    def __post_init__(self) -> None:
        _validate_request(
            self.resource_id, self.expected_version, self.expected_acl_version, self.idempotency_key
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PublicationResult:
    action_id: UUID
    resource_id: UUID
    publication_id: UUID | None
    public_url: str | None
    actual_public_fields: tuple[str, ...]
    resource_version: int
    acl_version: int
    scope_epoch: int
    changed: bool
    is_current: bool
    index_job_id: UUID | None
    index_job_status: JobStatus | None


def _parameters(command: PublishCommand | RevokeCommand) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "resource_id": str(command.resource_id),
        "expected_version": command.expected_version,
        "expected_acl_version": command.expected_acl_version,
    }
    if isinstance(command, PublishCommand):
        parameters.update(
            revision_id=str(command.revision_id),
            public_fields=sorted(
                {PUBLIC_FIELD_ALIASES.get(field, field) for field in command.public_fields}
            ),
            ai_enabled=command.ai_enabled,
            raw_download_enabled=command.raw_download_enabled,
        )
    return parameters


def _hash(parameters: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(parameters, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _result_id(data: dict[str, Any], key: str) -> UUID | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConflictError("已完成操作的结果标识损坏")
    try:
        return UUID(value)
    except ValueError as error:
        raise ConflictError("已完成操作的结果标识损坏") from error


class PublicationService:
    """只能由确认了明确用户请求的服务端入口调用，不能直接暴露给模型参数。

    本入口自行记录 explicit_request Action。生成内容或不明确范围须由 E7 提供
    持久预览/确认流程，不能通过设置某个请求字段绕过确认。
    """

    def __init__(self, uows: UnitOfWorkFactory) -> None:
        self._uows = uows

    async def publish_explicit(
        self, actor: ActorContext, command: PublishCommand, *, request_id: str | None = None
    ) -> PublicationResult:
        return await self._execute(actor, command, request_id)

    async def revoke_explicit(
        self, actor: ActorContext, command: RevokeCommand, *, request_id: str | None = None
    ) -> PublicationResult:
        return await self._execute(actor, command, request_id)

    async def _execute(
        self,
        actor: ActorContext,
        command: PublishCommand | RevokeCommand,
        request_id: str | None,
    ) -> PublicationResult:
        publishing = isinstance(command, PublishCommand)
        operation = Operation.PUBLISH_RESOURCE if publishing else Operation.REVOKE_PUBLICATION
        action_type = ActionType.PUBLISH if publishing else ActionType.REVOKE
        parameters = _parameters(command)
        async with self._uows() as uow:
            auth = await lock_authentication(uow, actor)
            resource = await uow.repositories.resources.get_for_update(command.resource_id)
            facts = await publication_facts(uow, operation, auth, resource)
            require_allowed(actor, facts)
            assert resource is not None and actor.user_id is not None
            assert actor.auth_session_id is not None
            creation = await uow.repositories.actions.begin_explicit_publication(
                actor_id=actor.user_id,
                auth_session_id=actor.auth_session_id,
                action_type=action_type,
                resource_id=resource.id,
                expected_version=command.expected_version,
                expected_acl_version=command.expected_acl_version,
                idempotency_key=command.idempotency_key,
                parameters=parameters,
                parameters_hash=_hash(parameters),
            )
            action = creation.record
            if action.status is not ActionStatus.SUCCEEDED:
                if action.status is not ActionStatus.READY or action.expires_at <= facts.now:
                    raise ConflictError("操作已过期或不处于可执行状态")
                before_acl = resource.acl_version
                publication: Publication | None
                publication_id = None
                changed = True
                if isinstance(command, PublishCommand):
                    fields = frozenset(parameters["public_fields"])
                    if not fields <= PUBLIC_FIELDS_BY_KIND[resource.kind]:
                        raise InvalidInputError("当前类型不允许所选公开字段")
                    if command.raw_download_enabled and resource.kind is not ResourceKind.DOCUMENT:
                        raise InvalidInputError("首版仅文档可开放原文件下载")
                    publication = await uow.repositories.publications.publish_under_resource_lock(
                        resource_id=resource.id,
                        revision_id=command.revision_id,
                        expected_version=command.expected_version,
                        expected_acl_version=command.expected_acl_version,
                        published_by=actor.user_id,
                        projection=PublicationProjection(
                            fields, command.ai_enabled, command.raw_download_enabled
                        ),
                    )
                    publication_id = publication.id
                else:
                    publication = await uow.repositories.publications.current_for_resource(
                        resource.id
                    )
                    publication_id = publication.id if publication is not None else None
                    changed = await uow.repositories.publications.revoke(
                        resource.id,
                        expected_version=command.expected_version,
                        expected_acl_version=command.expected_acl_version,
                    )
                resource = await uow.repositories.resources.get_for_update_or_raise(resource.id)
                facts = await publication_facts(uow, operation, auth, resource)
                require_allowed(actor, facts)
                job_id = None
                if changed:
                    job = await enqueue(
                        uow,
                        JobSpec(
                            kind="knowledge.publication_sync",
                            idempotency_key=f"knowledge.publication_sync:{action.id}",
                            actor_id=actor.user_id,
                            auth_session_id=actor.auth_session_id,
                            resource_id=resource.id,
                            payload={
                                "scope": "public",
                                "resource_id": str(resource.id),
                                "publication_id": str(publication_id) if publishing else None,
                                "acl_version": resource.acl_version,
                                "scope_epoch": facts.current_scope_epoch,
                                "ai_enabled": command.ai_enabled
                                if isinstance(command, PublishCommand)
                                else False,
                            },
                        ),
                    )
                    job_id = job.record.id
                await uow.repositories.audit_events.record(
                    event_type="resource.publish" if publishing else "resource.revoke",
                    result=AuditResult.SUCCEEDED,
                    actor_id=actor.user_id,
                    action_id=action.id,
                    resource_id=resource.id,
                    before_version=resource.version,
                    after_version=resource.version,
                    request_id=request_id,
                    metadata=AuditMetadata(
                        before_acl_version=before_acl,
                        after_acl_version=resource.acl_version,
                        scope_epoch=facts.current_scope_epoch,
                        changed_fields=("public_fields", "ai_enabled", "raw_download_enabled")
                        if publishing
                        else ("revoked_at",),
                        publication_id=publication_id,
                        job_id=job_id,
                    ),
                )
                action = await uow.repositories.actions.succeed_explicit(
                    action.id,
                    {
                        "publication_id": str(publication_id) if publication_id else None,
                        "resource_version": resource.version,
                        "acl_version": resource.acl_version,
                        "scope_epoch": facts.current_scope_epoch,
                        "changed": changed,
                        "index_job_id": str(job_id) if job_id else None,
                    },
                )
            result = await self._result(uow, action, resource, facts.now)
            # 最后一次实际数据库时间检查，过期/失权时回滚本事务全部副作用。
            require_allowed(actor, await publication_facts(uow, operation, auth, resource))
            return result

    @staticmethod
    async def _result(
        uow: UnitOfWork, action: Action, resource: Resource, now: datetime
    ) -> PublicationResult:
        data = action.result
        if (
            data is None
            or any(
                type(data.get(key)) is not int or data[key] < 0
                for key in ("resource_version", "acl_version", "scope_epoch")
            )
            or type(data.get("changed")) is not bool
        ):
            raise ConflictError("已完成操作的结果不可读取")
        publication_id = _result_id(data, "publication_id")
        job_id = _result_id(data, "index_job_id")
        publication = (
            await uow.repositories.publications.get_for_update(publication_id)
            if publication_id is not None
            else None
        )
        job = await uow.repositories.jobs.get(job_id) if job_id is not None else None
        if publication is not None and publication.resource_id != resource.id:
            raise ConflictError("已完成操作的公开版本归属不匹配")
        current = (
            publication is not None
            and publication.revoked_at is None
            and resource.is_active
            and (resource.expires_at is None or resource.expires_at > now)
        )
        url = None
        if current and publication is not None:
            url = f"/api/public/sources/{publication.id}"
            if resource.kind is ResourceKind.ARTICLE and re.fullmatch(
                r"[a-z0-9][a-z0-9_-]{0,159}", resource.slug
            ):
                url = f"/notes/{resource.slug}"
        return PublicationResult(
            action_id=action.id,
            resource_id=resource.id,
            publication_id=publication_id,
            public_url=url,
            actual_public_fields=tuple(publication.public_fields)
            if publication is not None
            else (),
            resource_version=data["resource_version"],
            acl_version=data["acl_version"],
            scope_epoch=data["scope_epoch"],
            changed=data["changed"],
            is_current=current,
            index_job_id=job_id,
            index_job_status=job.status if job is not None else None,
        )

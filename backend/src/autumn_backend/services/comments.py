"""E3 留言提交：当前权限、一级回复、提交去重与审计在同一短事务完成。"""

from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID

from autumn_backend.db.enums import AuditResult, CommentStatus
from autumn_backend.db.models import Resource
from autumn_backend.db.session import UnitOfWork, UnitOfWorkFactory
from autumn_backend.errors import ConflictError, InvalidInputError, NotFoundError
from autumn_backend.policies import ActorContext
from autumn_backend.policies.facts import (
    AuthenticationFacts,
    Operation,
    PolicyFacts,
    PublicationFacts,
    ResourceFacts,
    TargetFacts,
    TargetKind,
)
from autumn_backend.repositories.audit import AuditMetadata
from autumn_backend.services.access import lock_authentication, require_allowed


@dataclass(frozen=True, slots=True, kw_only=True)
class CreateCommentCommand:
    client_id: UUID
    body: str
    resource_id: UUID | None = None
    parent_id: UUID | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.client_id, UUID) or any(
            value is not None and not isinstance(value, UUID)
            for value in (self.resource_id, self.parent_id)
        ):
            raise InvalidInputError("留言、资源与父留言标识必须是 UUID")
        if not isinstance(self.body, str) or not self.body.strip() or len(self.body) > 2000:
            raise InvalidInputError("留言必须为非空文本且不超过 2000 字符")


@dataclass(frozen=True, slots=True, kw_only=True)
class CommentDTO:
    id: UUID
    resource_id: UUID | None
    parent_id: UUID | None
    author_display_name: str
    body: str
    status: CommentStatus
    created_at: datetime
    updated_at: datetime
    version: int


class CommentService:
    def __init__(self, uows: UnitOfWorkFactory) -> None:
        self._uows = uows

    async def create_comment(
        self,
        actor: ActorContext,
        command: CreateCommentCommand,
        *,
        request_id: str | None = None,
    ) -> CommentDTO:
        async with self._uows() as uow:
            auth = await lock_authentication(uow, actor)
            # 先认证，不用“对象不存在”掩盖匿名/失效登录；留言不受 AI 冷却限制。
            facts = await self._facts(uow, auth, None, None)
            require_allowed(actor, facts)
            assert actor.user_id is not None
            resource = None
            if command.resource_id is not None:
                resource = await uow.repositories.resources.get_for_update(command.resource_id)
            facts = await self._facts(uow, auth, resource, command.resource_id)
            require_allowed(actor, facts)
            # author/client 范围查询，先锁原结果；不枚举其他人的提交身份。
            existing = await uow.repositories.comments.by_client_id(
                actor.user_id, command.client_id, lock=True
            )
            if existing is not None and existing.deleted_at is not None:
                raise NotFoundError("留言不存在")
            if existing is None and command.parent_id is not None:
                parent = await uow.repositories.comments.get_for_update(command.parent_id)
                if parent is not None and parent.resource_id != command.resource_id:
                    raise NotFoundError("留言不存在")
                facts = replace(
                    facts,
                    requested_parent_id=command.parent_id,
                    target=TargetFacts(
                        object_id=parent.id,
                        kind=TargetKind.COMMENT,
                        owner_id=parent.author_id,
                        resource_id=parent.resource_id,
                        is_deleted=parent.deleted_at is not None,
                        comment_approved=parent.status is CommentStatus.APPROVED,
                    )
                    if parent is not None
                    else None,
                    now=await uow.repositories.comments.database_time(),
                )
                require_allowed(actor, facts)
                # 已确认可见的父留言若本身是回复，由 B8 仲裁为 409；不泄露不可见对象的层级。
            creation = await uow.repositories.comments.create_or_get(
                author_id=actor.user_id,
                client_id=command.client_id,
                body=command.body,
                resource_id=command.resource_id,
                parent_id=command.parent_id,
            )
            record = creation.record
            if record.deleted_at is not None:
                raise NotFoundError("留言不存在")
            if creation.created:
                await uow.repositories.audit_events.record(
                    event_type="comment.create",
                    result=AuditResult.SUCCEEDED,
                    actor_id=actor.user_id,
                    resource_id=command.resource_id,
                    request_id=request_id,
                    metadata=AuditMetadata(comment_id=record.id, after_status=record.status.value),
                )
            author = await uow.repositories.users.get(actor.user_id)
            if author is None:
                raise ConflictError("留言作者记录不可用")
            result = CommentDTO(
                id=record.id,
                resource_id=record.resource_id,
                parent_id=record.parent_id,
                author_display_name=author.display_name or "秋序访客",
                body=record.body,
                status=record.status,
                created_at=record.created_at,
                updated_at=record.updated_at,
                version=record.version,
            )
            # user/session/resource/parent 的记录仍持锁；到期条件按最后的实际 DB 时间复核。
            require_allowed(
                actor, replace(facts, now=await uow.repositories.comments.database_time())
            )
            return result

    @staticmethod
    async def _facts(
        uow: UnitOfWork,
        auth: AuthenticationFacts | None,
        resource: Resource | None,
        resource_id: UUID | None,
    ) -> PolicyFacts:
        snapshot = None
        if resource is not None and resource.current_revision_id is not None:
            publication = await uow.repositories.publications.current_for_resource(resource.id)
            snapshot = ResourceFacts(
                resource_id=resource.id,
                owner_id=resource.owner_id,
                current_revision_id=resource.current_revision_id,
                acl_version=resource.acl_version,
                is_deleted=resource.deleted_at is not None,
                is_archived=resource.archived_at is not None,
                expires_at=resource.expires_at,
                publication=PublicationFacts(
                    publication_id=publication.id,
                    resource_id=publication.resource_id,
                    revision_id=publication.revision_id,
                    is_current=publication.revoked_at is None,
                    ai_enabled=publication.ai_enabled,
                    raw_download_enabled=publication.raw_download_enabled,
                    revoked_at=publication.revoked_at,
                )
                if publication is not None
                else None,
            )
        return PolicyFacts(
            operation=Operation.CREATE_COMMENT,
            authentication=auth,
            resource=snapshot,
            requested_resource_id=resource_id,
            now=await uow.repositories.comments.database_time(),
            current_scope_epoch=await uow.repositories.settings.get_acl_epoch(),
        )

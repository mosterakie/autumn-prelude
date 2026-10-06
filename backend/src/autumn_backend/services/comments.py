"""E3 留言提交：当前权限、一级回复、提交去重与审计在同一短事务完成。"""

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from autumn_backend.db.enums import AuditResult, CommentStatus, ReportStatus
from autumn_backend.db.models import Comment, Report, Resource
from autumn_backend.db.session import UnitOfWork, UnitOfWorkFactory
from autumn_backend.errors import (
    ConflictError,
    InvalidInputError,
    NotFoundError,
    OptimisticLockError,
)
from autumn_backend.policies import ActorContext, ActorRole
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

    @staticmethod
    def _text(value: str) -> str:
        if not value.strip() or len(value) > 2000:
            raise InvalidInputError("内容不能为空或超过 2000 字符")
        return value.replace("\r\n", "\n").replace("\r", "\n")

    @staticmethod
    async def _dto(uow: UnitOfWork, comment: Comment) -> CommentDTO:
        author = await uow.repositories.users.get(comment.author_id)
        return CommentDTO(
            id=comment.id,
            resource_id=comment.resource_id,
            parent_id=comment.parent_id,
            author_display_name=author.display_name or "秋序访客" if author else "秋序访客",
            body=comment.body,
            status=comment.status,
            created_at=comment.created_at,
            updated_at=comment.updated_at,
            version=comment.version,
        )

    @staticmethod
    def _target(comment: Comment) -> TargetFacts:
        return TargetFacts(
            object_id=comment.id,
            kind=TargetKind.COMMENT,
            owner_id=comment.author_id,
            resource_id=comment.resource_id,
            is_deleted=comment.deleted_at is not None,
            comment_approved=comment.status is CommentStatus.APPROVED,
        )

    async def _locked(
        self,
        uow: UnitOfWork,
        actor: ActorContext,
        comment_id: UUID,
        operation: Operation,
    ) -> tuple[Comment, PolicyFacts]:
        auth = await lock_authentication(uow, actor)
        probe = await uow.repositories.comments.get_or_raise(comment_id)
        resource = (
            await uow.repositories.resources.get_for_update(probe.resource_id)
            if probe.resource_id
            else None
        )
        parent = (
            await uow.repositories.comments.get_for_update(probe.parent_id)
            if probe.parent_id
            else None
        )
        comment = await uow.repositories.comments.get_for_update_or_raise(comment_id)
        facts = replace(
            await self._facts(uow, auth, resource, comment.resource_id),
            operation=operation,
            target=self._target(comment),
        )
        require_allowed(actor, facts)
        if (
            operation in (Operation.READ_PUBLIC_COMMENT, Operation.REPORT_COMMENT)
            and comment.parent_id is not None
        ):
            if parent is None or parent.resource_id != comment.resource_id:
                raise NotFoundError("留言不存在")
            require_allowed(
                actor,
                replace(
                    facts, operation=Operation.READ_PUBLIC_COMMENT, target=self._target(parent)
                ),
            )
        return comment, facts

    async def public_page(
        self, *, resource_id: UUID | None, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        anonymous = ActorContext(
            user_id=None,
            role=ActorRole.ANONYMOUS,
            auth_session_id=None,
            step_up_expires_at=None,
            capabilities=frozenset(),
            scope_epoch=0,
        )
        async with self._uows() as uow:
            if resource_id is not None:
                resource = await uow.repositories.resources.get_for_update(resource_id)
                require_allowed(
                    anonymous,
                    replace(
                        await self._facts(uow, None, resource, resource_id),
                        operation=Operation.READ_PUBLIC_RESOURCE,
                    ),
                )
            page = await uow.repositories.comments.page(
                resource_id=resource_id, public=True, limit=limit, cursor=cursor
            )
            # 固定父 -> 子顺序；即使父留言不在当前页，也要锁定并复核其可见性。
            roots = {item.parent_id for item in page.items if item.parent_id is not None}
            roots.update(item.id for item in page.items if item.parent_id is None)
            for parent_id in sorted(roots):
                await uow.repositories.comments.get_for_update(parent_id)
            allowed = {}
            for candidate in sorted(
                page.items, key=lambda item: (item.parent_id is not None, item.id.int)
            ):
                try:
                    record, _ = await self._locked(
                        uow, anonymous, candidate.id, Operation.READ_PUBLIC_COMMENT
                    )
                    allowed[record.id] = await self._dto(uow, record)
                except NotFoundError:
                    continue
            return {
                "items": [allowed[item.id] for item in page.items if item.id in allowed],
                "next_cursor": page.next_cursor,
            }

    async def edit(
        self, actor: ActorContext, comment_id: UUID, *, body: str, expected_version: int
    ) -> CommentDTO:
        body = self._text(body)
        async with self._uows() as uow:
            previous, _ = await self._locked(uow, actor, comment_id, Operation.EDIT_COMMENT)
            before = previous.status.value
            record = await uow.repositories.comments.edit(comment_id, expected_version, body)
            await self._audit(uow, actor, record, "edit", before=before)
            await self._locked(uow, actor, comment_id, Operation.EDIT_COMMENT)
            return await self._dto(uow, record)

    async def delete(self, actor: ActorContext, comment_id: UUID, *, expected_version: int) -> None:
        async with self._uows() as uow:
            record, facts = await self._locked(uow, actor, comment_id, Operation.DELETE_COMMENT)
            record = await uow.repositories.comments.soft_delete(
                comment_id, expected_version, facts.now
            )
            await self._audit(uow, actor, record, "delete")
            require_allowed(actor, replace(facts, now=await uow.repositories.users.database_time()))

    async def _moderator(self, uow: UnitOfWork, actor: ActorContext) -> None:
        auth = await lock_authentication(uow, actor)
        require_allowed(
            actor,
            PolicyFacts(
                operation=Operation.MANAGE_MODERATION,
                authentication=auth,
                now=await uow.repositories.users.database_time(),
                current_scope_epoch=await uow.repositories.settings.get_acl_epoch(),
            ),
        )

    async def moderation_page(
        self,
        actor: ActorContext,
        *,
        status: Literal["pending", "approved", "rejected", "hidden"] | None,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        async with self._uows() as uow:
            await self._moderator(uow, actor)
            page = await uow.repositories.comments.page(
                status=CommentStatus(status) if status else None, limit=limit, cursor=cursor
            )
            result = {
                "items": [await self._dto(uow, item) for item in page.items],
                "next_cursor": page.next_cursor,
            }
            await self._moderator(uow, actor)
            return result

    async def decide(
        self,
        actor: ActorContext,
        comment_id: UUID,
        *,
        decision: Literal["approve", "reject", "hide"],
        reason: str,
        expected_version: int,
    ) -> CommentDTO:
        self._text(reason)
        async with self._uows() as uow:
            await self._moderator(uow, actor)
            record, facts = await self._locked(uow, actor, comment_id, Operation.MANAGE_MODERATION)
            if record.deleted_at is not None:
                raise NotFoundError("留言不存在")
            if decision == "approve" and record.resource_id is not None:
                require_allowed(actor, replace(facts, operation=Operation.READ_PUBLIC_RESOURCE))
            before = record.status.value
            assert actor.user_id is not None
            record = await uow.repositories.comments.decide(
                comment_id,
                expected_version,
                {
                    "approve": CommentStatus.APPROVED,
                    "reject": CommentStatus.REJECTED,
                    "hide": CommentStatus.HIDDEN,
                }[decision],
                actor.user_id,
            )
            await self._audit(uow, actor, record, "moderate", before=before)
            await self._moderator(uow, actor)
            return await self._dto(uow, record)

    @staticmethod
    def _report_dto(report: Report) -> dict[str, Any]:
        return {
            "id": report.id,
            "comment_id": report.comment_id,
            "reason": report.reason,
            "status": report.status.value,
            "version": report.version,
            "created_at": report.created_at,
            "resolution": report.resolution_note,
            "resolved_at": report.resolved_at,
        }

    async def report(self, actor: ActorContext, comment_id: UUID, *, reason: str) -> dict[str, Any]:
        reason = self._text(reason).strip()
        async with self._uows() as uow:
            comment, _ = await self._locked(uow, actor, comment_id, Operation.REPORT_COMMENT)
            assert actor.user_id is not None
            creation = await uow.repositories.reports.create_or_get(
                actor.user_id, comment_id, reason
            )
            if creation.created:
                await self._audit(uow, actor, comment, "report")
            await self._locked(uow, actor, comment_id, Operation.REPORT_COMMENT)
            return self._report_dto(creation.record)

    async def reports_page(
        self,
        actor: ActorContext,
        *,
        status: Literal["open", "resolved", "dismissed"],
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        async with self._uows() as uow:
            await self._moderator(uow, actor)
            page = await uow.repositories.reports.page(
                status=ReportStatus(status), limit=limit, cursor=cursor
            )
            result = {
                "items": [self._report_dto(item) for item in page.items],
                "next_cursor": page.next_cursor,
            }
            await self._moderator(uow, actor)
            return result

    async def resolve_report(
        self,
        actor: ActorContext,
        report_id: UUID,
        *,
        resolution: str,
        expected_version: int,
        dismissed: bool = False,
    ) -> dict[str, Any]:
        resolution = self._text(resolution).strip()
        async with self._uows() as uow:
            await self._moderator(uow, actor)
            probe = await uow.repositories.reports.get_or_raise(report_id)
            comment, _ = await self._locked(
                uow, actor, probe.comment_id, Operation.MANAGE_MODERATION
            )
            report = await uow.repositories.reports.get_for_update_or_raise(report_id)
            if report.status is not ReportStatus.OPEN:
                if (
                    report.resolution_note == resolution
                    and (report.status is ReportStatus.DISMISSED) == dismissed
                ):
                    return self._report_dto(report)
                raise OptimisticLockError("举报已处理")
            assert actor.user_id is not None
            report = await uow.repositories.reports.resolve(
                report_id, expected_version, actor.user_id, resolution, dismissed=dismissed
            )
            await self._audit(uow, actor, comment, "resolve_report")
            await self._moderator(uow, actor)
            return self._report_dto(report)

    @staticmethod
    async def _audit(
        uow: UnitOfWork,
        actor: ActorContext,
        comment: Comment,
        event: str,
        *,
        before: str | None = None,
    ) -> None:
        await uow.repositories.audit_events.record(
            event_type=f"comment.{event}",
            result=AuditResult.SUCCEEDED,
            actor_id=actor.user_id,
            resource_id=comment.resource_id,
            metadata=AuditMetadata(
                comment_id=comment.id,
                before_status=before,
                after_status=comment.status.value,
                comment_version=comment.version,
            ),
        )

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

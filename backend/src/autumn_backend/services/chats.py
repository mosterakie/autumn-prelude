"""会话与运行读写入口；消息输出先校验完整当前来源闭包。"""

from dataclasses import replace
from typing import Any
from uuid import UUID

from sqlalchemy import select

from autumn_backend.db.enums import (
    TERMINAL_RUN_STATUSES,
    MessageRole,
    RunEventType,
    RunStatus,
)
from autumn_backend.db.enums import ConversationMode as Mode
from autumn_backend.db.models import (
    Action,
    Conversation,
    KnowledgeChunk,
    Message,
    ResourceVersion,
    Run,
    RunSource,
)
from autumn_backend.db.session import UnitOfWork, UnitOfWorkFactory
from autumn_backend.errors import (
    ConcurrencyLimitError,
    ConflictError,
    InvalidInputError,
    NotFoundError,
)
from autumn_backend.policies import ActorContext
from autumn_backend.policies.facts import (
    ConversationMode,
    Operation,
    PolicyFacts,
    TargetFacts,
    TargetKind,
)
from autumn_backend.repositories.jobs import JobSpec
from autumn_backend.repositories.pagination import fetch_page
from autumn_backend.services.access import AuthorizationError, lock_authentication, require_allowed
from autumn_backend.services.actions import action_dto
from autumn_backend.services.ai_limits import read_ai_limits
from autumn_backend.services.context import advance_context_generation, run_facts, source_fact
from autumn_backend.services.input_waits import InputWaitService, load_input
from autumn_backend.services.quota import QuotaService


class ChatService:
    def __init__(self, uows: UnitOfWorkFactory) -> None:
        self.uows = uows

    async def _conversation(
        self,
        uow: UnitOfWork,
        actor: ActorContext,
        identifier: UUID,
        operation: Operation = Operation.READ_CONVERSATION,
    ) -> Conversation:
        auth = await lock_authentication(uow, actor)
        conversation = (
            await uow.repositories.conversations.for_user_for_update(identifier, actor.user_id)
            if actor.user_id
            else None
        )
        facts = PolicyFacts(
            operation=operation,
            authentication=auth,
            now=await uow.repositories.users.database_time(),
            current_scope_epoch=await uow.repositories.settings.get_acl_epoch(),
            target=TargetFacts(
                object_id=conversation.id,
                kind=TargetKind.CONVERSATION,
                owner_id=conversation.user_id,
                mode=ConversationMode(conversation.mode.value),
                is_deleted=conversation.deleted_at is not None,
                expires_at=conversation.expires_at,
            )
            if conversation
            else None,
        )
        require_allowed(actor, facts)
        assert conversation is not None
        return conversation

    async def _mode(self, uow: UnitOfWork, actor: ActorContext, mode: str) -> None:
        require_allowed(
            actor,
            PolicyFacts(
                operation=Operation.CREATE_CONVERSATION,
                authentication=await lock_authentication(uow, actor),
                now=await uow.repositories.users.database_time(),
                current_scope_epoch=await uow.repositories.settings.get_acl_epoch(),
                requested_mode=ConversationMode(mode),
            ),
        )

    @staticmethod
    async def _conversation_dto(uow: UnitOfWork, conversation: Conversation) -> dict[str, Any]:
        active = await uow.repositories.runs.active_for_conversation(conversation.id)
        return {
            "id": conversation.id,
            "title": conversation.title,
            "mode": conversation.mode.value,
            "version": conversation.version,
            "created_at": conversation.created_at,
            "active_run_id": active.id if active else None,
        }

    async def create(self, actor: ActorContext, *, mode: str, title: str | None) -> dict[str, Any]:
        if mode not in {"public", "owner"} or (title is not None and len(title) > 200):
            raise InvalidInputError("会话参数无效")
        async with self.uows() as uow:
            await self._mode(uow, actor, mode)
            assert actor.user_id is not None
            conversation = await uow.repositories.conversations.create(
                user_id=actor.user_id, mode=Mode(mode), title=title or "新的对话"
            )
            return await self._conversation_dto(uow, conversation)

    async def list(
        self, actor: ActorContext, *, mode: str, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        if mode not in {"public", "owner"}:
            raise InvalidInputError("会话模式无效")
        async with self.uows() as uow:
            await self._mode(uow, actor, mode)
            assert actor.user_id is not None
            page = await uow.repositories.conversations.for_user(
                actor.user_id, mode=Mode(mode), limit=limit, cursor=cursor
            )
            return {
                "items": [await self._conversation_dto(uow, item) for item in page.items],
                "next_cursor": page.next_cursor,
            }

    async def read(self, actor: ActorContext, identifier: UUID) -> dict[str, Any]:
        async with self.uows() as uow:
            return await self._conversation_dto(
                uow, await self._conversation(uow, actor, identifier)
            )

    async def rename(
        self, actor: ActorContext, identifier: UUID, *, version: int, title: str
    ) -> dict[str, Any]:
        if not title.strip() or len(title) > 200:
            raise InvalidInputError("会话标题无效")
        async with self.uows() as uow:
            await self._conversation(uow, actor, identifier, Operation.UPDATE_CONVERSATION)
            conversation = await uow.repositories.conversations.rename(
                identifier, expected_version=version, title=title.strip()
            )
            return await self._conversation_dto(uow, conversation)

    async def _cancel(self, uow: UnitOfWork, actor: ActorContext, run: Run) -> None:
        if run.status in TERMINAL_RUN_STATUSES:
            return
        run.status, run.version = RunStatus.CANCELLED, run.version + 1
        run.finished_at = await uow.repositories.users.database_time()
        await advance_context_generation(uow, run, preserve_sources=False)
        await uow.session.flush()
        if not await uow.repositories.provider_calls.model_was_dispatched(run.id):
            await QuotaService(self.uows).release_before_dispatch_in_uow(uow, run.id)
        pending = (
            (
                await uow.session.execute(
                    select(Action)
                    .where(
                        Action.run_id == run.id,
                        Action.status.in_(("proposed", "awaiting_confirmation", "ready")),
                    )
                    .order_by(Action.id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for action in pending:
            action.status, action.version = action.status.__class__.CANCELLED, action.version + 1
        # 旧 worker 由代际闸门拒绝提交；停止上游由 H 的取消 handler 尝试。
        await uow.repositories.jobs.enqueue(
            JobSpec(
                kind="run.cancel",
                idempotency_key=f"run.cancel:{run.id}",
                payload={"run_id": str(run.id), "execution_generation": run.execution_generation},
                actor_id=actor.user_id,
                run_id=run.id,
                auth_session_id=actor.auth_session_id,
            )
        )
        await uow.repositories.run_events.emit(
            run.id, RunEventType.RUN_STATUS, {"status": run.status.value}
        )
        await uow.repositories.run_events.emit(
            run.id, RunEventType.DONE, {"status": run.status.value}
        )

    async def delete(self, actor: ActorContext, identifier: UUID, *, version: int) -> None:
        async with self.uows() as uow:
            await self._conversation(uow, actor, identifier, Operation.DELETE_CONVERSATION)
            active = await uow.repositories.runs.active_for_conversation(identifier)
            if active is not None:
                _, _, active = await run_facts(
                    uow, actor, active.id, Operation.CANCEL_RUN, require_context=False
                )
                await self._cancel(uow, actor, active)
            await uow.repositories.conversations.soft_delete(identifier, expected_version=version)
            await uow.repositories.jobs.enqueue(
                JobSpec(
                    kind="conversation.cleanup",
                    idempotency_key=f"conversation.cleanup:{identifier}",
                    payload={"conversation_id": str(identifier)},
                    actor_id=actor.user_id,
                    auth_session_id=actor.auth_session_id,
                )
            )

    async def _message(
        self, uow: UnitOfWork, actor: ActorContext, message: Message
    ) -> dict[str, Any]:
        hidden = False
        citations = []
        if message.role is MessageRole.ASSISTANT:
            try:
                if message.run_id is None:
                    raise NotFoundError("消息没有完整来源记录")
                _, _, run = await run_facts(uow, actor, message.run_id, Operation.EMIT_RUN_OUTPUT)
                sources = await uow.repositories.knowledge.sources(
                    run.id, context_generation=run.execution_generation
                )
                for source in sources:
                    if source.chunk_id is not None or source.web_url is not None:
                        citations.append({"id": source.id, "title": source.web_title or "引用资料"})
            except (AuthorizationError, NotFoundError):
                hidden = True
        return {
            "id": message.id,
            "role": message.role.value,
            "body": "" if hidden else message.body_text,
            "content_version": message.content_version,
            "status": "hidden" if hidden else message.status.value,
            "created_at": message.created_at,
            "citations": citations,
        }

    async def messages(
        self, actor: ActorContext, conversation_id: UUID, *, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        async with self.uows() as uow:
            await self._conversation(uow, actor, conversation_id)
            page = await fetch_page(
                uow.session,
                Message,
                select(Message).where(Message.conversation_id == conversation_id),
                limit=limit,
                cursor=cursor,
            )
            run_ids = sorted({item.run_id for item in page.items if item.run_id is not None})
            for identifier in run_ids:
                await uow.repositories.runs.get_for_update(identifier)
            resource_ids = (
                (
                    await uow.session.execute(
                        select(RunSource.resource_id)
                        .join(Run, Run.id == RunSource.run_id)
                        .where(
                            RunSource.run_id.in_(run_ids),
                            RunSource.context_generation == Run.execution_generation,
                            RunSource.resource_id.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for identifier in sorted({value for value in resource_ids if value is not None}):
                await uow.repositories.resources.get_for_update(identifier)
            # 获取最近的一页；展示按消息 seq 升序，游标仍按 created_at/id 指向更早页。
            return {
                "items": [
                    await self._message(uow, actor, item)
                    for item in sorted(page.items, key=lambda item: item.seq)
                ],
                "next_cursor": page.next_cursor,
            }

    async def _run_dto(self, uow: UnitOfWork, actor: ActorContext, run: Run) -> dict[str, Any]:
        current = (
            await uow.session.get(Message, run.current_message_id)
            if run.current_message_id
            else None
        )
        permitted = True
        if run.status not in {RunStatus.QUEUED, RunStatus.CANCELLED, RunStatus.FAILED}:
            try:
                await run_facts(uow, actor, run.id, Operation.EMIT_RUN_OUTPUT)
            except (AuthorizationError, NotFoundError):
                permitted = False
        actions = (
            (
                await uow.session.execute(
                    select(Action).where(
                        Action.run_id == run.id,
                        Action.actor_id == actor.user_id,
                        Action.status.in_(("proposed", "awaiting_confirmation", "ready")),
                    )
                )
            )
            .scalars()
            .all()
            if permitted
            else []
        )
        waiting = (
            load_input(run.input_request)
            if permitted and run.status is RunStatus.WAITING_INPUT and run.input_request
            else None
        )
        message = await self._message(uow, actor, current) if current else None
        visible_actions = []
        _, base, _ = await run_facts(uow, actor, run.id, Operation.READ_RUN, require_context=False)
        for action in actions:
            try:
                require_allowed(
                    actor,
                    replace(
                        base,
                        operation=Operation.READ_ACTION,
                        target=TargetFacts(
                            object_id=action.id,
                            kind=TargetKind.ACTION,
                            owner_id=action.actor_id,
                            requires_step_up=True,
                        ),
                    ),
                )
                visible_actions.append(action_dto(action))
            except (AuthorizationError, NotFoundError):
                continue
        return {
            "id": run.id,
            "run_id": run.id,
            "conversation_id": run.conversation_id,
            "status": run.status.value,
            "version": run.version,
            "execution_generation": run.execution_generation,
            "current_message": message,
            "message": message,
            "pending_actions": visible_actions,
            "input_request": {
                "id": waiting.id,
                "prompt": waiting.prompt,
                "options": waiting.options,
                "expires_at": waiting.expires_at,
            }
            if waiting
            else None,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
            "error": {"code": run.error_code} if run.error_code else None,
        }

    async def run(self, actor: ActorContext, identifier: UUID) -> dict[str, Any]:
        async with self.uows() as uow:
            _, _, run = await run_facts(
                uow, actor, identifier, Operation.READ_RUN, require_context=False
            )
            return await self._run_dto(uow, actor, run)

    async def cancel(self, actor: ActorContext, identifier: UUID) -> dict[str, Any]:
        async with self.uows() as uow:
            _, _, run = await run_facts(
                uow, actor, identifier, Operation.CANCEL_RUN, require_context=False
            )
            await self._cancel(uow, actor, run)
            return await self._run_dto(uow, actor, run)

    async def resume(
        self,
        actor: ActorContext,
        identifier: UUID,
        *,
        wait_id: UUID | None,
        answer: str | None,
        resume: bool,
    ) -> dict[str, Any]:
        if wait_id is not None and answer is not None and not resume:
            await InputWaitService(self.uows).answer(
                actor, identifier, wait_id=wait_id, answer=answer
            )
            return await self.run(actor, identifier)
        if not resume or wait_id is not None or answer is not None:
            raise InvalidInputError("恢复参数无效")
        async with self.uows() as uow:
            _, facts, run = await run_facts(
                uow, actor, identifier, Operation.READ_RUN, require_context=False
            )
            manifest = run.config_snapshot.get("context_manifest", {})
            # 验证在首个上下文装配前过期：只有当前 epoch 的空闭包可以补齐。
            if (
                run.status is RunStatus.WAITING_AUTH
                and run.error_code in ("SESSION_EXPIRED", "STEP_UP_REQUIRED")
                and isinstance(manifest, dict)
                and manifest.get("complete") is False
                and run.scope_epoch == facts.current_scope_epoch
                and not await uow.repositories.knowledge.sources(
                    run.id, context_generation=run.execution_generation
                )
            ):
                run.config_snapshot = {
                    **run.config_snapshot,
                    "context_manifest": {
                        "schema_version": 1,
                        "complete": True,
                        "context_generation": run.execution_generation,
                    },
                }
                await uow.session.flush()
            await run_facts(uow, actor, identifier, Operation.RESUME_RUN)
            if run.status is RunStatus.QUEUED and run.auth_session_id == actor.auth_session_id:
                return await self._run_dto(uow, actor, run)
            if run.status is not RunStatus.WAITING_AUTH:
                raise ConflictError("当前运行不等待身份验证")
            assert actor.user_id is not None
            limits = read_ai_limits(await uow.repositories.settings.get("ai_limits")).for_role(
                actor.role
            )
            if await uow.repositories.runs.executing_count(actor.user_id) >= limits.concurrency:
                raise ConcurrencyLimitError("当前执行名额已满")
            run.status, run.auth_session_id, run.version = (
                RunStatus.QUEUED,
                actor.auth_session_id,
                run.version + 1,
            )
            run.error_code = None
            await advance_context_generation(uow, run, preserve_sources=True)
            await uow.session.flush()
            await uow.repositories.jobs.enqueue(
                JobSpec(
                    kind="run.resume",
                    idempotency_key=f"run.resume:{run.id}:auth:{run.execution_generation}",
                    payload={
                        "run_id": str(run.id),
                        "execution_generation": run.execution_generation,
                    },
                    actor_id=actor.user_id,
                    run_id=run.id,
                    auth_session_id=actor.auth_session_id,
                )
            )
            await uow.repositories.run_events.emit(
                run.id, RunEventType.RUN_STATUS, {"status": run.status.value}
            )
            return await self._run_dto(uow, actor, run)

    async def citation(self, actor: ActorContext, identifier: UUID) -> dict[str, Any]:
        async with self.uows() as uow:
            source = await uow.session.get(RunSource, identifier)
            if source is None:
                raise NotFoundError("引用不存在")
            current_actor, facts, run = await run_facts(
                uow, actor, source.run_id, Operation.READ_RUN, require_context=False
            )
            dependency = await source_fact(uow, source, run.user_id)
            assert facts.target is not None
            require_allowed(
                current_actor,
                replace(
                    facts,
                    operation=Operation.READ_CITATION,
                    target=replace(
                        facts.target,
                        object_id=source.id,
                        kind=TargetKind.CITATION,
                        resource_id=source.resource_id,
                    ),
                    source=dependency,
                    context=None,
                ),
            )
            chunk = (
                await uow.session.get(KnowledgeChunk, source.chunk_id) if source.chunk_id else None
            )
            if chunk is not None and chunk.index_id != source.index_id:
                raise ConflictError("引用片段归属无效")
            revision = (
                await uow.session.get(ResourceVersion, source.revision_id)
                if source.revision_id
                else None
            )
            # public 引用标题取公开投影；不能读取未选择公开的原稿标题。
            publication = (
                await uow.repositories.publications.current_for_resource(source.resource_id)
                if source.publication_id and source.resource_id
                else None
            )
            title = (
                publication.public_title
                if publication
                else (source.web_title or (revision.title if revision else None))
            )
            return {
                "id": source.id,
                "title": title or "引用资料",
                "excerpt": chunk.content_text if chunk else source.excerpt or "",
                "publication_id": source.publication_id,
                "url": source.web_url if source.web_url else None,
                "locator": source.locator,
                "page": source.locator.get("page") if source.locator else None,
            }

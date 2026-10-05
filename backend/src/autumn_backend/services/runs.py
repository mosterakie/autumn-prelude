"""E2 问答受理：当前权限、幂等、限制、输入与作业在同一短事务完成。"""

import hashlib
import hmac
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from autumn_backend.config import Settings, get_settings
from autumn_backend.db.enums import RunEventType, RunStatus
from autumn_backend.db.models import Conversation, QuotaBucket, Run
from autumn_backend.db.session import UnitOfWork, UnitOfWorkFactory
from autumn_backend.errors import (
    ConcurrencyLimitError,
    ConflictError,
    IdempotencyConflictError,
    InvalidInputError,
)
from autumn_backend.jobs.queue import enqueue
from autumn_backend.policies import ActorContext, ActorRole
from autumn_backend.policies.facts import (
    AuthenticationFacts,
    ConversationMode,
    Operation,
    PolicyFacts,
    PublicationFacts,
    ResourceFacts,
    SearchMode,
    TargetFacts,
    TargetKind,
)
from autumn_backend.repositories.constraints import ActiveRunConflictError
from autumn_backend.repositories.jobs import JobSpec
from autumn_backend.services.access import lock_authentication, require_allowed
from autumn_backend.services.ai_limits import daily_window, limits_from_snapshot, read_ai_limits


@dataclass(frozen=True, slots=True, kw_only=True)
class AcceptRunCommand:
    conversation_id: UUID
    client_message_id: UUID
    idempotency_key: str
    message: str
    resource_ids: tuple[UUID, ...] = ()
    search_mode: SearchMode = SearchMode.SITE

    def __post_init__(self) -> None:
        if not isinstance(self.conversation_id, UUID) or not isinstance(
            self.client_message_id, UUID
        ):
            raise InvalidInputError("会话与消息 ID 必须为 UUID")
        if (
            not isinstance(self.idempotency_key, str)
            or not self.idempotency_key.strip()
            or len(self.idempotency_key) > 128
        ):
            raise InvalidInputError("幂等键不能为空或超过 128 字符")
        if (
            not isinstance(self.message, str)
            or not self.message.strip()
            or len(self.message) > 32000
        ):
            raise InvalidInputError("消息必须为非空文本且不超过 32000 字符")
        if (
            not isinstance(self.resource_ids, tuple)
            or len(self.resource_ids) > 100
            or any(not isinstance(value, UUID) for value in self.resource_ids)
        ):
            raise InvalidInputError("资源范围必须为不超过 100 个 UUID 的不可变序列")
        if not isinstance(self.search_mode, SearchMode):
            raise InvalidInputError("搜索模式无效")

    @property
    def body(self) -> str:
        return self.message.replace("\r\n", "\n").replace("\r", "\n")

    @property
    def selected_resources(self) -> tuple[UUID, ...]:
        return tuple(sorted(set(self.resource_ids)))

    def digest(self, mode: ConversationMode) -> str:
        parameters = {
            "schema_version": 1,
            "conversation_id": str(self.conversation_id),
            "client_message_id": str(self.client_message_id),
            "message": self.body,
            "mode": mode.value,
            "resource_ids": [str(value) for value in self.selected_resources],
            "search_mode": self.search_mode.value,
        }
        return hashlib.sha256(
            json.dumps(
                parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class QuotaView:
    timezone: str
    window_start: datetime
    window_end: datetime
    daily_limit: int
    used: int
    reserved: int
    remaining: int
    cooldown_until: datetime | None
    next_reset_at: datetime
    server_time: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class AskAccepted:
    run_id: UUID
    conversation_id: UUID
    input_message_id: UUID
    status: RunStatus
    events_url: str
    quota: QuotaView


class RunService:
    def __init__(self, uows: UnitOfWorkFactory, *, settings: Settings | None = None) -> None:
        self._uows = uows
        self._settings = settings if settings is not None else get_settings()

    async def accept_run(self, actor: ActorContext, command: AcceptRunCommand) -> AskAccepted:
        async with self._uows() as uow:
            auth = await lock_authentication(uow, actor)
            conversation = None
            if actor.user_id is not None:
                conversation = await uow.repositories.conversations.for_user_for_update(
                    command.conversation_id, actor.user_id
                )
            facts = await self._facts(uow, auth, conversation, command.search_mode)
            require_allowed(
                actor,
                replace(
                    facts,
                    operation=Operation.CREATE_CONVERSATION,
                    target=None,
                    requested_mode=ConversationMode.PUBLIC,
                ),
            )
            # 同一当前身份/归属/邮箱/mode/web 判定，仅暂时排除“新问答”冷却。
            replay_facts = replace(
                facts, authentication=replace(auth, ai_cooldown_until=None) if auth else None
            )
            require_allowed(actor, replay_facts)
            assert conversation is not None and auth is not None and actor.user_id is not None
            scopes = await self._check_resources(
                uow, actor, replay_facts, command.selected_resources
            )
            digest = command.digest(ConversationMode(conversation.mode.value))
            existing = await uow.repositories.runs.by_idempotency_key(
                actor.user_id, command.idempotency_key
            )
            if existing is not None:
                if existing.request_hash != digest or existing.conversation_id != conversation.id:
                    raise IdempotencyConflictError("幂等键已用于不同请求")
                result = await self._result(uow, existing, auth)
                self._require_current(actor, replay_facts, scopes, result.quota.server_time)
                return result

            # resource 锁等待可能跨过会话过期/step-up 截止，重新取 DB 实际时间。
            facts = replace(facts, now=await uow.repositories.runs.database_time())
            require_allowed(actor, facts)
            if (
                await uow.repositories.messages.by_client_id(
                    conversation.id, command.client_message_id
                )
                is not None
            ):
                raise IdempotencyConflictError("消息标识已用于另一请求")
            if await uow.repositories.runs.active_for_conversation(conversation.id) is not None:
                raise ActiveRunConflictError("当前会话已有未结束的运行")
            policy = read_ai_limits(await uow.repositories.settings.get("ai_limits"))
            limits = policy.for_role(auth.role)
            if await uow.repositories.runs.executing_count(actor.user_id) >= limits.concurrency:
                raise ConcurrencyLimitError("账号当前执行任务已达到上限")
            minute = facts.now.astimezone(UTC).replace(second=0, microsecond=0)
            # 域隔离 HMAC；DB 短期桶不保存用户 ID/邮箱，秘密不进快照或作业。
            scope_hash = hmac.new(
                self._settings.session_secret.get_secret_value().encode(),
                f"rate:ai.accept:user:{actor.user_id}".encode(),
                hashlib.sha256,
            ).hexdigest()
            await uow.repositories.rate_limits.consume(
                scope_hash=scope_hash,
                policy_key="ai.accept",
                window_start=minute,
                window_end=minute + timedelta(minutes=1),
                limit=limits.per_minute,
            )
            start, end = daily_window(facts.now, self._settings.quota_timezone)
            web_enabled = command.search_mode is SearchMode.WEB or (
                command.search_mode is SearchMode.AUTO
                and actor.role is ActorRole.OWNER
                and actor.step_up_expires_at is not None
                and actor.step_up_expires_at > facts.now
                and auth.step_up_expires_at is not None
                and auth.step_up_expires_at > facts.now
            )
            creation = await uow.repositories.runs.create_idempotent(
                user_id=actor.user_id,
                conversation_id=conversation.id,
                idempotency_key=command.idempotency_key,
                request_hash=digest,
                scope_epoch=await uow.repositories.settings.get_acl_epoch(),
                auth_session_id=auth.session_id,
                config_snapshot={
                    "schema_version": 1,
                    "mode": conversation.mode.value,
                    "search_mode": command.search_mode.value,
                    "web_enabled": web_enabled,
                    "resource_ids": [str(value) for value in command.selected_resources],
                    "ai_limits_version": policy.version,
                    "ai_limits": limits.snapshot(),
                    "quota_timezone": self._settings.quota_timezone,
                },
            )
            # user 锁已串行化同账号受理；保留 Repository 的唯一约束防线。
            if not creation.created:
                raise ConflictError("受理身份已变化，请重试")
            run = creation.record
            bucket = await uow.repositories.quota_buckets.get_or_create_for_update(
                actor.user_id,
                start,
                end,
                timezone=self._settings.quota_timezone,
                policy_version=policy.version,
            )
            await uow.repositories.quota_reservations.reserve(
                run_id=run.id,
                bucket_id=bucket.id,
                user_id=actor.user_id,
                current_limit=limits.daily_limit,
            )
            message = await uow.repositories.messages.append_user(
                conversation_id=conversation.id,
                run_id=run.id,
                client_message_id=command.client_message_id,
                body=command.body,
            )
            run = await uow.repositories.runs.attach_input_message(
                run.id, conversation.id, message.id
            )
            await enqueue(
                uow,
                JobSpec(
                    kind="run.dispatch",
                    idempotency_key=f"run.dispatch:{run.id}",
                    payload={
                        "run_id": str(run.id),
                        "execution_generation": run.execution_generation,
                    },
                    actor_id=actor.user_id,
                    run_id=run.id,
                    auth_session_id=auth.session_id,
                ),
            )
            await uow.repositories.run_events.emit(
                run.id, RunEventType.RUN_STATUS, {"status": run.status.value}
            )
            # 身份记录被锁定，但时间继续流动；失效时整个受理回滚。
            result = await self._result(uow, run, auth)
            self._require_current(actor, facts, scopes, result.quota.server_time)
            return result

    @staticmethod
    def _require_current(
        actor: ActorContext,
        facts: PolicyFacts,
        scopes: tuple[PolicyFacts, ...],
        now: datetime,
    ) -> None:
        require_allowed(actor, replace(facts, now=now))
        # 用户/会话/资源记录仍持锁；到期时间仍需在最后一次 DB 时钟下复核。
        for scope in scopes:
            require_allowed(actor, replace(scope, now=now))

    @staticmethod
    async def _facts(
        uow: UnitOfWork,
        auth: AuthenticationFacts | None,
        conversation: Conversation | None,
        search_mode: SearchMode,
    ) -> PolicyFacts:
        return PolicyFacts(
            operation=Operation.ASK,
            authentication=auth,
            search_mode=search_mode,
            now=await uow.repositories.runs.database_time(),
            current_scope_epoch=await uow.repositories.settings.get_acl_epoch(),
            target=TargetFacts(
                object_id=conversation.id,
                owner_id=conversation.user_id,
                kind=TargetKind.CONVERSATION,
                mode=ConversationMode(conversation.mode.value),
                is_deleted=conversation.deleted_at is not None,
                expires_at=conversation.expires_at,
            )
            if conversation
            else None,
        )

    @staticmethod
    async def _check_resources(
        uow: UnitOfWork,
        actor: ActorContext,
        facts: PolicyFacts,
        resource_ids: tuple[UUID, ...],
    ) -> tuple[PolicyFacts, ...]:
        assert facts.target is not None
        operation = (
            Operation.SEARCH_PRIVATE_KNOWLEDGE
            if facts.target.mode is ConversationMode.OWNER
            else Operation.SEARCH_PUBLIC_KNOWLEDGE
        )
        scopes = []
        for resource_id in resource_ids:
            # 固定 UUID 顺序加锁，保证当前范围校验到受理提交之间不被撤回/删除。
            resource = await uow.repositories.resources.get_for_update(resource_id)
            publication = (
                await uow.repositories.publications.current_for_resource(resource_id)
                if resource
                else None
            )
            snapshot = None
            if resource is not None and resource.current_revision_id is not None:
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
                    if publication
                    else None,
                )
            scope = replace(
                facts,
                operation=operation,
                target=None,
                resource=snapshot,
                requested_resource_id=resource_id,
                now=await uow.repositories.runs.database_time(),
            )
            require_allowed(actor, scope)
            scopes.append(scope)
        return tuple(scopes)

    @staticmethod
    async def _result(uow: UnitOfWork, run: Run, auth: AuthenticationFacts) -> AskAccepted:
        reservation = await uow.repositories.quota_reservations.for_run(run.id)
        if reservation is None or run.input_message_id is None:
            raise ConflictError("运行受理记录不完整")
        bucket: QuotaBucket = await uow.repositories.quota_buckets.get_or_raise(
            reservation.bucket_id
        )
        limits = limits_from_snapshot(run.config_snapshot.get("ai_limits"))
        return AskAccepted(
            run_id=run.id,
            conversation_id=run.conversation_id,
            input_message_id=run.input_message_id,
            status=run.status,
            events_url=f"/api/runs/{run.id}/events",
            quota=QuotaView(
                timezone=bucket.timezone,
                window_start=bucket.window_start,
                window_end=bucket.window_end,
                daily_limit=limits.daily_limit,
                used=bucket.used,
                reserved=bucket.reserved,
                remaining=max(0, limits.daily_limit - bucket.used - bucket.reserved),
                cooldown_until=auth.ai_cooldown_until,
                next_reset_at=bucket.window_end,
                server_time=await uow.repositories.runs.database_time(),
            ),
        )

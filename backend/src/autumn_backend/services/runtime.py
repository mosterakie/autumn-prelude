"""Agent 的数据库身份、任务票据和永久累计预算；不依赖 agent 包。"""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from autumn_backend.config import Settings, get_settings
from autumn_backend.db.enums import MessageRole, MessageStatus, RunStatus
from autumn_backend.db.models import Message, Run
from autumn_backend.db.session import UnitOfWork, UnitOfWorkFactory
from autumn_backend.errors import ConflictError, DomainError, NotFoundError, OptimisticLockError
from autumn_backend.policies import ActorContext, ActorRole
from autumn_backend.policies.facts import Operation
from autumn_backend.services.context import run_facts


class BudgetExceededError(DomainError):
    code = "agent_budget_exceeded"


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    model_calls: int = Field(default=4, ge=1, le=20)
    tool_calls: int = Field(default=4, ge=0, le=20)
    input_units: int = Field(default=120000, ge=1000, le=1000000)
    output_tokens: int = Field(default=16384, ge=256, le=65536)
    output_per_call: int = Field(default=4096, ge=128, le=16384)
    elapsed_ms: int = Field(default=120000, ge=1000, le=600000)


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    model_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    input_units: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    elapsed_ms: int = Field(default=0, ge=0)


class RuntimeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal[1] = 1
    limits: Limits = Field(default_factory=Limits)
    usage: Usage = Field(default_factory=Usage)
    node: Literal["authorize", "context", "plan", "tool", "wait", "reply"] = "authorize"


@dataclass(frozen=True, slots=True)
class RuntimeTicket:
    actor: ActorContext
    job_id: UUID
    token: UUID
    run_id: UUID
    generation: int
    thread_id: str
    mode: str
    search_mode: str
    resource_ids: tuple[UUID, ...]
    request_id: UUID
    request_text: str
    history_ids: tuple[UUID, ...]
    record: RuntimeRecord


class RuntimeService:
    def __init__(self, uows: UnitOfWorkFactory, *, settings: Settings | None = None) -> None:
        self.uows = uows
        self.settings = settings or get_settings()

    def _record(self, run: Run) -> RuntimeRecord:
        try:
            raw = run.config_snapshot.get("agent_runtime")
            return (
                RuntimeRecord.model_validate(raw)
                if raw is not None
                else RuntimeRecord(limits=Limits(**self.settings.agent_limits))
            )
        except ValidationError as error:
            raise ConflictError("运行预算损坏") from error

    async def _locked(
        self,
        uow: UnitOfWork,
        job_id: UUID,
        token: UUID,
        *,
        generation: int | None = None,
        context: bool = False,
    ) -> tuple[ActorContext, Run, str]:
        probe = await uow.repositories.jobs.get_or_raise(job_id)
        if (
            probe.kind not in ("run.dispatch", "run.resume")
            or probe.actor_id is None
            or probe.auth_session_id is None
            or probe.run_id is None
        ):
            raise NotFoundError("运行任务不存在")
        user = await uow.repositories.users.get_for_update_or_raise(probe.actor_id)
        session = await uow.repositories.auth_sessions.for_user_for_update(
            probe.auth_session_id, user.id
        )
        if session is None:
            raise NotFoundError("运行会话不存在")
        actor = ActorContext(
            user_id=user.id,
            role=ActorRole(user.role.value),
            auth_session_id=session.id,
            step_up_expires_at=session.step_up_expires_at,
            capabilities=frozenset(),
            scope_epoch=0,
        )
        actor, facts, run = await run_facts(
            uow,
            actor,
            probe.run_id,
            Operation.CONTINUE_RUN if context else Operation.READ_RUN,
            require_context=context,
            expected_generation=generation,
        )
        job = await uow.repositories.jobs.require_lease(job_id, token)
        if job.auth_session_id != run.auth_session_id or run.auth_session_id != session.id:
            raise ConflictError("任务授权会话已变化")
        payload = job.payload or {}
        if (
            type(payload.get("execution_generation")) is not int
            or payload["execution_generation"] != run.execution_generation
            or (generation is not None and generation != run.execution_generation)
        ):
            raise OptimisticLockError("任务代际已失效")
        assert facts.target is not None and facts.target.mode is not None
        return actor, run, facts.target.mode.value

    async def open(self, job_id: UUID, token: UUID) -> RuntimeTicket:
        async with self.uows() as uow:
            actor, run, mode = await self._locked(uow, job_id, token)
            if run.status not in (RunStatus.QUEUED, RunStatus.RUNNING):
                raise ConflictError("运行已等待或结束")
            messages = list(
                (
                    await uow.session.scalars(
                        select(Message)
                        .where(
                            Message.run_id == run.id,
                            Message.conversation_id == run.conversation_id,
                            Message.role == MessageRole.USER,
                            Message.status == MessageStatus.COMPLETE,
                        )
                        .order_by(Message.seq)
                    )
                ).all()
            )
            if not messages or run.input_message_id not in {item.id for item in messages}:
                raise ConflictError("运行缺少真实输入消息")
            history = tuple(
                (
                    await uow.session.scalars(
                        select(Message.id)
                        .where(
                            Message.conversation_id == run.conversation_id,
                            Message.run_id != run.id,
                            Message.status == MessageStatus.COMPLETE,
                        )
                        .order_by(Message.seq.desc())
                        .limit(12)
                    )
                ).all()
            )[::-1]
            record = self._record(run)
            thread_id = f"conversation:{run.conversation_id}:mode:{mode}"
            if run.checkpoint_thread_id != thread_id or "agent_runtime" not in run.config_snapshot:
                run.checkpoint_thread_id = thread_id
                run.config_snapshot = {**run.config_snapshot, "agent_runtime": record.model_dump()}
                run.version += 1
                await uow.session.flush()
            try:
                ids = tuple(UUID(value) for value in run.config_snapshot.get("resource_ids", []))
            except (ValueError, TypeError, AttributeError) as error:
                raise ConflictError("资源范围损坏") from error
            # 原问题与持久补充答案均来自本 Run 的实际用户消息，而非图/checkpoint 载荷。
            return RuntimeTicket(
                actor,
                job_id,
                token,
                run.id,
                run.execution_generation,
                thread_id,
                mode,
                str(run.config_snapshot.get("search_mode", "site")),
                ids,
                messages[-1].id,
                "\n\n".join(item.body_text for item in messages),
                history,
                record,
            )

    async def guard(self, ticket: RuntimeTicket, *, context: bool = True) -> None:
        async with self.uows() as uow:
            await self._locked(
                uow, ticket.job_id, ticket.token, generation=ticket.generation, context=context
            )

    async def reserve(
        self,
        ticket: RuntimeTicket,
        *,
        kind: Literal["model", "tool", "time"],
        input_units: int = 0,
        elapsed_ms: int = 0,
    ) -> RuntimeRecord:
        if (
            type(input_units) is not int
            or type(elapsed_ms) is not int
            or input_units < 0
            or elapsed_ms < 0
        ):
            raise ConflictError("预算输入无效")
        async with self.uows() as uow:
            _, run, _ = await self._locked(
                uow, ticket.job_id, ticket.token, generation=ticket.generation, context=True
            )
            record = self._record(run)
            usage = record.usage.model_copy(
                update={
                    "model_calls": record.usage.model_calls + int(kind == "model"),
                    "tool_calls": record.usage.tool_calls + int(kind == "tool"),
                    "input_units": record.usage.input_units + input_units,
                    # 供应商无 usage 时也有上限；每轮预占其最大输出，不按未知值当零。
                    "output_tokens": record.usage.output_tokens
                    + (record.limits.output_per_call if kind == "model" else 0),
                    "elapsed_ms": record.usage.elapsed_ms + elapsed_ms,
                }
            )
            if any(
                getattr(usage, name) > getattr(record.limits, name)
                for name in (
                    "model_calls",
                    "tool_calls",
                    "input_units",
                    "output_tokens",
                    "elapsed_ms",
                )
            ):
                raise BudgetExceededError("运行预算已用尽")
            record = record.model_copy(
                update={"usage": usage, "node": "tool" if kind == "tool" else "plan"}
            )
            run.config_snapshot = {**run.config_snapshot, "agent_runtime": record.model_dump()}
            run.version += 1
            await uow.session.flush()
            return record

    async def checkpoint(
        self,
        ticket: RuntimeTicket,
        node: Literal["authorize", "context", "plan", "tool", "wait", "reply"],
    ) -> None:
        async with self.uows() as uow:
            _, run, _ = await self._locked(
                uow, ticket.job_id, ticket.token, generation=ticket.generation, context=True
            )
            record = self._record(run).model_copy(update={"node": node})
            run.config_snapshot = {**run.config_snapshot, "agent_runtime": record.model_dump()}
            run.version += 1
            await uow.session.flush()

"""E8 结果提交闸门；H 运行器负责调度/heartbeat/失败分类，不得绕过这里写正式结果。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID, uuid5

from autumn_backend.db.enums import ActionStatus, ProviderCallStatus, RunEventType, RunStatus
from autumn_backend.db.models import Action, Run
from autumn_backend.db.session import UnitOfWork, UnitOfWorkFactory
from autumn_backend.errors import ConflictError, NotFoundError, OptimisticLockError
from autumn_backend.io_boundary import require_outside_uow
from autumn_backend.jobs.queue import enqueue
from autumn_backend.policies import ActorContext
from autumn_backend.policies.facts import Operation
from autumn_backend.repositories.jobs import JobSpec
from autumn_backend.repositories.provider_calls import external_idempotency_key
from autumn_backend.services.actions import ActionService, Command, stored_command
from autumn_backend.services.context import advance_context_generation, run_facts
from autumn_backend.services.quota import QuotaService
from autumn_backend.services.runtime import spend_time_in_uow


@dataclass(frozen=True, slots=True)
class ExecutionFence:
    job_id: UUID
    lease_token: UUID
    run_id: UUID
    generation: int
    run_version: int
    call_id: UUID
    external_idempotency_key: str


@dataclass(frozen=True, slots=True)
class ModelResult:
    text: str
    external_request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class ModelCall(Protocol):
    async def complete(self, *, external_idempotency_key: str) -> ModelResult: ...


@dataclass(frozen=True, slots=True)
class CommittedReply:
    run_id: UUID
    message_id: UUID
    generation: int


ActionWriter = Callable[[UnitOfWork, Action, Command], Awaitable[dict[str, Any]]]


class ExecutionService:
    def __init__(self, uows: UnitOfWorkFactory) -> None:
        self._uows = uows

    async def _run_job(
        self,
        uow: UnitOfWork,
        actor: ActorContext,
        job_id: UUID,
        token: UUID,
        *,
        fence: ExecutionFence | None = None,
        after_result: bool = False,
    ) -> Run:
        probe = await uow.repositories.jobs.get(job_id)
        if (
            probe is None
            or probe.actor_id != actor.user_id
            or probe.run_id is None
            or probe.kind not in ("run.dispatch", "run.resume")
        ):
            raise NotFoundError("运行任务不存在")
        _, _, run = await run_facts(
            uow,
            actor,
            probe.run_id,
            Operation.EMIT_RUN_OUTPUT if after_result else Operation.CONTINUE_RUN,
            expected_version=fence.run_version if fence is not None and not after_result else None,
            expected_generation=fence.generation if fence is not None else None,
        )
        job = await uow.repositories.jobs.require_lease(job_id, token)
        if (
            job.auth_session_id != actor.auth_session_id
            or run.auth_session_id != actor.auth_session_id
        ):
            raise ConflictError("任务必须绑定当前授权会话")
        payload = job.payload
        if (
            not isinstance(payload, dict)
            or type(payload.get("execution_generation")) is not int
            or payload["execution_generation"] != run.execution_generation
        ):
            raise OptimisticLockError("任务所属执行代际已失效")
        if fence is not None and (
            fence.run_id != run.id or fence.generation != run.execution_generation
        ):
            raise OptimisticLockError("旧执行代际已失效")
        if not after_result and run.status not in (RunStatus.QUEUED, RunStatus.RUNNING):
            raise ConflictError("运行已进入等待或终态")
        return run

    async def start_model_call(
        self, actor: ActorContext, job_id: UUID, token: UUID, call_id: UUID
    ) -> ExecutionFence:
        async with self._uows() as uow:
            run = await self._run_job(uow, actor, job_id, token)
            call = await uow.repositories.provider_calls.get_for_update_or_raise(call_id)
            if call.run_id != run.id or call.job_id != job_id:
                raise NotFoundError("模型调用不存在")
            if call.status is not ProviderCallStatus.PREPARED:
                raise ConflictError("该调用已派发；须先对账，不能自动重复网络请求")
            if run.status is RunStatus.QUEUED:
                run.status = RunStatus.RUNNING
                run.started_at = run.started_at or await uow.repositories.users.database_time()
                run.version += 1
                await uow.session.flush()
            await QuotaService(self._uows).dispatch_model_in_uow(uow, run.id, call.id)
            # 以最后一次数据库时间复核 lease / 完整来源；到期回滚派发和扣次。
            await self._run_job(uow, actor, job_id, token)
            return ExecutionFence(
                job_id,
                token,
                run.id,
                run.execution_generation,
                run.version,
                call.id,
                external_idempotency_key(call.logical_call_key),
            )

    async def commit_model_result(
        self,
        actor: ActorContext,
        fence: ExecutionFence,
        result: ModelResult,
        *,
        elapsed_ms: int | None = None,
    ) -> CommittedReply:
        async with self._uows() as uow:
            run = await self._run_job(uow, actor, fence.job_id, fence.lease_token, fence=fence)
            call = await uow.repositories.provider_calls.get_for_update_or_raise(fence.call_id)
            if (
                call.run_id != run.id
                or call.job_id != fence.job_id
                or call.status is not ProviderCallStatus.DISPATCHED
            ):
                raise ConflictError("模型结果与派发记录不匹配")
            if elapsed_ms is not None:
                spend_time_in_uow(run, elapsed_ms)
            message = await uow.repositories.messages.append_assistant_complete(
                conversation_id=run.conversation_id,
                run_id=run.id,
                client_message_id=uuid5(run.id, f"final:{fence.generation}"),
                body=result.text,
            )
            await uow.repositories.provider_calls.settle_succeeded(
                call.id,
                external_request_id=result.external_request_id,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )
            run.current_message_id = message.id
            run.status = RunStatus.SUCCEEDED
            run.finished_at = await uow.repositories.users.database_time()
            run.version += 1
            await uow.session.flush()
            await uow.repositories.run_events.emit(
                run.id,
                RunEventType.MESSAGE_SNAPSHOT,
                {
                    "message_id": str(message.id),
                    "content_version": message.content_version,
                    "execution_generation": fence.generation,
                },
            )
            await uow.repositories.run_events.emit(
                run.id, RunEventType.DONE, {"status": run.status.value}
            )
            await self._run_job(
                uow, actor, fence.job_id, fence.lease_token, fence=fence, after_result=True
            )
            await uow.repositories.jobs.finish(
                fence.job_id,
                fence.lease_token,
                result={
                    "run_id": str(run.id),
                    "message_id": str(message.id),
                    "execution_generation": fence.generation,
                },
            )
            return CommittedReply(run.id, message.id, fence.generation)

    async def commit_model_step(
        self, actor: ActorContext, fence: ExecutionFence, result: ModelResult, *, elapsed_ms: int
    ) -> None:
        """已完成的规划调用只结算账本，不把工具协议写成正式回复或结束 Run。"""
        async with self._uows() as uow:
            run = await self._run_job(uow, actor, fence.job_id, fence.lease_token, fence=fence)
            call = await uow.repositories.provider_calls.get_for_update_or_raise(fence.call_id)
            if (
                call.run_id != run.id
                or call.job_id != fence.job_id
                or call.status is not ProviderCallStatus.DISPATCHED
            ):
                raise ConflictError("规划结果与派发记录不匹配")
            spend_time_in_uow(run, elapsed_ms)
            await uow.repositories.provider_calls.settle_succeeded(
                call.id,
                external_request_id=result.external_request_id,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )
            run.version += 1
            await uow.session.flush()
            await self._run_job(
                uow, actor, fence.job_id, fence.lease_token, fence=fence, after_result=True
            )

    async def call_model(
        self, actor: ActorContext, job_id: UUID, token: UUID, call_id: UUID, model: ModelCall
    ) -> CommittedReply:
        fence = await self.start_model_call(actor, job_id, token, call_id)
        require_outside_uow()
        result = await model.complete(external_idempotency_key=fence.external_idempotency_key)
        require_outside_uow()
        return await self.commit_model_result(actor, fence, result)

    async def commit_action_result(
        self, actor: ActorContext, job_id: UUID, token: UUID, writer: ActionWriter
    ) -> UUID:
        """H 的可信数据库 handler 回调；不对 HTTP/模型暴露，也不接受外部 I/O 回调。"""
        async with self._uows() as uow:
            probe = await uow.repositories.jobs.get(job_id)
            if (
                probe is None
                or probe.actor_id != actor.user_id
                or probe.kind != "action.execute"
                or not isinstance(probe.payload, dict)
            ):
                raise NotFoundError("动作任务不存在")
            try:
                action_id = UUID(probe.payload["action_id"])
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                raise ConflictError("动作任务载荷无效") from error
            service = ActionService(self._uows)
            action = await service._action(uow, actor, action_id, Operation.EXECUTE_ACTION)
            command = stored_command(action)
            await service._target(uow, actor, command)
            job = await uow.repositories.jobs.require_lease(job_id, token)
            if (
                job.run_id != action.run_id
                or job.resource_id != action.target_resource_id
                or job.auth_session_id != actor.auth_session_id
                or action.auth_session_id != actor.auth_session_id
                or probe.payload.get("parameters_hash") != action.parameters_hash
                or action.status is not ActionStatus.READY
                or action.expires_at <= await uow.repositories.users.database_time()
            ):
                raise ConflictError("动作状态、目标、摘要或授权会话已失效")
            generation = None
            if action.run_id is not None:
                _, _, run = await run_facts(uow, actor, action.run_id, Operation.RESUME_RUN)
                generation = run.execution_generation
                if (
                    type(probe.payload.get("execution_generation")) is not int
                    or probe.payload["execution_generation"] != generation
                    or run.status not in (RunStatus.QUEUED, RunStatus.RUNNING)
                ):
                    raise OptimisticLockError("动作执行代际已失效")
            data = await writer(uow, action, command)
            await uow.session.flush()
            if not isinstance(data, dict) or not data.keys() <= {
                "resource_id",
                "publication_id",
                "memory_id",
                "settings_key",
                "resource_version",
                "acl_version",
                "scope_epoch",
                "changed",
            }:
                raise ConflictError("动作结果只允许保存对象身份与状态元数据")
            for key, value in data.items():
                if key.endswith("_id"):
                    try:
                        UUID(value)
                    except (ValueError, TypeError, AttributeError) as error:
                        raise ConflictError("动作结果对象身份无效") from error
                elif key == "changed":
                    if type(value) is not bool:
                        raise ConflictError("动作结果状态无效")
                elif key == "settings_key":
                    if value != "ai_limits":
                        raise ConflictError("动作结果设置键无效")
                elif type(value) is not int or value < 0:
                    raise ConflictError("动作结果版本无效")
            # 该操作可能有意修改 ACL/删除目标；末端检查执行身份、归属与代际，禁止复用旧上下文。
            await service._action(uow, actor, action_id, Operation.EXECUTE_ACTION)
            if action.run_id is not None:
                _, _, run = await run_facts(
                    uow, actor, action.run_id, Operation.READ_RUN, require_context=False
                )
                if run.execution_generation != generation:
                    raise OptimisticLockError("动作执行代际已变化")
            await uow.repositories.jobs.require_lease(job_id, token)
            action = await uow.repositories.actions.succeed_confirmed(action.id, data)
            await service._audit(uow, actor, action, "succeeded")
            if action.run_id is not None:
                await advance_context_generation(uow, run, preserve_sources=False)
                run.status = RunStatus.QUEUED
                run.version += 1
                await uow.session.flush()
                await enqueue(
                    uow,
                    JobSpec(
                        kind="run.resume",
                        idempotency_key=f"run.resume:action:{action.id}",
                        actor_id=actor.user_id,
                        auth_session_id=actor.auth_session_id,
                        run_id=run.id,
                        payload={
                            "schema_version": 1,
                            "action_id": str(action.id),
                            "execution_generation": run.execution_generation,
                            "rebuild_context": True,
                        },
                    ),
                )
                await uow.repositories.run_events.emit(
                    run.id,
                    RunEventType.ACTION_SUCCEEDED,
                    {"action_id": str(action.id), "execution_generation": run.execution_generation},
                )
                await uow.repositories.run_events.emit(
                    run.id,
                    RunEventType.SCOPE_CHANGED,
                    {
                        "execution_generation": run.execution_generation,
                        "scope_epoch": await uow.repositories.settings.get_acl_epoch(),
                        "rebuild_context": True,
                    },
                )
            await uow.repositories.jobs.finish(job_id, token, result={"action_id": str(action.id)})
            return action.id

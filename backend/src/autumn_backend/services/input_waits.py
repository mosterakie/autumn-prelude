"""版本化持久补充请求；Run 锁内原子消费，同答案重试不重复消息/任务/额度。"""

import hashlib
from datetime import timedelta
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, ValidationError, model_validator

from autumn_backend.db.enums import RunEventType, RunStatus
from autumn_backend.db.session import UnitOfWorkFactory
from autumn_backend.errors import (
    ConcurrencyLimitError,
    ConflictError,
    IdempotencyConflictError,
    InvalidInputError,
)
from autumn_backend.jobs.queue import enqueue
from autumn_backend.policies import ActorContext
from autumn_backend.policies.facts import Operation
from autumn_backend.repositories.jobs import JobSpec
from autumn_backend.services.actions import Payload
from autumn_backend.services.ai_limits import read_ai_limits
from autumn_backend.services.context import (
    TaskFence,
    advance_context_generation,
    require_task,
    run_facts,
)


class InputRequest(Payload):
    schema_version: Literal[1] = 1
    id: UUID
    prompt: str = Field(min_length=1, max_length=4000)
    options: tuple[str, ...] = Field(default=(), max_length=10)
    expires_at: AwareDatetime
    consumed_at: AwareDatetime | None = None
    answer_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    answer_message_id: UUID | None = None

    @model_validator(mode="after")
    def valid_request(self) -> "InputRequest":
        if (
            not self.prompt.strip()
            or any(not option.strip() or len(option) > 500 for option in self.options)
            or len(self.options) != len(set(self.options))
        ):
            raise ValueError("补充请求文本或选项无效")
        if any(
            value is not None
            for value in (self.consumed_at, self.answer_hash, self.answer_message_id)
        ) != all(
            value is not None
            for value in (self.consumed_at, self.answer_hash, self.answer_message_id)
        ):
            raise ValueError("消费标记必须一起写入")
        return self


def load_input(value: object) -> InputRequest:
    import json

    try:
        return InputRequest.model_validate_json(json.dumps(value))
    except ValidationError as error:
        raise ConflictError("持久补充请求损坏或版本不支持") from error


class InputWaitService:
    def __init__(self, uows: UnitOfWorkFactory) -> None:
        self._uows = uows

    async def request(
        self,
        actor: ActorContext,
        run_id: UUID,
        *,
        wait_id: UUID,
        prompt: str,
        options: tuple[str, ...] = (),
        fence: TaskFence | None = None,
    ) -> InputRequest:
        async with self._uows() as uow:
            _, facts, run = await run_facts(uow, actor, run_id, Operation.CONTINUE_RUN)
            if fence is not None:
                await require_task(uow, actor, run, fence)
            if run.input_request is not None:
                previous = load_input(run.input_request)
                if previous.id == wait_id:
                    if (previous.prompt, previous.options) != (prompt, options):
                        raise IdempotencyConflictError("等待标识已用于另一补充请求")
                    return previous
                if previous.consumed_at is None:
                    raise ConflictError("前一补充请求尚未完成")
            if run.status not in (RunStatus.QUEUED, RunStatus.RUNNING):
                raise ConflictError("当前运行不能等待补充")
            try:
                request = InputRequest(
                    id=wait_id,
                    prompt=prompt,
                    options=options,
                    expires_at=facts.now + timedelta(minutes=30),
                )
            except ValidationError as error:
                raise InvalidInputError("补充请求参数无效") from error
            run.input_request = request.model_dump(mode="json")
            run.status = RunStatus.WAITING_INPUT
            run.version += 1
            await advance_context_generation(uow, run, preserve_sources=True)
            await uow.session.flush()
            await uow.repositories.run_events.emit(
                run.id,
                RunEventType.RUN_STATUS,
                {
                    "status": run.status.value,
                    "input_request_id": str(request.id),
                    "schema_version": 1,
                },
            )
            if fence is not None:
                await run_facts(uow, actor, run_id, Operation.CONTINUE_RUN)
                await uow.repositories.jobs.finish(
                    fence.job_id,
                    fence.token,
                    result={"run_id": str(run_id), "input_request_id": str(wait_id)},
                )
            return request

    async def answer(
        self, actor: ActorContext, run_id: UUID, *, wait_id: UUID, answer: str
    ) -> InputRequest:
        if not answer.strip() or len(answer) > 8000:
            raise InvalidInputError("补充答案不能为空或超过 8000 字符")
        body = answer.replace("\r\n", "\n").replace("\r", "\n")
        digest = hashlib.sha256(f"1:{wait_id}:{body}".encode()).hexdigest()
        async with self._uows() as uow:
            _, facts, run = await run_facts(uow, actor, run_id, Operation.RESUME_RUN)
            request = load_input(run.input_request)
            if request.id != wait_id:
                raise ConflictError("等待项已变化")
            if request.consumed_at is not None:
                if request.answer_hash != digest:
                    raise IdempotencyConflictError("等待项已由不同答案消费")
                return request
            if run.status is not RunStatus.WAITING_INPUT or request.expires_at <= facts.now:
                raise ConflictError("等待项已过期或不再等待答案")
            if request.options and body not in request.options:
                raise InvalidInputError("答案必须来自当前选项")
            limits = read_ai_limits(await uow.repositories.settings.get("ai_limits")).for_role(
                actor.role
            )
            if await uow.repositories.runs.executing_count(run.user_id) >= limits.concurrency:
                raise ConcurrencyLimitError("账号执行名额已满")
            message = await uow.repositories.messages.append_user(
                conversation_id=run.conversation_id,
                run_id=run.id,
                client_message_id=wait_id,
                body=body,
            )
            consumed = request.model_copy(
                update={
                    "consumed_at": facts.now,
                    "answer_hash": digest,
                    "answer_message_id": message.id,
                }
            )
            run.input_request = consumed.model_dump(mode="json")
            run.status = RunStatus.QUEUED
            run.auth_session_id = actor.auth_session_id
            run.version += 1
            await advance_context_generation(uow, run, preserve_sources=True)
            await uow.session.flush()
            await enqueue(
                uow,
                JobSpec(
                    kind="run.resume",
                    idempotency_key=f"run.resume:input:{wait_id}",
                    actor_id=actor.user_id,
                    auth_session_id=actor.auth_session_id,
                    run_id=run.id,
                    payload={
                        "schema_version": 1,
                        "input_request_id": str(wait_id),
                        "answer_message_id": str(message.id),
                        "execution_generation": run.execution_generation,
                    },
                ),
            )
            await uow.repositories.run_events.emit(
                run.id,
                RunEventType.RUN_STATUS,
                {"status": run.status.value, "input_request_id": str(wait_id)},
            )
            await run_facts(uow, actor, run_id, Operation.RESUME_RUN)
            return consumed

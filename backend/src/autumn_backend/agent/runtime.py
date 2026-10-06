"""G 最小业务运行器；H 负责领取、heartbeat 与恢复调度，I 提供真实供应商适配器。"""

import asyncio
from contextlib import suppress
from dataclasses import dataclass, replace
from time import monotonic
from typing import Any
from uuid import UUID, uuid5

from langgraph.checkpoint.base import BaseCheckpointSaver

from autumn_backend.agent.context import Context, ContextLoader
from autumn_backend.agent.contracts import InputPlan, ModelDriver, ReplyPlan, ToolPlan, parse_plan
from autumn_backend.agent.graph import GraphState, Node, Nodes, build_graph
from autumn_backend.agent.prompts import build_prompt
from autumn_backend.agent.tools import Tools
from autumn_backend.db.enums import RunStatus
from autumn_backend.errors import (
    ConflictError,
    InvalidInputError,
    LeaseLostError,
    OptimisticLockError,
)
from autumn_backend.io_boundary import require_outside_uow
from autumn_backend.policies import DenialCode
from autumn_backend.services.access import AuthorizationError
from autumn_backend.services.context import TaskFence
from autumn_backend.services.execution import ExecutionFence, ExecutionService, ModelResult
from autumn_backend.services.input_waits import InputWaitService
from autumn_backend.services.runtime import (
    BudgetExceededError,
    ProviderReconciliationRequired,
    RuntimeService,
    RuntimeTicket,
    StopCode,
)


@dataclass(frozen=True, slots=True)
class Outcome:
    run_id: UUID
    generation: int
    status: RunStatus
    message_id: UUID | None = None
    action_id: UUID | None = None
    input_request_id: UUID | None = None
    error_code: str | None = None


class NodeFailure(Exception):
    def __init__(self, code: StopCode) -> None:
        self.code = code
        super().__init__(code)


class AgentRuntime:
    def __init__(
        self,
        service: RuntimeService,
        knowledge: ContextLoader,
        tools: Tools,
        execution: ExecutionService,
        waits: InputWaitService,
        model: ModelDriver,
        *,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
    ) -> None:
        self.service, self.context, self.tools = service, knowledge, tools
        self.execution, self.waits, self.model = execution, waits, model
        self.checkpointer = checkpointer

    async def _generate(
        self, ticket: RuntimeTicket, prompt: str, *, limit: int, key: str
    ) -> ModelResult:
        require_outside_uow()
        task = asyncio.create_task(
            self.model.generate(prompt, max_output_tokens=limit, external_idempotency_key=key)
        )
        try:
            # 供应商请求等待期间也复核权限/lease；失效后停止消费，不等结果写入再判断。
            while not task.done():
                await asyncio.wait({task}, timeout=1)
                await self.service.guard(ticket)
            result = await task
            require_outside_uow()
            await self.service.guard(ticket)
            return result
        finally:
            if not task.done():
                task.cancel()
                with suppress(Exception):
                    async with asyncio.timeout(2):
                        require_outside_uow()
                        await self.model.cancel(external_idempotency_key=key)
                await asyncio.wait({task}, timeout=2)
                # 不等待忽略取消的适配器无限运行；迟到结果没有正式提交入口。
                task.add_done_callback(lambda done: None if done.cancelled() else done.exception())

    async def execute(self, job_id: UUID, token: UUID) -> Outcome:
        ticket: RuntimeTicket
        context = Context((), ())
        plan: ToolPlan | InputPlan | ReplyPlan | None = None
        result: ModelResult | None = None
        fence: ExecutionFence | None = None
        outcome: Outcome | None = None
        accounted = monotonic()
        phase = "authorize"

        def elapsed() -> int:
            return max(0, int((monotonic() - accounted) * 1000))

        def safe(node: Node) -> Node:
            async def run(state: GraphState) -> dict[str, Any]:
                try:
                    return await node(state)
                except LeaseLostError:
                    raise LeaseLostError("任务租约失效") from None
                except OptimisticLockError:
                    raise OptimisticLockError("任务代际或版本失效") from None
                except AuthorizationError as error:
                    raise AuthorizationError(error.decision) from None
                except (BudgetExceededError, TimeoutError):
                    raise NodeFailure("AGENT_BUDGET_EXCEEDED") from None
                except ProviderReconciliationRequired:
                    raise NodeFailure("PROVIDER_OUTCOME_UNKNOWN") from None
                except InvalidInputError:
                    raise NodeFailure("MODEL_OUTPUT_INVALID") from None
                except Exception:
                    # 框架可能持久化异常 repr；供应商异常正文也不能进入 checkpoint。
                    raise NodeFailure(
                        "PROVIDER_ERROR"
                        if phase == "provider"
                        else "ACL_CONTEXT_INVALIDATED"
                        if phase == "context"
                        else "AGENT_ERROR"
                    ) from None

            return run

        async def authorize(state: GraphState) -> dict[str, Any]:
            nonlocal ticket, phase
            if outcome is not None:
                return {"route": "stop"}
            phase = "authorize"
            ticket = await self.service.open(job_id, token)
            return {"run_id": str(ticket.run_id), "generation": ticket.generation, "route": "plan"}

        async def load(state: GraphState) -> dict[str, Any]:
            nonlocal context, phase
            phase = "context"
            context = await self.context.load(ticket)
            await self.service.checkpoint(ticket, "context")
            return {}

        async def decide(state: GraphState) -> dict[str, Any]:
            nonlocal plan, result, fence, accounted, phase
            phase = "plan"
            await self.service.checkpoint(ticket, "plan")
            prompt = build_prompt(
                ticket.request_text,
                history=context.history,
                sources=context.sources,
                tools=self.tools.schemas(ticket),
                completed_actions=context.actions,
            )
            measured = monotonic()
            record = await self.service.reserve(
                ticket, kind="model", input_units=len(prompt.encode("utf-8")), elapsed_ms=elapsed()
            )
            accounted = measured
            call_id = await self.service.prepare_model(
                ticket,
                provider=self.model.provider,
                model=self.model.model,
                ordinal=record.usage.model_calls,
            )
            fence = await self.execution.start_model_call(ticket.actor, job_id, token, call_id)
            phase = "provider"
            result = await self._generate(
                ticket,
                prompt,
                limit=record.limits.output_per_call,
                key=fence.external_idempotency_key,
            )
            if (
                result.output_tokens is not None
                and result.output_tokens > record.limits.output_per_call
            ):
                raise BudgetExceededError("供应商输出超过预占上限")
            phase = "parse"
            plan = parse_plan(result.text)
            if not isinstance(plan, ReplyPlan):
                measured = monotonic()
                await self.execution.commit_model_step(
                    ticket.actor, fence, result, elapsed_ms=elapsed()
                )
                accounted = measured
            return {
                "step": state["step"] + 1,
                "route": "reply"
                if isinstance(plan, ReplyPlan)
                else "wait"
                if isinstance(plan, InputPlan)
                else "tool",
            }

        async def tool(state: GraphState) -> dict[str, Any]:
            nonlocal outcome, phase
            phase = "tool"
            assert isinstance(plan, ToolPlan)
            item = await self.tools.execute(ticket, plan.call, step=state["step"])
            if item.action_id is not None:
                outcome = Outcome(
                    ticket.run_id,
                    ticket.generation + 1,
                    RunStatus.WAITING_APPROVAL,
                    action_id=item.action_id,
                )
                return {"route": "stop"}
            return {"route": "plan"}

        async def wait(state: GraphState) -> dict[str, Any]:
            nonlocal outcome, phase
            phase = "wait"
            assert isinstance(plan, InputPlan)
            await self.service.reserve(ticket, kind="time", elapsed_ms=elapsed())
            item = await self.waits.request(
                ticket.actor,
                ticket.run_id,
                wait_id=uuid5(ticket.run_id, f"input:{ticket.generation}:{state['step']}"),
                prompt=plan.prompt,
                options=plan.options,
                fence=TaskFence(ticket.job_id, ticket.token, ticket.generation),
            )
            outcome = Outcome(
                ticket.run_id,
                ticket.generation + 1,
                RunStatus.WAITING_INPUT,
                input_request_id=item.id,
            )
            return {"route": "stop"}

        async def reply(state: GraphState) -> dict[str, Any]:
            nonlocal outcome, phase
            phase = "reply"
            assert isinstance(plan, ReplyPlan) and result is not None and fence is not None
            committed = await self.execution.commit_model_result(
                ticket.actor, fence, replace(result, text=plan.text), elapsed_ms=elapsed()
            )
            outcome = Outcome(
                ticket.run_id,
                committed.generation,
                RunStatus.SUCCEEDED,
                message_id=committed.message_id,
            )
            return {"route": "stop"}

        try:
            ticket = await self.service.open(job_id, token)
            remaining = ticket.record.limits.elapsed_ms - ticket.record.usage.elapsed_ms
            if remaining <= 0:
                raise BudgetExceededError("运行时间预算已用尽")
            graph = build_graph(
                Nodes(
                    safe(authorize), safe(load), safe(decide), safe(tool), safe(wait), safe(reply)
                ),
                checkpointer=self.checkpointer,
            )
            # 每个合法业务恢复都用新代际并重进授权；不执行 ainvoke(None) 的旧正文重放。
            async with asyncio.timeout(remaining / 1000):
                await graph.ainvoke(
                    GraphState(
                        run_id=str(ticket.run_id),
                        generation=ticket.generation,
                        step=0,
                        route="plan",
                    ),
                    config={
                        "configurable": {
                            "thread_id": ticket.thread_id,
                        },
                        # metadata.run_id 在当前库中参与恢复推断，不能填业务 Run ID。
                        "metadata": {
                            "autumn_run_id": str(ticket.run_id),
                            "autumn_execution_generation": ticket.generation,
                        },
                        "recursion_limit": 160,
                    },
                )
            if outcome is None:
                raise ConflictError("图结束但业务状态未落定")
            return outcome
        except (LeaseLostError, OptimisticLockError):
            # 旧 worker 不能替新代际/lease 写停止状态，交给 H 调度处理。
            raise
        except AuthorizationError as error:
            code: StopCode
            if error.decision.code is DenialCode.SESSION_EXPIRED:
                code = "SESSION_EXPIRED"
            elif error.decision.code is DenialCode.STEP_UP_REQUIRED:
                code = "STEP_UP_REQUIRED"
            else:
                code = "ACL_CONTEXT_INVALIDATED"
        except (BudgetExceededError, TimeoutError):
            code = "AGENT_BUDGET_EXCEEDED"
        except ProviderReconciliationRequired:
            code = "PROVIDER_OUTCOME_UNKNOWN"
        except InvalidInputError:
            code = "MODEL_OUTPUT_INVALID"
        except NodeFailure as error:
            code = error.code
        except Exception:
            code = (
                "PROVIDER_ERROR"
                if phase == "provider"
                else "ACL_CONTEXT_INVALIDATED"
                if phase == "context"
                else "AGENT_ERROR"
            )
        stopped = await self.service.stop(job_id, token, code=code, elapsed_ms=elapsed())
        return Outcome(stopped.run_id, stopped.generation, stopped.status, error_code=stopped.code)

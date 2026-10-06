"""SSE 的持久批次读取；不回放历史 payload 中的正文。"""

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from autumn_backend.db.enums import TERMINAL_RUN_STATUSES, RunEventType, RunStatus
from autumn_backend.db.models import Job, Message
from autumn_backend.db.session import UnitOfWorkFactory
from autumn_backend.errors import InvalidInputError
from autumn_backend.policies import ActorContext
from autumn_backend.policies.facts import Operation
from autumn_backend.services.actions import ActionService, action_dto
from autumn_backend.services.chats import ChatService
from autumn_backend.services.context import run_facts


@dataclass(frozen=True, slots=True)
class StreamEvent:
    name: str
    data: Any
    seq: int | None = None


@dataclass(frozen=True, slots=True)
class EventBatch:
    events: tuple[StreamEvent, ...]
    cursor: int
    close: bool


def identifier(payload: dict[str, Any] | None, field: str) -> UUID | None:
    if payload is None:
        return None
    try:
        return UUID(payload[field])
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def safe_code(value: object, default: str) -> str:
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", value)
        else default
    )


class EventService:
    def __init__(self, uows: UnitOfWorkFactory) -> None:
        self.uows = uows
        self.chats = ChatService(uows)

    async def batch(
        self, actor: ActorContext, run_id: UUID, *, after: int, snapshot: bool = False
    ) -> EventBatch:
        if after < 0:
            raise InvalidInputError("事件游标无效")
        async with self.uows() as uow:
            _, _, run = await run_facts(
                uow, actor, run_id, Operation.READ_RUN, require_context=False
            )
            if after > run.next_event_seq - 1:
                raise InvalidInputError("事件游标超过当前运行范围")
            rows = await uow.repositories.run_events.after(run_id, after, limit=100)
            action_ids = {
                value for row in rows if (value := identifier(row.payload, "action_id")) is not None
            }
            targets = []
            for candidate_id in sorted(action_ids):
                action = await uow.repositories.actions.get(candidate_id)
                if (
                    action is not None
                    and action.actor_id == actor.user_id
                    and action.run_id == run_id
                    and action.target_resource_id
                ):
                    targets.append(action.target_resource_id)
            manifest = run.config_snapshot.get("context_manifest", {})
            has_context = isinstance(manifest, dict) and manifest.get("complete") is True
            if has_context or run.status in {
                RunStatus.RUNNING,
                RunStatus.SUCCEEDED,
                RunStatus.WAITING_INPUT,
                RunStatus.WAITING_APPROVAL,
            }:
                await run_facts(
                    uow, actor, run_id, Operation.EMIT_RUN_OUTPUT, extra_resource_ids=tuple(targets)
                )
            else:
                for target in sorted(set(targets)):
                    await uow.repositories.resources.get_for_update(target)
            view = await self.chats._run_dto(uow, actor, run)
            events: list[StreamEvent] = []
            if snapshot:
                events.append(
                    StreamEvent(
                        "run.status",
                        {
                            "run_id": run.id,
                            "status": run.status.value,
                            "input_request": view["input_request"],
                        },
                    )
                )
                if view["message"] is not None:
                    message = view["message"]
                    events.append(
                        StreamEvent("message.snapshot", {"message_id": message["id"], **message})
                    )
            for row in rows:
                payload = row.payload or {}
                data: Any = None
                name = row.type.value
                match row.type:
                    case RunEventType.RUN_STATUS:
                        data = {
                            "run_id": run.id,
                            "status": run.status.value,
                            "input_request": view["input_request"],
                        }
                    case RunEventType.MESSAGE_SNAPSHOT:
                        message_id = identifier(row.payload, "message_id")
                        generation = payload.get("execution_generation", run.execution_generation)
                        message = await uow.session.get(Message, message_id) if message_id else None
                        if (
                            generation != run.execution_generation
                            or message is None
                            or message.run_id != run.id
                            or message.id != run.current_message_id
                        ):
                            continue
                        current = await self.chats._message(uow, actor, message)
                        data = {"message_id": message.id, **current}
                    case RunEventType.ACTION_PROPOSED | RunEventType.ACTION_SUCCEEDED:
                        action_id = identifier(row.payload, "action_id")
                        if action_id is None:
                            continue
                        action = await uow.repositories.actions.get(action_id)
                        if (
                            action is None
                            or action.run_id != run.id
                            or action.actor_id != actor.user_id
                        ):
                            continue
                        action = await ActionService(self.uows)._action(
                            uow, actor, action_id, Operation.READ_ACTION
                        )
                        data = (
                            action_dto(action)
                            if row.type is RunEventType.ACTION_PROPOSED
                            else {
                                "action_id": action.id,
                                "status": action.status.value,
                                "result": {
                                    key: value
                                    for key, value in (action.result or {}).items()
                                    if key
                                    in {
                                        "resource_id",
                                        "publication_id",
                                        "memory_id",
                                        "resource_version",
                                        "acl_version",
                                        "changed",
                                    }
                                },
                            }
                        )
                    case RunEventType.TOOL_STARTED:
                        data = {"display_name": "处理任务", "summary": "正在处理当前步骤"}
                    case RunEventType.TOOL_FINISHED:
                        data = {"result_summary": "当前步骤已结束"}
                    case RunEventType.KNOWLEDGE_PROCESSING:
                        job_id = identifier(row.payload, "job_id")
                        job = await uow.session.get(Job, job_id) if job_id else None
                        if job is None or job.actor_id != actor.user_id or job.run_id != run.id:
                            continue
                        data = {
                            "job_id": job.id,
                            "phase": job.phase.value if job.phase else None,
                            "progress": job.progress,
                        }
                    case RunEventType.SOURCE_INVALIDATED:
                        data = {
                            "message_ids": [run.current_message_id]
                            if run.current_message_id
                            else [],
                            "reason": "ACL_CONTEXT_INVALIDATED",
                        }
                    case RunEventType.SCOPE_CHANGED:
                        data = {
                            "reason": safe_code(payload.get("reason"), "CONTEXT_REBUILD_REQUIRED"),
                            "requires_step_up": payload.get("requires_step_up") is True,
                        }
                    case RunEventType.ERROR:
                        data = {
                            "code": safe_code(run.error_code, "RUN_ERROR"),
                            "message": "任务暂时无法继续",
                            "retryable": False,
                        }
                    case RunEventType.DONE:
                        if run.status not in TERMINAL_RUN_STATUSES:
                            continue
                        data = {"run_id": run.id, "status": run.status.value}
                if data is not None:
                    events.append(StreamEvent(name, data, row.seq))
            cursor = rows[-1].seq if rows else after
            close = (
                run.status
                in {
                    *TERMINAL_RUN_STATUSES,
                    RunStatus.WAITING_INPUT,
                    RunStatus.WAITING_APPROVAL,
                    RunStatus.WAITING_AUTH,
                }
                and cursor >= run.next_event_seq - 1
            )
            if (
                snapshot
                and close
                and run.status in TERMINAL_RUN_STATUSES
                and not any(item.name == "done" for item in events)
            ):
                events.append(StreamEvent("done", {"run_id": run.id, "status": run.status.value}))
            return EventBatch(tuple(events), cursor, close)

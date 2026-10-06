"""累计快照 SSE；每批事务关闭后发送，断线不修改运行状态。"""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from time import monotonic
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from fastapi.encoders import jsonable_encoder
from starlette.responses import StreamingResponse

from autumn_backend.api.deps import authenticated_actor
from autumn_backend.errors import ConfigurationError, DomainError, InvalidInputError
from autumn_backend.services.access import AuthorizationError
from autumn_backend.services.events import EventBatch, EventService, StreamEvent

router = APIRouter(prefix="/api/runs", tags=["events"])


def encode(event: StreamEvent) -> str:
    data = jsonable_encoder(
        event.data,
        custom_encoder={
            datetime: lambda value: value.astimezone(UTC).isoformat().replace("+00:00", "Z")
        },
    )
    prefix = f"id: {event.seq}\n" if event.seq is not None else ""
    return f"{prefix}event: {event.name}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


@router.get("/{run_id}/events")
async def events(
    request: Request,
    run_id: UUID,
    after: Annotated[int | None, Query(ge=0)] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID", max_length=20)] = None,
) -> StreamingResponse:
    actor = authenticated_actor(request)
    service = getattr(request.app.state, "events", None)
    if not isinstance(service, EventService):
        raise ConfigurationError("事件服务未初始化")
    if last_event_id is not None and (not last_event_id.isascii() or not last_event_id.isdigit()):
        raise InvalidInputError("事件游标无效")
    header_cursor = int(last_event_id) if last_event_id is not None else None
    if after is not None and header_cursor is not None and after != header_cursor:
        raise InvalidInputError("两个事件游标不一致")
    cursor = after if after is not None else header_cursor or 0
    initial = await service.batch(actor, run_id, after=cursor, snapshot=True)

    async def stream(first: EventBatch) -> AsyncIterator[str]:
        batch = first
        heartbeat = monotonic()
        try:
            while True:
                for item in batch.events:
                    if await request.is_disconnected():
                        return
                    yield encode(item)
                if batch.close or await request.is_disconnected():
                    return
                if monotonic() - heartbeat >= 15:
                    yield ": heartbeat\n\n"
                    heartbeat = monotonic()
                await asyncio.sleep(1)
                # 服务重新读取同一 session 的当前状态、到期与来源；不持有上批 UoW。
                batch = await service.batch(actor, run_id, after=batch.cursor)
        except DomainError as error:
            code = (
                str(error.decision.code)
                if isinstance(error, AuthorizationError)
                else error.code.upper()
            )
            yield encode(StreamEvent("source.invalidated", {"message_ids": [], "reason": code}))
            yield encode(
                StreamEvent(
                    "error",
                    {
                        "code": code,
                        "message": "当前权限已失效，请重新验证或刷新",
                        "retryable": False,
                    },
                )
            )

    return StreamingResponse(
        stream(initial),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )

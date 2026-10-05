"""运行幂等与持久事件序号的数据库仲裁。"""

from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from autumn_backend.db.enums import RunEventType
from autumn_backend.db.models import Run, RunEvent
from autumn_backend.errors import ConflictError, InvalidInputError, NotFoundError
from autumn_backend.repositories.base import AppendOnlyRepository, ControlledMutableRepository
from autumn_backend.repositories.constraints import database_errors
from autumn_backend.repositories.result import Creation


class RunRepository(ControlledMutableRepository[Run]):
    model = Run

    async def create_idempotent(
        self,
        *,
        user_id: UUID,
        conversation_id: UUID,
        idempotency_key: str,
        request_hash: str,
        scope_epoch: int,
        auth_session_id: UUID | None = None,
        config_snapshot: dict[str, Any] | None = None,
    ) -> Creation[Run]:
        if not idempotency_key or len(idempotency_key) > 128 or not request_hash or scope_epoch < 0:
            raise InvalidInputError("运行身份或权限版本无效")
        statement = (
            insert(Run)
            .values(
                user_id=user_id,
                conversation_id=conversation_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                scope_epoch=scope_epoch,
                auth_session_id=auth_session_id,
                checkpoint_thread_id=f"conversation:{conversation_id}",
                config_snapshot=config_snapshot or {},
            )
            .on_conflict_do_nothing(constraint="uq_runs_user_id_idempotency_key")
            .returning(Run)
        )
        with database_errors():
            record = (await self.session.execute(statement)).scalar_one_or_none()
        if record is not None:
            return Creation(record, True)
        # 新语句获得 READ COMMITTED 的新快照，能看见刚提交的竞争赢家。
        record = (
            await self.session.execute(
                select(Run).where(Run.user_id == user_id, Run.idempotency_key == idempotency_key)
            )
        ).scalar_one_or_none()
        if (
            record is None
            or record.request_hash != request_hash
            or record.conversation_id != conversation_id
        ):
            raise ConflictError("幂等键已用于不同请求")
        return Creation(record, False)


class RunEventRepository(AppendOnlyRepository):
    async def emit(
        self, run_id: UUID, event_type: RunEventType, payload: dict[str, Any] | None = None
    ) -> RunEvent:
        with database_errors():
            sequence = (
                await self.session.execute(
                    update(Run)
                    .where(Run.id == run_id)
                    .values(next_event_seq=Run.next_event_seq + 1)
                    .returning(Run.next_event_seq - 1)
                    .execution_options(synchronize_session=False)
                )
            ).scalar_one_or_none()
            if sequence is None:
                raise NotFoundError("运行不存在")
            event = RunEvent(run_id=run_id, seq=sequence, type=event_type, payload=payload or {})
            self.session.add(event)
            await self.session.flush()
        return event

    async def after(self, run_id: UUID, seq: int, *, limit: int = 200) -> list[RunEvent]:
        if seq < 0 or not 1 <= limit <= 500:
            raise InvalidInputError("事件回放范围无效")
        return list(
            (
                await self.session.scalars(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id, RunEvent.seq > seq)
                    .order_by(RunEvent.seq)
                    .limit(limit)
                )
            ).all()
        )

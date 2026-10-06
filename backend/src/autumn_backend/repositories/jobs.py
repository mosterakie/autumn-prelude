"""持久队列的入队、领取和租约仲裁；不执行业务 handler。"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from autumn_backend.db.enums import JobStatus
from autumn_backend.db.models import Job
from autumn_backend.errors import ConflictError, InvalidInputError, LeaseLostError
from autumn_backend.repositories.base import ControlledMutableRepository
from autumn_backend.repositories.constraints import database_errors
from autumn_backend.repositories.result import Creation


@dataclass(frozen=True, slots=True)
class JobSpec:
    kind: str
    idempotency_key: str
    payload: dict[str, Any]
    actor_id: UUID | None = None
    run_id: UUID | None = None
    resource_id: UUID | None = None
    auth_session_id: UUID | None = None
    available_at: datetime | None = None
    max_attempts: int = 5


class JobRepository(ControlledMutableRepository[Job]):
    model = Job

    async def require_lease(self, job_id: UUID, token: UUID) -> Job:
        job = (
            await self.session.execute(
                select(Job)
                .where(
                    Job.id == job_id,
                    Job.status == JobStatus.RUNNING,
                    Job.lease_token == token,
                    Job.lease_expires_at > func.clock_timestamp(),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if job is None:
            raise LeaseLostError("任务租约已丢失或过期")
        return job

    async def enqueue(self, spec: JobSpec) -> Creation[Job]:
        if (
            not spec.idempotency_key
            or spec.max_attempts < 1
            or (spec.available_at is not None and spec.available_at.tzinfo is None)
        ):
            raise InvalidInputError("任务身份、重试次数或时间无效")
        values: dict[str, Any] = dict(
            kind=spec.kind,
            idempotency_key=spec.idempotency_key,
            payload=spec.payload,
            actor_id=spec.actor_id,
            run_id=spec.run_id,
            resource_id=spec.resource_id,
            auth_session_id=spec.auth_session_id,
            max_attempts=spec.max_attempts,
        )
        if spec.available_at is not None:
            values["available_at"] = spec.available_at
        with database_errors():
            job = (
                await self.session.execute(
                    insert(Job)
                    .values(**values)
                    .on_conflict_do_nothing(constraint="uq_jobs_idempotency_key")
                    .returning(Job)
                )
            ).scalar_one_or_none()
        if job is not None:
            return Creation(job, True)
        job = (
            await self.session.execute(
                select(Job).where(Job.idempotency_key == spec.idempotency_key)
            )
        ).scalar_one_or_none()
        if job is None or (
            job.kind,
            job.payload,
            job.actor_id,
            job.run_id,
            job.resource_id,
            job.auth_session_id,
        ) != (
            spec.kind,
            spec.payload,
            spec.actor_id,
            spec.run_id,
            spec.resource_id,
            spec.auth_session_id,
        ):
            raise ConflictError("任务幂等键已用于不同操作")
        return Creation(job, False)

    @staticmethod
    def _duration(seconds: int) -> timedelta:
        if not 1 <= seconds <= 3600:
            raise InvalidInputError("租约时长必须在 1 到 3600 秒之间")
        return timedelta(seconds=seconds)

    async def claim(
        self, *, lease_seconds: int = 60, kinds: tuple[str, ...] | None = None
    ) -> Job | None:
        duration = self._duration(lease_seconds)
        statement = (
            select(Job.id)
            .where(
                Job.status == JobStatus.QUEUED,
                Job.available_at <= func.clock_timestamp(),
                Job.attempts < Job.max_attempts,
            )
            .order_by(Job.available_at, Job.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if kinds is not None:
            statement = statement.where(Job.kind.in_(kinds))
        job_id = (await self.session.execute(statement)).scalar_one_or_none()
        if job_id is None:
            return None
        with database_errors():
            return (
                await self.session.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.status == JobStatus.QUEUED)
                    .values(
                        status=JobStatus.RUNNING,
                        lease_token=uuid4(),
                        lease_expires_at=func.clock_timestamp() + duration,
                        heartbeat_at=func.clock_timestamp(),
                        attempts=Job.attempts + 1,
                        version=Job.version + 1,
                    )
                    .returning(Job)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()

    async def heartbeat(self, job_id: UUID, lease_token: UUID, *, lease_seconds: int = 60) -> Job:
        duration = self._duration(lease_seconds)
        return await self._leased_update(
            job_id,
            lease_token,
            dict(
                heartbeat_at=func.clock_timestamp(),
                lease_expires_at=func.clock_timestamp() + duration,
            ),
        )

    async def expired(self, *, limit: int = 100) -> tuple[UUID, ...]:
        if not 1 <= limit <= 1000:
            raise InvalidInputError("回收批量无效")
        return tuple(
            (
                await self.session.scalars(
                    select(Job.id)
                    .where(
                        Job.status == JobStatus.RUNNING,
                        Job.lease_expires_at <= func.clock_timestamp(),
                    )
                    .order_by(Job.lease_expires_at, Job.id)
                    .limit(limit)
                )
            ).all()
        )

    async def retry(self, job_id: UUID, token: UUID, *, delay_seconds: int, error_code: str) -> Job:
        if not 1 <= delay_seconds <= 3600:
            raise InvalidInputError("重试退避无效")
        job = await self.require_lease(job_id, token)
        exhausted = job.attempts >= job.max_attempts
        return await self._leased_update(
            job_id,
            token,
            dict(
                status=JobStatus.FAILED if exhausted else JobStatus.QUEUED,
                available_at=func.clock_timestamp() + timedelta(seconds=delay_seconds),
                error_code="max_attempts_exceeded" if exhausted else error_code,
                lease_token=None,
                lease_expires_at=None,
            ),
        )

    async def reclaim(
        self, job_id: UUID, expired_token: UUID, *, status: JobStatus, error_code: str
    ) -> Job:
        """只有可信收敛服务决定回收去向；本层不判断副作用是否可以重放。"""
        if status not in (
            JobStatus.QUEUED,
            JobStatus.WAITING_AUTH,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        ):
            raise InvalidInputError("回收去向无效")
        probe = await self.get_for_update_or_raise(job_id)
        if status is JobStatus.QUEUED and probe.attempts >= probe.max_attempts:
            status, error_code = JobStatus.FAILED, "max_attempts_exceeded"
        with database_errors():
            job = (
                await self.session.execute(
                    update(Job)
                    .where(
                        Job.id == job_id,
                        Job.status == JobStatus.RUNNING,
                        Job.lease_token == expired_token,
                        Job.lease_expires_at <= func.clock_timestamp(),
                    )
                    .values(
                        status=status,
                        error_code=error_code,
                        lease_token=None,
                        lease_expires_at=None,
                        available_at=func.clock_timestamp() + timedelta(seconds=5),
                        version=Job.version + 1,
                    )
                    .returning(Job)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
        if job is None:
            raise LeaseLostError("过期租约已被其他执行者处理")
        return job

    async def finish(
        self,
        job_id: UUID,
        lease_token: UUID,
        *,
        status: JobStatus = JobStatus.SUCCEEDED,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> Job:
        if status not in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}:
            raise InvalidInputError("任务只能结算为终态")
        return await self._leased_update(
            job_id,
            lease_token,
            dict(
                status=status,
                result=result or {},
                error_code=error_code,
                lease_token=None,
                lease_expires_at=None,
            ),
        )

    async def _leased_update(self, job_id: UUID, lease_token: UUID, values: dict[str, Any]) -> Job:
        with database_errors():
            job = (
                await self.session.execute(
                    update(Job)
                    .where(
                        Job.id == job_id,
                        Job.status == JobStatus.RUNNING,
                        Job.lease_token == lease_token,
                        Job.lease_expires_at > func.clock_timestamp(),
                    )
                    .values(**values, version=Job.version + 1)
                    .returning(Job)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
        if job is None:
            raise LeaseLostError("任务租约已丢失或过期")
        return job

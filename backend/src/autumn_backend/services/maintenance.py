"""有界后台收敛：未知结果保留，所有修改逐项重锁，不凭年龄重放副作用。"""

from datetime import timedelta
from uuid import UUID

from sqlalchemy import exists, false, func, or_, select

from autumn_backend.db.enums import (
    ActionStatus,
    IndexScope,
    JobStatus,
    ProviderCallStatus,
    RunEventType,
    RunStatus,
)
from autumn_backend.db.models import (
    Job,
    KnowledgeIndex,
    ProviderCall,
    Publication,
    Resource,
)
from autumn_backend.db.session import UnitOfWork, UnitOfWorkFactory
from autumn_backend.errors import ConflictError, LeaseLostError
from autumn_backend.jobs.queue import enqueue
from autumn_backend.repositories.jobs import JobSpec
from autumn_backend.services.context import advance_context_generation
from autumn_backend.services.quota import QuotaService


async def fail_run_in_uow(
    uow: UnitOfWork, uows: UnitOfWorkFactory, run_id: UUID, code: str
) -> None:
    run = await uow.repositories.runs.get_for_update_or_raise(run_id)
    if run.status not in (RunStatus.QUEUED, RunStatus.RUNNING):
        return
    await advance_context_generation(uow, run, preserve_sources=False)
    run.status, run.error_code = RunStatus.FAILED, code
    run.finished_at = await uow.repositories.users.database_time()
    run.version += 1
    await uow.session.flush()
    if not await uow.repositories.provider_calls.model_was_dispatched(run.id):
        reservation = await uow.repositories.quota_reservations.for_run(run.id)
        if reservation and reservation.status.value == "reserved":
            await QuotaService(uows).release_before_dispatch_in_uow(uow, run.id)
    await uow.repositories.run_events.emit(run.id, RunEventType.ERROR, {"code": code})
    await uow.repositories.run_events.emit(run.id, RunEventType.DONE, {"status": run.status.value})


class MaintenanceService:
    SAFE_KINDS = frozenset(
        {
            "run.dispatch",
            "run.resume",
            "run.cancel",
            "action.execute",
            "storage.finalize",
            "storage.delete",
            "knowledge.ingest",
            "knowledge.publication_sync",
            "knowledge.cleanup",
            "conversation.cleanup",
        }
    )

    def __init__(self, uows: UnitOfWorkFactory) -> None:
        self.uows = uows

    async def _reclaim(self, job_id: UUID) -> None:
        async with self.uows() as uow:
            probe = await uow.repositories.jobs.get_or_raise(job_id)
            if probe.actor_id:
                await uow.repositories.users.get_for_update_or_raise(probe.actor_id)
                if probe.auth_session_id:
                    await uow.repositories.auth_sessions.for_user_for_update(
                        probe.auth_session_id, probe.actor_id
                    )
            run = None
            if probe.run_id:
                candidate = await uow.repositories.runs.get_or_raise(probe.run_id)
                await uow.repositories.conversations.get_for_update_or_raise(
                    candidate.conversation_id
                )
                run = await uow.repositories.runs.get_for_update_or_raise(candidate.id)
            action = None
            if probe.kind == "action.execute":
                try:
                    action_id = UUID((probe.payload or {})["action_id"])
                except (KeyError, ValueError, TypeError):
                    action_id = None
                if action_id:
                    action = await uow.repositories.actions.get_for_update(action_id)
            job = await uow.repositories.jobs.get_for_update_or_raise(job_id)
            now = await uow.repositories.users.database_time()
            if (
                job.status is not JobStatus.RUNNING
                or job.lease_expires_at is None
                or job.lease_expires_at > now
            ):
                return
            assert job.lease_token is not None
            calls = (
                await uow.session.scalars(
                    select(ProviderCall)
                    .where(
                        or_(
                            ProviderCall.job_id == job.id,
                            (ProviderCall.job_id.is_(None)) & (ProviderCall.run_id == run.id)
                            if run
                            and run.execution_generation
                            == (job.payload or {}).get("execution_generation")
                            else false(),
                        ),
                        ProviderCall.status.in_(
                            (ProviderCallStatus.DISPATCHED, ProviderCallStatus.UNKNOWN)
                        ),
                    )
                    .order_by(ProviderCall.id)
                    .with_for_update()
                )
            ).all()
            for call in calls:
                if call.status is ProviderCallStatus.DISPATCHED:
                    await uow.repositories.provider_calls.settle_unknown(
                        call.id, error_code="WORKER_LEASE_EXPIRED"
                    )
            destination, code = JobStatus.QUEUED, "lease_expired"
            if calls:
                destination, code = JobStatus.FAILED, "provider_outcome_unknown"
            elif job.kind not in self.SAFE_KINDS:
                destination, code = JobStatus.FAILED, "unsupported_recovery"
            elif job.kind == "action.execute" and (
                action is None
                or action.status is not ActionStatus.READY
                or action.expires_at <= now
            ):
                destination, code = JobStatus.FAILED, "action_expired"
            elif (
                job.kind in ("run.dispatch", "run.resume", "action.execute")
                and run
                and (
                    run.execution_generation != (job.payload or {}).get("execution_generation")
                    or run.status not in (RunStatus.QUEUED, RunStatus.RUNNING)
                )
            ):
                destination, code = JobStatus.CANCELLED, "execution_superseded"
            job = await uow.repositories.jobs.reclaim(
                job.id, job.lease_token, status=destination, error_code=code
            )
            if job.kind == "auth.email":
                job.payload = {
                    key: value for key, value in (job.payload or {}).items() if key != "ciphertext"
                }
                job.version += 1
                await uow.session.flush()
            if job.status is JobStatus.FAILED:
                if action is not None and action.status is ActionStatus.READY:
                    action.status, action.error_code = ActionStatus.FAILED, job.error_code
                    action.version += 1
                if run and run.execution_generation == (job.payload or {}).get(
                    "execution_generation"
                ):
                    await fail_run_in_uow(
                        uow, self.uows, run.id, (job.error_code or "WORKER_ERROR").upper()
                    )
                await uow.session.flush()

    async def _release(self, run_id: UUID) -> None:
        async with self.uows() as uow:
            run = await QuotaService._lock_run(uow, run_id)
            if run.status not in (RunStatus.FAILED, RunStatus.CANCELLED):
                return
            reservation = await uow.repositories.quota_reservations.for_run(run.id, lock=True)
            if (
                reservation
                and reservation.status.value == "reserved"
                and not await uow.repositories.provider_calls.model_was_dispatched(run.id)
            ):
                await QuotaService(self.uows).release_before_dispatch_in_uow(uow, run.id)

    async def _orphan_call(self, call_id: UUID) -> None:
        async with self.uows() as uow:
            probe = await uow.repositories.provider_calls.get_or_raise(call_id)
            if probe.run_id:
                run = await QuotaService._lock_run(uow, probe.run_id)
                live = await uow.session.scalar(
                    select(Job.id)
                    .where(
                        Job.run_id == run.id,
                        Job.status == JobStatus.RUNNING,
                        Job.lease_expires_at > func.clock_timestamp(),
                    )
                    .limit(1)
                )
                if live:
                    return
            if probe.job_id:
                job = await uow.repositories.jobs.get_for_update_or_raise(probe.job_id)
                if (
                    job.status is JobStatus.RUNNING
                    and job.lease_expires_at is not None
                    and job.lease_expires_at > await uow.repositories.jobs.database_time()
                ):
                    return
            call = await uow.repositories.provider_calls.get_for_update_or_raise(call_id)
            if (
                call.status is ProviderCallStatus.DISPATCHED
                and call.started_at is not None
                and call.started_at
                < await uow.repositories.jobs.database_time() - timedelta(hours=1)
            ):
                await uow.repositories.provider_calls.settle_unknown(
                    call.id, error_code="ORPHANED_PROVIDER_CALL"
                )

    async def _retry_delete(self, job_id: UUID) -> None:
        async with self.uows() as uow:
            probe = await uow.repositories.jobs.get_or_raise(job_id)
            if probe.actor_id:
                await uow.repositories.users.get_for_update_or_raise(probe.actor_id)
            try:
                file_id = UUID((probe.payload or {})["file_id"])
            except (KeyError, ValueError, TypeError):
                return
            file = await uow.repositories.files.get_for_update(file_id)
            job = await uow.repositories.jobs.get_for_update_or_raise(job_id)
            if (
                file is None
                or file.deleted_at is not None
                or file.status.value != "pending_delete"
                or file.owner_id != job.actor_id
                or job.kind != "storage.delete"
                or job.status is not JobStatus.FAILED
                or job.error_code != "HANDLER_FAILED"
                or job.attempts >= job.max_attempts
            ):
                return
            unresolved = await uow.session.scalar(
                select(ProviderCall.id)
                .where(
                    ProviderCall.job_id == job.id,
                    ProviderCall.status.in_(
                        (ProviderCallStatus.DISPATCHED, ProviderCallStatus.UNKNOWN)
                    ),
                )
                .limit(1)
            )
            if unresolved:
                return
            job.status, job.error_code = JobStatus.QUEUED, None
            job.available_at = await uow.repositories.jobs.database_time() + timedelta(
                seconds=min(300, 5 * 2 ** min(job.attempts, 6))
            )
            job.version += 1
            await uow.session.flush()

    async def tick(self) -> None:
        async with self.uows() as uow:
            await uow.repositories.auth_credentials.scrub_expired_mail(limit=100)
            expired = await uow.repositories.jobs.expired(limit=100)
            now = await uow.repositories.users.database_time()
            reservations = await uow.repositories.quota_reservations.cleanup_candidates(
                now - timedelta(hours=1), limit=100
            )
            deletes = tuple(
                await uow.session.scalars(
                    select(Job.id)
                    .where(
                        Job.kind == "storage.delete",
                        Job.status == JobStatus.FAILED,
                        Job.error_code == "HANDLER_FAILED",
                        Job.attempts < Job.max_attempts,
                    )
                    .order_by(Job.id)
                    .limit(100)
                )
            )
            orphans = tuple(
                await uow.session.scalars(
                    select(ProviderCall.id)
                    .where(
                        ProviderCall.status == ProviderCallStatus.DISPATCHED,
                        ProviderCall.started_at < now - timedelta(hours=1),
                    )
                    .order_by(ProviderCall.id)
                    .limit(100)
                )
            )
            invalid = or_(
                Resource.deleted_at.is_not(None),
                Resource.archived_at.is_not(None),
                Resource.expires_at <= func.clock_timestamp(),
                (KnowledgeIndex.scope == IndexScope.OWNER)
                & (KnowledgeIndex.revision_id != Resource.current_revision_id),
                (KnowledgeIndex.scope == IndexScope.PUBLIC)
                & ~exists(
                    select(Publication.id).where(
                        Publication.id == KnowledgeIndex.publication_id,
                        Publication.revoked_at.is_(None),
                        Publication.ai_enabled.is_(True),
                    )
                ),
            )
            resources = tuple(
                await uow.session.scalars(
                    select(KnowledgeIndex.resource_id)
                    .join(Resource)
                    .where(
                        KnowledgeIndex.is_active.is_(True),
                        invalid,
                    )
                    .distinct()
                    .order_by(KnowledgeIndex.resource_id)
                    .limit(100)
                )
            )
        for job_id in expired:
            try:
                await self._reclaim(job_id)
            except (ConflictError, LeaseLostError):
                continue  # 已由另一执行者完成或重新续租。
        for run_id in reservations:
            await self._release(run_id)
        for call_id in orphans:
            await self._orphan_call(call_id)
        for job_id in deletes:
            await self._retry_delete(job_id)
        for resource_id in resources:
            async with self.uows() as uow:
                resource = await uow.repositories.resources.get_for_update_or_raise(resource_id)
                await enqueue(
                    uow,
                    JobSpec(
                        kind="knowledge.cleanup",
                        idempotency_key=f"knowledge.cleanup:{resource.id}:v{resource.version}:a{resource.acl_version}",
                        resource_id=resource.id,
                        payload={},
                    ),
                )

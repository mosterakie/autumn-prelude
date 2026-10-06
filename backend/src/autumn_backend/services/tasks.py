"""Worker 的可信身份与错误收尾；不读取模型提供的权限字段。"""

from uuid import UUID

from sqlalchemy import select

from autumn_backend.db.enums import JobStatus, ProviderCallStatus, RunStatus
from autumn_backend.db.models import ProviderCall
from autumn_backend.db.session import UnitOfWorkFactory
from autumn_backend.errors import NotFoundError, OptimisticLockError
from autumn_backend.jobs.queue import LeasedJob
from autumn_backend.policies import ActorContext, ActorRole, DenialCode
from autumn_backend.policies.facts import Operation, PolicyFacts
from autumn_backend.services.access import AuthorizationError, lock_authentication, require_allowed
from autumn_backend.services.runtime import RuntimeService, StopCode


class TaskService:
    def __init__(self, uows: UnitOfWorkFactory) -> None:
        self.uows = uows

    async def actor(self, job: LeasedJob) -> ActorContext:
        async with self.uows() as uow:
            probe = await uow.repositories.jobs.get_or_raise(job.id)
            if probe.actor_id is None or probe.auth_session_id is None or probe.kind != job.kind:
                raise OptimisticLockError("任务身份绑定无效")
            user = await uow.repositories.users.get_for_update_or_raise(probe.actor_id)
            session = await uow.repositories.auth_sessions.for_user_for_update(
                probe.auth_session_id, user.id
            )
            await uow.repositories.jobs.require_lease(job.id, job.token)
            if session is None:
                raise OptimisticLockError("任务会话不存在")
            return ActorContext(
                user_id=user.id,
                role=ActorRole(user.role.value),
                auth_session_id=session.id,
                step_up_expires_at=session.step_up_expires_at,
                capabilities=frozenset(),
                scope_epoch=await uow.repositories.settings.get_acl_epoch(),
            )

    async def failure(self, job: LeasedJob, error: Exception) -> None:
        code: StopCode = "AGENT_ERROR"
        if isinstance(error, AuthorizationError) and error.decision.code in (
            DenialCode.SESSION_EXPIRED,
            DenialCode.STEP_UP_REQUIRED,
        ):
            code = (
                "STEP_UP_REQUIRED"
                if error.decision.code is DenialCode.STEP_UP_REQUIRED
                else "SESSION_EXPIRED"
            )
        if job.kind in ("run.dispatch", "run.resume"):
            if await self.close_obsolete(job):
                return
            await RuntimeService(self.uows).stop(job.id, job.token, code=code)
            return
        async with self.uows() as uow:
            if (
                isinstance(error, AuthorizationError)
                and error.decision.code
                in (
                    DenialCode.SESSION_EXPIRED,
                    DenialCode.STEP_UP_REQUIRED,
                )
                and job.kind
                in (
                    "storage.finalize",
                    "storage.delete",
                    "knowledge.ingest",
                    "knowledge.publication_sync",
                )
            ):
                await uow.repositories.jobs.pause_auth(job.id, job.token, error_code=code)
                return
            await uow.repositories.jobs.finish(
                job.id, job.token, status=JobStatus.FAILED, error_code="HANDLER_FAILED"
            )

    async def close_obsolete(self, leased: LeasedJob) -> bool:
        """仅终结已失效的旧任务，不能改写新代际 Run 或生成正文。"""
        async with self.uows() as uow:
            probe = await uow.repositories.jobs.get_or_raise(leased.id)
            if probe.run_id is None or probe.actor_id is None:
                return False
            await uow.repositories.users.get_for_update_or_raise(probe.actor_id)
            if probe.auth_session_id:
                await uow.repositories.auth_sessions.for_user_for_update(
                    probe.auth_session_id, probe.actor_id
                )
            candidate = await uow.repositories.runs.get_or_raise(probe.run_id)
            await uow.repositories.conversations.for_user_for_update(
                candidate.conversation_id, probe.actor_id
            )
            run = await uow.repositories.runs.get_for_update_or_raise(candidate.id)
            job = await uow.repositories.jobs.require_lease(leased.id, leased.token)
            if (
                run.execution_generation == (job.payload or {}).get("execution_generation")
                and run.status
                in (
                    RunStatus.QUEUED,
                    RunStatus.RUNNING,
                )
                and run.auth_session_id == job.auth_session_id
            ):
                return False
            calls = (
                await uow.session.scalars(
                    select(ProviderCall)
                    .where(
                        ProviderCall.job_id == job.id,
                        ProviderCall.status == ProviderCallStatus.DISPATCHED,
                    )
                    .order_by(ProviderCall.id)
                    .with_for_update()
                )
            ).all()
            for call in calls:
                await uow.repositories.provider_calls.settle_unknown(
                    call.id, error_code="EXECUTION_SUPERSEDED"
                )
            await uow.repositories.jobs.finish(
                job.id, leased.token, status=JobStatus.CANCELLED, error_code="EXECUTION_SUPERSEDED"
            )
            return True

    async def resume(
        self, actor: ActorContext, job_id: UUID, *, expected_version: int
    ) -> dict[str, object]:
        async with self.uows() as uow:
            authentication = await lock_authentication(uow, actor)
            facts = PolicyFacts(
                operation=Operation.INGEST_RESOURCE,
                authentication=authentication,
                now=await uow.repositories.users.database_time(),
                current_scope_epoch=await uow.repositories.settings.get_acl_epoch(),
            )
            require_allowed(actor, facts)
            job = await uow.repositories.jobs.get_for_update_or_raise(job_id)
            if job.actor_id != actor.user_id or job.kind not in (
                "storage.finalize",
                "storage.delete",
                "knowledge.ingest",
                "knowledge.publication_sync",
            ):
                raise NotFoundError("任务不存在")
            unsettled = await uow.session.scalar(
                select(ProviderCall.id)
                .where(
                    ProviderCall.job_id == job.id,
                    ProviderCall.status.in_(
                        (ProviderCallStatus.DISPATCHED, ProviderCallStatus.UNKNOWN)
                    ),
                )
                .limit(1)
            )
            if unsettled is not None:
                raise OptimisticLockError("供应商结果未确定，不能恢复派发")
            assert actor.auth_session_id is not None
            job = await uow.repositories.jobs.resume_auth(
                job.id,
                expected_version=expected_version,
                session_id=actor.auth_session_id,
            )
            facts = PolicyFacts(
                operation=Operation.INGEST_RESOURCE,
                authentication=authentication,
                now=await uow.repositories.users.database_time(),
                current_scope_epoch=facts.current_scope_epoch,
            )
            require_allowed(actor, facts)
            return {"id": str(job.id), "status": job.status.value, "version": job.version}

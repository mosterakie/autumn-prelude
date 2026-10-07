"""Worker 的可信身份与错误收尾；不读取模型提供的权限字段。"""

from uuid import UUID

from sqlalchemy import select

from autumn_backend.db.enums import ActionStatus, JobStatus, ProviderCallStatus, RunStatus
from autumn_backend.db.models import Job, ProviderCall
from autumn_backend.db.session import UnitOfWork, UnitOfWorkFactory
from autumn_backend.errors import ConflictError, NotFoundError, OptimisticLockError
from autumn_backend.jobs.queue import LeasedJob
from autumn_backend.policies import ActorContext, ActorRole, DenialCode
from autumn_backend.policies.facts import Operation, PolicyFacts
from autumn_backend.services.access import AuthorizationError, lock_authentication, require_allowed
from autumn_backend.services.maintenance import fail_run_in_uow
from autumn_backend.services.runtime import RuntimeService, StopCode

KNOWLEDGE_TASK_KINDS = ("knowledge.ingest", "knowledge.bookmark")


def job_dto(job: Job) -> dict[str, object]:
    """只暴露状态和受控结果，不返回任务载荷、存储路径或租约令牌。"""
    messages = {
        "STEP_UP_REQUIRED": "站长验证已过期，请重新验证后恢复任务。",
        "SESSION_EXPIRED": "登录会话已过期，请重新登录并验证站长身份。",
        "IMPORT_INVALID_INPUT": "文件无法提取文字或网页内容无效；请检查文件类型、扫描件和页面访问限制。",
        "IMPORT_FETCH_FAILED": "网页抓取失败，请检查网址是否可公开访问。",
        "IMPORT_STALE": "资源内容或权限已变化，本次处理已停止。",
        "HANDLER_FAILED": "处理失败；请查看 Worker 日志，确认文件、网页与嵌入服务配置。",
        "provider_outcome_unknown": "供应商结果未确定，请先核对调用记录。",
    }
    return {
        "id": job.id,
        "kind": job.kind,
        "version": job.version,
        "resource_id": job.resource_id,
        "status": job.status.value,
        "phase": job.phase.value if job.phase else None,
        "progress": job.progress,
        "can_cancel": job.kind in KNOWLEDGE_TASK_KINDS and job.status is JobStatus.QUEUED,
        "can_retry": job.kind in KNOWLEDGE_TASK_KINDS
        and job.status is JobStatus.WAITING_AUTH
        and job.attempts < job.max_attempts,
        "result": {k: v for k, v in (job.result or {}).items() if k in ("resource_id", "index_id")},
        "error": {
            "code": job.error_code,
            "message": messages.get(job.error_code, "任务处理失败，请查看 Worker 日志。"),
        }
        if job.error_code
        else None,
    }


class TaskService:
    def __init__(self, uows: UnitOfWorkFactory) -> None:
        self.uows = uows

    async def _knowledge_job(self, uow: UnitOfWork, actor: ActorContext, job_id: UUID) -> Job:
        authentication = await lock_authentication(uow, actor)
        require_allowed(
            actor,
            PolicyFacts(
                operation=Operation.INGEST_RESOURCE,
                authentication=authentication,
                now=await uow.repositories.users.database_time(),
                current_scope_epoch=await uow.repositories.settings.get_acl_epoch(),
            ),
        )
        job = await uow.repositories.jobs.get_for_update_or_raise(job_id)
        if job.actor_id != actor.user_id or job.kind not in KNOWLEDGE_TASK_KINDS:
            raise NotFoundError("任务不存在")
        return job

    async def read(self, actor: ActorContext, job_id: UUID) -> dict[str, object]:
        async with self.uows() as uow:
            job = await self._knowledge_job(uow, actor, job_id)
            dto = job_dto(job)
            if dto["can_retry"]:
                dto["can_retry"] = (
                    await uow.session.scalar(
                        select(ProviderCall.id)
                        .where(
                            ProviderCall.job_id == job.id,
                            ProviderCall.status.in_(
                                (ProviderCallStatus.DISPATCHED, ProviderCallStatus.UNKNOWN)
                            ),
                        )
                        .limit(1)
                    )
                    is None
                )
            return dto

    async def cancel(self, actor: ActorContext, job_id: UUID) -> dict[str, object]:
        async with self.uows() as uow:
            job = await self._knowledge_job(uow, actor, job_id)
            if job.status is JobStatus.CANCELLED:
                return job_dto(job)
            if job.status is not JobStatus.QUEUED:
                raise ConflictError("仅尚未开始处理的任务可以取消")
            job.status, job.version = JobStatus.CANCELLED, job.version + 1
            await uow.session.flush()
            await self._knowledge_job(uow, actor, job_id)
            return job_dto(job)

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
        if job.kind == "action.execute":
            await self._fail_action(job)
            return
        async with self.uows() as uow:
            await uow.repositories.jobs.require_lease(job.id, job.token)
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
                    call.id, error_code="HANDLER_OUTCOME_UNKNOWN"
                )
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
                    "knowledge.bookmark",
                    "knowledge.publication_sync",
                )
            ):
                await uow.repositories.jobs.pause_auth(job.id, job.token, error_code=code)
                return
            from autumn_backend.errors import InvalidInputError

            error_code = "HANDLER_FAILED"
            if job.kind in KNOWLEDGE_TASK_KINDS:
                if isinstance(error, InvalidInputError):
                    error_code = "IMPORT_INVALID_INPUT"
                elif isinstance(error, OptimisticLockError):
                    error_code = "IMPORT_STALE"
                elif isinstance(error, (OSError, TimeoutError)):
                    error_code = "IMPORT_FETCH_FAILED"
            await uow.repositories.jobs.finish(
                job.id, job.token, status=JobStatus.FAILED, error_code=error_code
            )

    async def _fail_action(self, leased: LeasedJob) -> None:
        async with self.uows() as uow:
            probe = await uow.repositories.jobs.get_or_raise(leased.id)
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
            try:
                action_id = UUID((probe.payload or {})["action_id"])
            except (KeyError, ValueError, TypeError):
                action_id = None
            action = await uow.repositories.actions.get_for_update(action_id) if action_id else None
            job = await uow.repositories.jobs.require_lease(leased.id, leased.token)
            if action and action.actor_id == job.actor_id and action.status is ActionStatus.READY:
                action.status, action.error_code = ActionStatus.FAILED, "ACTION_EXECUTION_FAILED"
                action.version += 1
            if run and run.execution_generation == (job.payload or {}).get("execution_generation"):
                await fail_run_in_uow(uow, self.uows, run.id, "ACTION_EXECUTION_FAILED")
            await uow.session.flush()
            await uow.repositories.jobs.finish(
                job.id, leased.token, status=JobStatus.FAILED, error_code="ACTION_EXECUTION_FAILED"
            )

    async def waiting(self, actor: ActorContext) -> tuple[dict[str, object], ...]:
        async with self.uows() as uow:
            authentication = await lock_authentication(uow, actor)
            require_allowed(
                actor,
                PolicyFacts(
                    operation=Operation.INGEST_RESOURCE,
                    authentication=authentication,
                    now=await uow.repositories.users.database_time(),
                    current_scope_epoch=await uow.repositories.settings.get_acl_epoch(),
                ),
            )
            from autumn_backend.db.models import Job

            jobs = (
                await uow.session.scalars(
                    select(Job)
                    .where(Job.actor_id == actor.user_id, Job.status == JobStatus.WAITING_AUTH)
                    .order_by(Job.created_at, Job.id)
                    .limit(100)
                )
            ).all()
            return tuple(
                {"id": str(j.id), "kind": j.kind, "version": j.version, "status": j.status.value}
                for j in jobs
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
                "knowledge.bookmark",
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
            return job_dto(job)

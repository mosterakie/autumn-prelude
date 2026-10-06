"""Worker 的可信身份与错误收尾；不读取模型提供的权限字段。"""

from autumn_backend.db.enums import JobStatus
from autumn_backend.db.session import UnitOfWorkFactory
from autumn_backend.errors import OptimisticLockError
from autumn_backend.jobs.queue import LeasedJob
from autumn_backend.policies import ActorContext, ActorRole, DenialCode
from autumn_backend.services.access import AuthorizationError
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
        if isinstance(error, AuthorizationError):
            code = (
                "STEP_UP_REQUIRED"
                if error.decision.code is DenialCode.STEP_UP_REQUIRED
                else "SESSION_EXPIRED"
            )
        if job.kind in ("run.dispatch", "run.resume"):
            await RuntimeService(self.uows).stop(job.id, job.token, code=code)
            return
        async with self.uows() as uow:
            await uow.repositories.jobs.finish(
                job.id, job.token, status=JobStatus.FAILED, error_code="HANDLER_FAILED"
            )

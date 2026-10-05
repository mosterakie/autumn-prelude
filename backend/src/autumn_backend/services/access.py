"""当前事务中的事实装配与权限错误转换；纯 policies 不依赖此模块。"""

from autumn_backend.db.models import Resource
from autumn_backend.db.session import UnitOfWork
from autumn_backend.errors import DomainError, NotFoundError
from autumn_backend.policies import ActorContext, ActorRole, Decision, DenialCode, evaluate
from autumn_backend.policies.facts import (
    AccountStatus,
    AuthenticationFacts,
    Operation,
    PolicyFacts,
    ResourceFacts,
)


class AuthorizationError(DomainError):
    code = "permission_denied"

    def __init__(self, decision: Decision) -> None:
        self.decision = decision
        super().__init__("当前身份没有执行该操作的权限")


async def lock_authentication(uow: UnitOfWork, actor: ActorContext) -> AuthenticationFacts | None:
    """统一锁顺序 user → auth_session；禁用/撤销入口也应遵循相同顺序。"""
    if actor.user_id is None or actor.auth_session_id is None:
        return None
    user = await uow.repositories.users.get_for_update(actor.user_id)
    session = await uow.repositories.auth_sessions.for_user_for_update(
        actor.auth_session_id, actor.user_id
    )
    if user is None or session is None:
        return None
    return AuthenticationFacts(
        user_id=user.id,
        session_id=session.id,
        session_user_id=session.user_id,
        role=ActorRole(user.role.value),
        status=AccountStatus(user.status.value),
        user_auth_version=user.auth_version,
        session_auth_version=session.auth_version,
        idle_expires_at=session.idle_expires_at,
        absolute_expires_at=session.absolute_expires_at,
        verified_at=user.verified_at,
        ai_cooldown_until=user.ai_cooldown_until,
        step_up_expires_at=session.step_up_expires_at,
        revoked_at=session.revoked_at,
        user_deleted_at=user.deleted_at,
    )


async def publication_facts(
    uow: UnitOfWork,
    operation: Operation,
    authentication: AuthenticationFacts | None,
    resource: Resource | None,
) -> PolicyFacts:
    snapshot = None
    if resource is not None and resource.current_revision_id is not None:
        snapshot = ResourceFacts(
            resource_id=resource.id,
            owner_id=resource.owner_id,
            current_revision_id=resource.current_revision_id,
            acl_version=resource.acl_version,
            is_deleted=resource.deleted_at is not None,
            is_archived=resource.archived_at is not None,
            expires_at=resource.expires_at,
        )
    return PolicyFacts(
        operation=operation,
        now=await uow.repositories.users.database_time(),
        current_scope_epoch=await uow.repositories.settings.get_acl_epoch(),
        authentication=authentication,
        resource=snapshot,
    )


def require_allowed(actor: ActorContext, facts: PolicyFacts) -> None:
    decision = evaluate(actor, facts)
    if decision.code is DenialCode.NOT_FOUND:
        raise NotFoundError("对象不存在")
    if not decision.allowed:
        raise AuthorizationError(decision)

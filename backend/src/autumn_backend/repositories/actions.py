"""E1 所需的明确请求动作；预览、确认及其它动作由 E7 扩展。"""

from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, true, update
from sqlalchemy.dialects.postgresql import insert

from autumn_backend.db.enums import ActionAuthorizationKind, ActionStatus, ActionType
from autumn_backend.db.models import Action
from autumn_backend.errors import ConflictError, IdempotencyConflictError, InvalidInputError
from autumn_backend.repositories.base import ControlledMutableRepository
from autumn_backend.repositories.constraints import database_errors
from autumn_backend.repositories.result import Creation


class ActionRepository(ControlledMutableRepository[Action]):
    model = Action
    transition_fields = frozenset({"executed_at", "result"})

    async def by_actor_key(self, actor_id: UUID, key: str) -> Action | None:
        return (
            await self.session.execute(
                select(Action)
                .where(
                    Action.actor_id == actor_id,
                    Action.idempotency_key == key,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    async def begin_explicit_content(
        self,
        *,
        actor_id: UUID,
        auth_session_id: UUID,
        action_type: ActionType,
        resource_id: UUID,
        key: str,
        parameters: dict[str, Any],
        digest: str,
        expected_version: int | None,
        expected_acl_version: int | None,
    ) -> Action:
        if action_type not in {
            ActionType.CREATE_RESOURCE,
            ActionType.UPDATE_RESOURCE,
            ActionType.DELETE,
        }:
            raise InvalidInputError("不支持的原稿操作")
        # 调用方持有 actor 的 User 行锁并已查过 key；DB 唯一约束仍做最终仲裁。
        now = await self.database_time()
        action = Action(
            actor_id=actor_id,
            auth_session_id=auth_session_id,
            type=action_type,
            target_resource_id=resource_id,
            idempotency_key=key,
            parameters=parameters,
            parameters_hash=digest,
            expected_version=expected_version,
            expected_acl_version=expected_acl_version,
            status=ActionStatus.READY,
            requires_confirmation=False,
            authorization_kind=ActionAuthorizationKind.EXPLICIT_REQUEST,
            confirmed_at=now,
            expires_at=now + timedelta(minutes=15),
        )
        with database_errors():
            self.session.add(action)
            await self.session.flush()
        return action

    async def propose(
        self,
        *,
        actor_id: UUID,
        auth_session_id: UUID,
        run_id: UUID | None,
        authorization_message_id: UUID | None,
        action_type: ActionType,
        target_resource_id: UUID | None,
        expected_version: int | None,
        expected_acl_version: int | None,
        idempotency_key: str,
        parameters: dict[str, Any],
        parameters_hash: str,
    ) -> Creation[Action]:
        with database_errors():
            action = (
                await self.session.execute(
                    insert(Action)
                    .values(
                        actor_id=actor_id,
                        auth_session_id=auth_session_id,
                        run_id=run_id,
                        authorization_message_id=authorization_message_id,
                        type=action_type,
                        target_resource_id=target_resource_id,
                        expected_version=expected_version,
                        expected_acl_version=expected_acl_version,
                        idempotency_key=idempotency_key,
                        parameters=parameters,
                        parameters_hash=parameters_hash,
                        authorization_kind=ActionAuthorizationKind.CONFIRMED_PREVIEW,
                        status=ActionStatus.AWAITING_CONFIRMATION,
                        requires_confirmation=True,
                        expires_at=func.clock_timestamp() + timedelta(minutes=15),
                    )
                    .on_conflict_do_nothing(constraint="uq_actions_actor_id_idempotency_key")
                    .returning(Action)
                )
            ).scalar_one_or_none()
        if action is not None:
            return Creation(action, True)
        action = (
            await self.session.execute(
                select(Action)
                .where(Action.actor_id == actor_id, Action.idempotency_key == idempotency_key)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        if (
            action.type,
            action.target_resource_id,
            action.expected_version,
            action.expected_acl_version,
            action.run_id,
            action.authorization_message_id,
            action.parameters_hash,
            action.parameters,
            action.authorization_kind,
            action.requires_confirmation,
        ) != (
            action_type,
            target_resource_id,
            expected_version,
            expected_acl_version,
            run_id,
            authorization_message_id,
            parameters_hash,
            parameters,
            ActionAuthorizationKind.CONFIRMED_PREVIEW,
            True,
        ):
            raise IdempotencyConflictError("动作幂等键已用于不同语义")
        return Creation(action, False)

    async def confirm(
        self, action_id: UUID, expected_version: int, auth_session_id: UUID
    ) -> Action:
        with database_errors():
            action = (
                await self.session.execute(
                    update(Action)
                    .where(
                        Action.id == action_id,
                        Action.version == expected_version,
                        Action.status == ActionStatus.AWAITING_CONFIRMATION,
                        Action.authorization_kind == ActionAuthorizationKind.CONFIRMED_PREVIEW,
                        Action.requires_confirmation.is_(True),
                        Action.expires_at > func.clock_timestamp(),
                    )
                    .values(
                        status=ActionStatus.READY,
                        confirmed_at=func.clock_timestamp(),
                        auth_session_id=auth_session_id,
                        version=Action.version + 1,
                    )
                    .returning(Action)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
        if action is None:
            raise ConflictError("动作已过期或状态已变化")
        return action

    async def cancel(
        self, action_id: UUID, expected_version: int, *, expired: bool = False
    ) -> Action:
        with database_errors():
            action = (
                await self.session.execute(
                    update(Action)
                    .where(
                        Action.id == action_id,
                        Action.version == expected_version,
                        Action.status.in_((ActionStatus.AWAITING_CONFIRMATION, ActionStatus.READY)),
                        Action.expires_at <= func.clock_timestamp() if expired else true(),
                    )
                    .values(
                        status=ActionStatus.EXPIRED if expired else ActionStatus.CANCELLED,
                        version=Action.version + 1,
                    )
                    .returning(Action)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
        if action is None:
            raise ConflictError("动作状态已变化")
        return action

    async def begin_explicit_publication(
        self,
        *,
        actor_id: UUID,
        auth_session_id: UUID,
        action_type: ActionType,
        resource_id: UUID,
        expected_version: int,
        expected_acl_version: int,
        idempotency_key: str,
        parameters: dict[str, Any],
        parameters_hash: str,
    ) -> Creation[Action]:
        if action_type not in (ActionType.PUBLISH, ActionType.REVOKE):
            raise InvalidInputError("此入口只接受发布或撤回")
        with database_errors():
            action = (
                await self.session.execute(
                    insert(Action)
                    .values(
                        actor_id=actor_id,
                        auth_session_id=auth_session_id,
                        type=action_type,
                        target_resource_id=resource_id,
                        expected_version=expected_version,
                        expected_acl_version=expected_acl_version,
                        idempotency_key=idempotency_key,
                        parameters=parameters,
                        parameters_hash=parameters_hash,
                        authorization_kind=ActionAuthorizationKind.EXPLICIT_REQUEST,
                        status=ActionStatus.READY,
                        requires_confirmation=False,
                        confirmed_at=func.clock_timestamp(),
                        expires_at=func.clock_timestamp() + timedelta(minutes=15),
                    )
                    .on_conflict_do_nothing(constraint="uq_actions_actor_id_idempotency_key")
                    .returning(Action)
                )
            ).scalar_one_or_none()
        if action is not None:
            return Creation(action, True)
        action = (
            await self.session.execute(
                select(Action)
                .where(Action.actor_id == actor_id, Action.idempotency_key == idempotency_key)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if action is None:
            raise ConflictError("操作身份读取失败")
        if (
            action.type is not action_type
            or action.target_resource_id != resource_id
            or action.expected_version != expected_version
            or action.expected_acl_version != expected_acl_version
            or action.parameters_hash != parameters_hash
            or action.parameters != parameters
            or action.authorization_kind is not ActionAuthorizationKind.EXPLICIT_REQUEST
            or action.requires_confirmation
        ):
            raise IdempotencyConflictError("幂等键已用于不同操作")
        return Creation(action, False)

    async def succeed_explicit(self, action_id: UUID, result: dict[str, Any]) -> Action:
        with database_errors():
            action = (
                await self.session.execute(
                    update(Action)
                    .where(
                        Action.id == action_id,
                        Action.status == ActionStatus.READY,
                        Action.authorization_kind == ActionAuthorizationKind.EXPLICIT_REQUEST,
                        Action.requires_confirmation.is_(False),
                        Action.expires_at > func.clock_timestamp(),
                    )
                    .values(
                        status=ActionStatus.SUCCEEDED,
                        executed_at=func.clock_timestamp(),
                        result=result,
                        version=Action.version + 1,
                    )
                    .returning(Action)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
        if action is None:
            raise ConflictError("操作已过期或状态已变化")
        return action

    async def succeed_confirmed(self, action_id: UUID, result: dict[str, Any]) -> Action:
        with database_errors():
            action = (
                await self.session.execute(
                    update(Action)
                    .where(
                        Action.id == action_id,
                        Action.status == ActionStatus.READY,
                        Action.authorization_kind == ActionAuthorizationKind.CONFIRMED_PREVIEW,
                        Action.requires_confirmation.is_(True),
                        Action.confirmed_at.is_not(None),
                        Action.expires_at > func.clock_timestamp(),
                    )
                    .values(
                        status=ActionStatus.SUCCEEDED,
                        executed_at=func.clock_timestamp(),
                        result=result,
                        version=Action.version + 1,
                    )
                    .returning(Action)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
        if action is None:
            raise ConflictError("确认动作已过期或状态已变化")
        return action

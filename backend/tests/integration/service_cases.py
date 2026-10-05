"""后续 E 节点共享隔离夹具，真实 PostgreSQL，业务 UoW 使用独立 SAVEPOINT。"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from autumn_backend.db.enums import ConversationMode, ResourceKind, UserRole, UserStatus
from autumn_backend.db.models import AuthSession, Run, User
from autumn_backend.db.session import UnitOfWork, UnitOfWorkFactory
from autumn_backend.policies import ActorContext, ActorRole
from autumn_backend.repositories.publications import PublicationProjection
from autumn_backend.repositories.resources import RevisionDraft
from autumn_backend.services.ai_limits import daily_window


@dataclass(frozen=True)
class ServiceCase:
    uows: UnitOfWorkFactory
    member: ActorContext
    owner: ActorContext
    resource_id: UUID
    revision_id: UUID
    publication_id: UUID

    async def run(self, uow: UnitOfWork, *, owner: bool = False) -> Run:
        actor = self.owner if owner else self.member
        assert actor.user_id is not None
        conversation = await uow.repositories.conversations.create(
            user_id=actor.user_id, mode=ConversationMode.OWNER if owner else ConversationMode.PUBLIC
        )
        run = (
            await uow.repositories.runs.create_idempotent(
                user_id=actor.user_id,
                conversation_id=conversation.id,
                idempotency_key=uuid4().hex,
                request_hash=uuid4().hex,
                auth_session_id=actor.auth_session_id,
                scope_epoch=await uow.repositories.settings.get_acl_epoch(),
            )
        ).record
        now = await uow.repositories.users.database_time()
        start, end = daily_window(now, "Asia/Shanghai")
        bucket = await uow.repositories.quota_buckets.get_or_create_for_update(
            actor.user_id, start, end
        )
        await uow.repositories.quota_reservations.reserve(
            run_id=run.id, bucket_id=bucket.id, user_id=actor.user_id, current_limit=100
        )
        return run


@pytest.fixture
async def e_case(engine: AsyncEngine) -> AsyncIterator[ServiceCase]:
    async with engine.connect() as connection:
        async with connection.begin():
            uows = UnitOfWorkFactory(
                async_sessionmaker(
                    connection,
                    expire_on_commit=False,
                    autoflush=False,
                    join_transaction_mode="create_savepoint",
                )
            )
            async with uows() as uow:
                now = await uow.repositories.users.database_time()
                actors = []
                for role in (UserRole.MEMBER, UserRole.OWNER):
                    user = User(
                        email_normalized=f"stage-e-{uuid4().hex}@example.com",
                        password_hash="placeholder",
                        role=role,
                        status=UserStatus.ACTIVE,
                        verified_at=now,
                    )
                    uow.session.add(user)
                    await uow.session.flush()
                    session = AuthSession(
                        user_id=user.id,
                        token_hash=uuid4().hex,
                        auth_version=user.auth_version,
                        idle_expires_at=now + timedelta(hours=2),
                        absolute_expires_at=now + timedelta(days=1),
                        step_up_expires_at=now + timedelta(minutes=15)
                        if role is UserRole.OWNER
                        else None,
                    )
                    uow.session.add(session)
                    await uow.session.flush()
                    actors.append(
                        ActorContext(
                            user_id=user.id,
                            role=ActorRole(role.value),
                            auth_session_id=session.id,
                            step_up_expires_at=session.step_up_expires_at,
                            capabilities=frozenset(),
                            scope_epoch=0,
                        )
                    )
                member, owner = actors
                assert owner.user_id is not None
                resource = await uow.repositories.resources.create(
                    owner_id=owner.user_id,
                    kind=ResourceKind.ARTICLE,
                    slug=f"stage-e-{uuid4().hex}",
                    draft=RevisionDraft(
                        title="公开标题", body_text="公共正文", private_note="私密备注"
                    ),
                )
                assert resource.current_revision_id is not None
                publication = await uow.repositories.publications.publish_under_resource_lock(
                    resource_id=resource.id,
                    revision_id=resource.current_revision_id,
                    expected_version=0,
                    expected_acl_version=0,
                    published_by=owner.user_id,
                    projection=PublicationProjection(frozenset({"title", "body"}), ai_enabled=True),
                )
                value = ServiceCase(
                    uows, member, owner, resource.id, resource.current_revision_id, publication.id
                )
            yield value
            await connection.rollback()

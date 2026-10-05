"""真实 PostgreSQL 验证事务边界和 Task 所有权。"""

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from autumn_backend.db.models import User
from autumn_backend.db.session import UnitOfWorkFactory

pytestmark = pytest.mark.integration


@pytest.fixture
async def uows(engine: AsyncEngine) -> AsyncIterator[UnitOfWorkFactory]:
    async with engine.connect() as connection:
        async with connection.begin():
            yield UnitOfWorkFactory(
                async_sessionmaker(
                    connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
                )
            )
            await connection.rollback()


async def test_success_commits_and_exception_rolls_back(uows: UnitOfWorkFactory) -> None:
    email = f"{uuid4()}@example.com"
    async with uows() as uow:
        user = User(email_normalized=email, password_hash="placeholder")
        uow.session.add(user)
    async with uows() as uow:
        assert await uow.repositories.users.get(user.id) is not None
    with pytest.raises(ValueError, match="abort"):
        async with uows() as uow:
            uow.session.add(User(email_normalized="rolled-back@example.com", password_hash="x"))
            await uow.session.flush()
            raise ValueError("abort")
    async with uows() as uow:
        assert (
            await uow.session.scalar(
                select(User).where(User.email_normalized == "rolled-back@example.com")
            )
            is None
        )


async def test_commit_failure_rolls_back_and_factory_can_start_again(
    uows: UnitOfWorkFactory,
) -> None:
    from autumn_backend.errors import ConflictError

    async with uows() as uow:
        uow.session.add(User(email_normalized="unique@example.com", password_hash="x"))
    broken = uows()
    with pytest.raises(ConflictError):
        async with broken:
            broken.session.add(User(email_normalized="unique@example.com", password_hash="x"))
    assert broken.finished
    async with uows() as uow:
        assert await uow.repositories.settings.get("content_acl_epoch") is not None


async def test_repositories_share_session_and_cannot_escape_transaction(
    uows: UnitOfWorkFactory,
) -> None:
    instance = uows()
    async with instance as uow:
        repositories = uow.repositories
        assert repositories.users.session is repositories.settings.session is uow.session
        await uow.commit()
        with pytest.raises(RuntimeError, match="已经结束"):
            await repositories.settings.get("content_acl_epoch")
    with pytest.raises(RuntimeError, match="已经结束"):
        _ = instance.session
    with pytest.raises(RuntimeError, match="重复进入"):
        async with instance:
            pass


async def test_cross_task_access_rejected(uows: UnitOfWorkFactory) -> None:
    async with uows() as uow:
        repositories = uow.repositories

        async def another_task() -> None:
            await repositories.settings.get("content_acl_epoch")

        with pytest.raises(RuntimeError, match="跨并发 Task"):
            await asyncio.create_task(another_task())


async def test_factory_creates_independent_sessions_per_task(engine: AsyncEngine) -> None:
    factory = UnitOfWorkFactory(async_sessionmaker(engine))
    sessions = []
    barrier = asyncio.Barrier(2)

    async def read() -> None:
        async with factory() as uow:
            sessions.append(uow.session)
            await barrier.wait()
            assert await uow.repositories.settings.get("content_acl_epoch") is not None

    await asyncio.gather(read(), read())
    assert sessions[0] is not sessions[1]

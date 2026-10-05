"""真实独立连接 + 同步起跑；提交测试数据，结束仅清理本用例创建的用户。"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from autumn_backend.db.enums import ConversationMode
from autumn_backend.db.models import Conversation, Publication, Setting, User
from autumn_backend.db.session import UnitOfWork, UnitOfWorkFactory
from autumn_backend.repositories.jobs import JobSpec


@dataclass(frozen=True)
class ConcurrentCase:
    uows: UnitOfWorkFactory
    user_id: UUID
    conversation_ids: tuple[UUID, ...]
    run_ids: tuple[UUID, ...]
    job_kind: str
    job_id: UUID


@pytest.fixture
async def concurrent_case(real_database_url: str) -> AsyncIterator[ConcurrentCase]:
    name = make_url(real_database_url).database or ""
    if "test" not in name and name != "autumn_ci":
        pytest.fail("并发测试只能运行在名称含 test 或为 autumn_ci 的独立数据库")
    engine = create_async_engine(
        real_database_url,
        pool_size=10,
        max_overflow=0,
        connect_args={
            "server_settings": {
                "timezone": "UTC",
                "statement_timeout": "10000",
                "lock_timeout": "8000",
            }
        },
    )
    factory = UnitOfWorkFactory(async_sessionmaker(engine, expire_on_commit=False, autoflush=False))
    user_id = uuid4()
    job_kind = "test.concurrent_" + uuid4().hex
    epoch_value, epoch_version = None, None
    try:
        async with factory() as uow:
            epoch = await uow.repositories.settings.get("content_acl_epoch")
            assert epoch is not None
            epoch_value, epoch_version = epoch.value, epoch.version
            uow.session.add(
                User(
                    id=user_id, email_normalized=f"{user_id}@example.com", password_hash="test-only"
                )
            )
            await uow.session.flush()
            conversations = [
                Conversation(user_id=user_id, mode=ConversationMode.PUBLIC) for _ in range(3)
            ]
            uow.session.add_all(conversations)
            await uow.session.flush()
            run_ids = []
            for conversation in conversations[:2]:
                run = (
                    await uow.repositories.runs.create_idempotent(
                        user_id=user_id,
                        conversation_id=conversation.id,
                        idempotency_key=str(conversation.id),
                        request_hash="seed",
                        scope_epoch=0,
                    )
                ).record
                run_ids.append(run.id)
            job = (
                await uow.repositories.jobs.enqueue(
                    JobSpec(kind=job_kind, idempotency_key=job_kind, payload={}, actor_id=user_id)
                )
            ).record
            case = ConcurrentCase(
                factory,
                user_id,
                tuple(c.id for c in conversations),
                tuple(run_ids),
                job_kind,
                job.id,
            )
        yield case
    finally:
        try:
            async with engine.begin() as connection:
                # published_by 是 RESTRICT；先清理本用例的发布，避免 FK 清理顺序依赖。
                await connection.execute(
                    delete(Publication).where(Publication.published_by == user_id)
                )
                await connection.execute(delete(User).where(User.id == user_id))
                # 仅在独立测试库恢复迁移种子。真实业务中权限版本必须保持单调。
                if epoch_value is not None:
                    await connection.execute(
                        update(Setting)
                        .where(Setting.key == "content_acl_epoch")
                        .values(value=epoch_value, version=epoch_version)
                    )
        finally:
            await engine.dispose()


async def race(
    case: ConcurrentCase,
    operation: Callable[[UnitOfWork, int], Awaitable[object]],
    *,
    count: int = 2,
) -> list[object]:
    barrier = asyncio.Barrier(count)
    pids: list[int] = []

    async def participant(index: int) -> object:
        async with case.uows() as uow:
            pids.append((await uow.session.execute(select(func.pg_backend_pid()))).scalar_one())
            await barrier.wait()
            return await operation(uow, index)

    results = await asyncio.wait_for(
        asyncio.gather(*(participant(i) for i in range(count)), return_exceptions=True), timeout=20
    )
    assert len(set(pids)) == count, "必须使用不同 PostgreSQL 连接进行并发验证"
    return list(results)

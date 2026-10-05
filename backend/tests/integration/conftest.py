"""集成测试夹具：真实 PostgreSQL + pgvector。

纪律（实施顺序 C1）：**禁止**用 SQLite 或 Mock Repository 替代真实数据库。
连接串来自 ``AUTUMN_TEST_DATABASE_URL``；未设置时用例显式 skip。

每个用例运行在一个外层事务中，结束即回滚——因此约束违例不会污染数据库，
而"约束真的拦住了"这件事仍然在真实事务里发生。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from autumn_backend.db.models import load_all_models


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("AUTUMN_TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "未设置 AUTUMN_TEST_DATABASE_URL："
            "真实 PostgreSQL 用例需要独立测试库（阶段 C1 起为硬闸门）"
        )
    return url


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    load_all_models()
    engine = create_async_engine(
        database_url,
        poolclass=None,  # NullPool：不跨事件循环复用连接
        connect_args={"server_settings": {"timezone": "UTC"}},
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """每个用例一个事务，结束时回滚。

    清理顺序很重要：必须先把连接**交还**给外层事务，再关闭会话。
    反过来（先 close 会话）会让 SQLAlchemy 报
    ``SAWarning: transaction already deassociated from connection``。
    """
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async_session = AsyncSession(bind=connection, expire_on_commit=False)
        try:
            yield async_session
        finally:
            # 会话必须在外层事务之前关闭：负向用例会让会话处于"已失败"状态，
            # 若先 rollback 外层事务，连接会与会话中仍存在的 transaction 解绑，
            # SQLAlchemy 随即报
            # ``SAWarning: transaction already deassociated from connection``。
            await async_session.close()
            await transaction.rollback()


@pytest.fixture
def make_user(session: AsyncSession) -> Callable[..., object]:
    """构造 User 的便捷工厂；``email_canonical`` 默认与 ``email`` 相同。"""

    from autumn_backend.db.models import User

    def _make(*, email: str = "user@example.com", **overrides: object) -> User:
        user = User(
            email=email,
            email_canonical=str(overrides.pop("email_canonical", email)),
            password_hash=str(overrides.pop("password_hash", "$argon2id$placeholder")),
            **overrides,
        )
        session.add(user)
        return user

    return _make

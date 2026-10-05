"""集成测试夹具：真实 PostgreSQL + pgvector。

纪律（实施顺序 C1）：**禁止**用 SQLite 或 Mock Repository 替代真实数据库。
连接串来自 ``AUTUMN_TEST_DATABASE_URL``；未设置时用例显式 skip。

每个用例运行在一个外层事务中，结束即回滚——因此约束违例不会污染数据库，
而"约束真的拦住了"这件事仍然在真实事务里发生。

除 ``session`` 外，这里还提供各主要实体的**构造工厂**（``make_user`` /
``make_resource`` / ``make_resource_version`` / ``make_publication`` /
``make_conversation`` / ``make_run``），让约束用例专注于"哪一条约束被触发"，
而不是重复拼装满足所有前序外键的脚手架。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from autumn_backend.db.enums import ContentFormat, ConversationMode, ResourceKind
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
def make_user(session: AsyncSession) -> Callable[..., Any]:
    """构造 ``users`` 的便捷工厂。

    新契约里唯一性由 ``email_normalized`` 承担：它必须**已经是**小写去空白形式
    （CHECK ``email_normalized = lower(btrim(email_normalized))``），规范化由 auth
    service 在写入前完成。``email`` 参数因此是"规范化后的邮箱"的简写，
    没有保留原始书写形式的 ``email`` 列。
    """

    from autumn_backend.db.models import User

    def _make(*, email: str = "user@example.com", **overrides: Any) -> User:
        user = User(
            email_normalized=str(overrides.pop("email_normalized", email)),
            password_hash=str(overrides.pop("password_hash", "$argon2id$placeholder")),
            **overrides,
        )
        session.add(user)
        return user

    return _make


@pytest_asyncio.fixture
async def make_resource(
    session: AsyncSession,
) -> Callable[..., Awaitable[Any]]:
    """构造 ``resources``：``slug`` 全局唯一，因此默认随机生成。"""
    from autumn_backend.db.models import Resource

    async def _make(
        owner_id: uuid.UUID,
        *,
        kind: ResourceKind = ResourceKind.ARTICLE,
        slug: str | None = None,
        **overrides: Any,
    ) -> Any:
        resource = Resource(
            owner_id=owner_id,
            kind=kind,
            slug=slug if slug is not None else f"res-{uuid.uuid4().hex[:16]}",
            **overrides,
        )
        session.add(resource)
        await session.flush()
        return resource

    return _make


@pytest_asyncio.fixture
async def make_resource_version(
    session: AsyncSession,
) -> Callable[..., Awaitable[Any]]:
    """构造 ``resource_versions``：版本行创建后不可编辑（Repository 规则）。"""
    from autumn_backend.db.models import ResourceVersion

    async def _make(
        resource: Any,
        *,
        revision_no: int = 1,
        content_format: ContentFormat = ContentFormat.MARKDOWN,
        **overrides: Any,
    ) -> Any:
        version = ResourceVersion(
            resource_id=resource.id,
            revision_no=revision_no,
            content_format=content_format,
            title=overrides.pop("title", "标题"),
            body_text=overrides.pop("body_text", "正文"),
            tags=overrides.pop("tags", []),
            **overrides,
        )
        session.add(version)
        await session.flush()
        return version

    return _make


@pytest_asyncio.fixture
async def make_publication(
    session: AsyncSession,
) -> Callable[..., Awaitable[Any]]:
    """构造 ``publications``：默认是"只公开标题"的最小合法投影。

    ``public_fields`` 白名单要求"不在白名单里的字段列必须为空"，因此默认值是
    ``public_fields=['title']`` + ``public_title`` 有值，其余字段列为 NULL/空数组。
    """
    from autumn_backend.db.models import Publication

    async def _make(
        resource: Any,
        revision: Any,
        *,
        publication_no: int = 1,
        published_by: uuid.UUID | None = None,
        **overrides: Any,
    ) -> Any:
        publication = Publication(
            resource_id=resource.id,
            revision_id=revision.id,
            publication_no=publication_no,
            published_by=published_by if published_by is not None else resource.owner_id,
            public_fields=overrides.pop("public_fields", ["title"]),
            public_title=overrides.pop("public_title", "公开标题"),
            **overrides,
        )
        session.add(publication)
        await session.flush()
        return publication

    return _make


@pytest_asyncio.fixture
async def make_conversation(
    session: AsyncSession,
) -> Callable[..., Awaitable[Any]]:
    """构造 ``conversations``：``mode`` 没有数据库默认值，必须显式给出。"""
    from autumn_backend.db.models import Conversation

    async def _make(
        user_id: uuid.UUID,
        *,
        mode: ConversationMode = ConversationMode.PUBLIC,
        **overrides: Any,
    ) -> Any:
        conversation = Conversation(user_id=user_id, mode=mode, **overrides)
        session.add(conversation)
        await session.flush()
        return conversation

    return _make


@pytest_asyncio.fixture
async def make_run(session: AsyncSession) -> Callable[..., Awaitable[Any]]:
    """构造 ``runs``：默认归属该会话的所有者，满足 ``(user_id, conversation_id)``。"""
    from autumn_backend.db.models import Run

    async def _make(
        conversation: Any,
        *,
        user_id: uuid.UUID | None = None,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
        **overrides: Any,
    ) -> Any:
        run = Run(
            user_id=user_id if user_id is not None else conversation.user_id,
            conversation_id=conversation.id,
            idempotency_key=idempotency_key or f"key-{uuid.uuid4().hex[:16]}",
            request_hash=request_hash or "r" * 64,
            checkpoint_thread_id=overrides.pop(
                "checkpoint_thread_id", f"thread-{uuid.uuid4().hex[:16]}"
            ),
            **overrides,
        )
        session.add(run)
        await session.flush()
        return run

    return _make

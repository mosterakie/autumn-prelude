"""异步数据库引擎、会话工厂与最小 Unit of Work。

UoW 原则（架构文档 §4.1 / Repository 文档 §4）：
- 一个原子数据库阶段 = 一个 UoW；退出时统一 commit 或 rollback。
- Repository 内允许 flush，**禁止 commit**（阶段 B1 的 RepositoryBase 强制）。
- 外部 I/O 前必须结束当前 UoW；外部 I/O 后需要落库时开启新 UoW。
- ``AsyncSession`` **不跨并发协程共享**：一个 UoW 只属于一个协程。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextvars import Token
from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from autumn_backend.config import Settings, get_settings
from autumn_backend.io_boundary import active_uows
from autumn_backend.observability.logging import get_logger
from autumn_backend.repositories.bundle import Repositories
from autumn_backend.repositories.constraints import database_errors

logger = get_logger(__name__)


def create_engine(settings: Settings | None = None) -> AsyncEngine:
    """构造异步引擎。

    数据库时间统一 UTC：由连接的 ``timezone=UTC`` 会话参数保证，
    业务时间语义（如每日额度按 Asia/Shanghai 00:00 切分）在 service 层换算。
    """
    resolved = settings or get_settings()
    return create_async_engine(
        resolved.async_database_url,
        echo=resolved.db_echo,
        pool_size=resolved.db_pool_size,
        max_overflow=resolved.db_max_overflow,
        pool_timeout=resolved.db_pool_timeout,
        pool_recycle=resolved.db_pool_recycle,
        pool_pre_ping=True,
        connect_args={"server_settings": {"timezone": "UTC"}},
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """会话工厂：``expire_on_commit=False``，避免 commit 后访问属性触发隐式 I/O。"""
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


class UnitOfWork:
    """一个 UoW = 一个数据库事务。

    用法::

        async with uow_factory() as uow:
            ...  # 无异常 -> commit；有异常 -> rollback

    进入时显式开启事务，并绑定所有 Repository。实例只可进入一次，
    且只能由进入它的 asyncio Task 使用；工厂可跨 Task 共享。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self._finished = False
        self._entered = False
        self._owner_task: asyncio.Task[object] | None = None
        self._repositories: Repositories | None = None
        self._boundary_token: Token[int] | None = None

    # ------------------------------------------------------------- 生命周期 --
    async def __aenter__(self) -> Self:
        if self._entered:
            raise RuntimeError("UnitOfWork 不可重复进入；请从工厂创建新实例")
        self._entered = True
        self._owner_task = asyncio.current_task()
        self._session = self._session_factory()
        try:
            await self._session.begin()
            self._boundary_token = active_uows.set(active_uows.get() + 1)
            self._repositories = Repositories.bind(self._session, self._assert_active)
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if exc_type is not None:
                await self.rollback()
            else:
                try:
                    await self.commit()
                except BaseException:
                    await self.rollback()
                    raise
        finally:
            await self.close()

    # --------------------------------------------------------------- 操作 ----
    @property
    def session(self) -> AsyncSession:
        """当前事务的会话。UoW 之外的访问是编程错误，直接失败。"""
        self._assert_active()
        assert self._session is not None
        return self._session

    @property
    def repositories(self) -> Repositories:
        self._assert_active()
        assert self._repositories is not None
        return self._repositories

    def _assert_owner(self) -> None:
        if self._owner_task is not asyncio.current_task():
            raise RuntimeError("UnitOfWork 不可跨并发 Task 共享")

    def _assert_active(self) -> None:
        self._assert_owner()
        if self._session is None or self._finished:
            raise RuntimeError("UnitOfWork 的事务未开启或已经结束")

    @property
    def finished(self) -> bool:
        """是否已经 commit 或 rollback；用于断言"外部 I/O 前已结束 UoW"。"""
        return self._finished

    async def commit(self) -> None:
        self._assert_owner()
        if self._session is None or self._finished:
            return
        with database_errors():
            await self._session.commit()
        self._finished = True

    async def rollback(self) -> None:
        self._assert_owner()
        if self._session is None or self._finished:
            return
        await self._session.rollback()
        self._finished = True

    async def close(self) -> None:
        self._assert_owner()
        try:
            if self._session is not None:
                await self._session.close()
                self._session = None
                self._repositories = None
                self._finished = True
        finally:
            if self._boundary_token is not None:
                active_uows.reset(self._boundary_token)
                self._boundary_token = None


class UnitOfWorkFactory:
    """UoW 工厂：``async with uow_factory() as uow`` 每次开启一个新事务。

    工厂本身可被多个协程并发使用，但每次调用都创建**独立的**会话。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        uow_class: type[UnitOfWork] = UnitOfWork,
    ) -> None:
        self._session_factory = session_factory
        self._uow_class = uow_class

    def __call__(self) -> UnitOfWork:
        return self._uow_class(self._session_factory)

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory


async def run_in_uow[T](
    factory: UnitOfWorkFactory,
    operation: Callable[[UnitOfWork], Awaitable[T]],
) -> T:
    """在新建 UoW 中执行一次操作并提交。

    需要乐观锁重试时，必须调用本函数**多次**（每次新事务），
    不得在同一个已中毒的事务里重试。
    """
    async with factory() as uow:
        return await operation(uow)

"""异步数据库引擎、会话工厂与最小 Unit of Work。

UoW 原则（架构文档 §4.1 / Repository 文档 §4）：
- 一个原子数据库阶段 = 一个 UoW；退出时统一 commit 或 rollback。
- Repository 内允许 flush，**禁止 commit**（阶段 B1 的 RepositoryBase 强制）。
- 外部 I/O 前必须结束当前 UoW；外部 I/O 后需要落库时开启新 UoW。
- ``AsyncSession`` **不跨并发协程共享**：一个 UoW 只属于一个协程。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from autumn_backend.config import Settings, get_settings
from autumn_backend.observability.logging import get_logger

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

    构造 Repository 的职责落在阶段 B1 的具体 UoW 子类上；
    本类只保证事务边界、回滚语义与统一释放会话。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self._finished = False

    # ------------------------------------------------------------- 生命周期 --
    async def __aenter__(self) -> Self:
        self._session = self._session_factory()
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
                await self.commit()
        finally:
            await self.close()

    # --------------------------------------------------------------- 操作 ----
    @property
    def session(self) -> AsyncSession:
        """当前事务的会话。UoW 之外的访问是编程错误，直接失败。"""
        if self._session is None:
            raise RuntimeError("UnitOfWork 尚未进入；请使用 `async with uow_factory() as uow:`")
        return self._session

    @property
    def finished(self) -> bool:
        """是否已经 commit 或 rollback；用于断言"外部 I/O 前已结束 UoW"。"""
        return self._finished

    async def commit(self) -> None:
        if self._session is None or self._finished:
            return
        await self._session.commit()
        self._finished = True

    async def rollback(self) -> None:
        if self._session is None or self._finished:
            return
        await self._session.rollback()
        self._finished = True

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


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

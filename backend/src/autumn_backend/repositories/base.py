"""事务内的数据访问能力；提交与回滚只由 UoW 执行。"""

from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession


class RepositoryBase:
    """薄基类，不假设主键类型，也不暴露通用写操作。"""

    def __init__(
        self, session: AsyncSession, *, access_guard: Callable[[], None] | None = None
    ) -> None:
        self._session = session
        self._access_guard = access_guard

    @property
    def session(self) -> AsyncSession:
        if self._access_guard is not None:
            self._access_guard()
        return self._session

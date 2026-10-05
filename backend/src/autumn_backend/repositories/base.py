"""事务内的数据访问能力；提交与回滚只由 UoW 执行。"""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from autumn_backend.db.base import Base
from autumn_backend.errors import (
    ConflictError,
    InvalidInputError,
    NotFoundError,
    OptimisticLockError,
)
from autumn_backend.repositories.constraints import database_errors


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

    async def database_time(self) -> datetime:
        """读取数据库实际时间，避免事务开始时间被行锁等待固定。"""
        value: datetime = (await self.session.execute(select(func.clock_timestamp()))).scalar_one()
        return value


class UUIDRepository[T: Base](RepositoryBase):
    """只为 UUID 主键表提供读取与行锁，不用于 settings 或复合主键表。"""

    model: type[T]

    async def get(self, object_id: UUID) -> T | None:
        return await self.session.get(self.model, object_id)

    async def get_or_raise(self, object_id: UUID) -> T:
        record = await self.get(object_id)
        if record is None:
            raise NotFoundError("对象不存在")
        return record

    async def get_for_update(self, object_id: UUID) -> T | None:
        statement = (
            select(self.model)
            .where(self.model.__table__.c.id == object_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def get_for_update_or_raise(self, object_id: UUID) -> T:
        record = await self.get_for_update(object_id)
        if record is None:
            raise NotFoundError("对象不存在")
        return record


class VersionedRepository[T: Base](UUIDRepository[T]):
    """受保护的单语句 CAS；具体 Repository 以领域方法和字段白名单开放。"""

    mutable_fields: frozenset[str] = frozenset()

    async def _update_versioned(
        self, object_id: UUID, expected_version: int, changes: dict[str, object]
    ) -> T:
        if expected_version < 0 or not changes or not changes.keys() <= self.mutable_fields:
            raise InvalidInputError("不允许的版本更新")
        table = self.model.__table__
        statement = (
            update(self.model)
            .where(table.c.id == object_id, table.c.version == expected_version)
            .values(**changes, version=expected_version + 1)
            .returning(self.model)
            .execution_options(populate_existing=True)
        )
        with database_errors():
            record = (await self.session.execute(statement)).scalar_one_or_none()
        if record is None:
            raise OptimisticLockError("对象已变化或不存在，请重新读取")
        return record


class AppendOnlyRepository(RepositoryBase):
    """仅供专用追加方法继承；没有通用 record/update/delete。"""


class ControlledMutableRepository[T: Base](UUIDRepository[T]):
    """状态机表的读能力；状态变更必须由具体 Repository 的命名方法提供。"""

    transition_fields: frozenset[str] = frozenset()

    async def _transition(
        self, object_id: UUID, expected_status: str, status: str, changes: dict[str, object]
    ) -> T:
        if not changes.keys() <= self.transition_fields:
            raise InvalidInputError("不允许的状态更新字段")
        table = self.model.__table__
        statement = (
            update(self.model)
            .where(table.c.id == object_id, table.c.status == expected_status)
            .values(**changes, status=status, version=table.c.version + 1)
            .returning(self.model)
            .execution_options(populate_existing=True)
        )
        with database_errors():
            record = (await self.session.execute(statement)).scalar_one_or_none()
        if record is None:
            raise ConflictError("对象状态已变化或不存在")
        return record

"""同一个 UoW 中的 Repository 使用同一个 AsyncSession。"""

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from autumn_backend.repositories.identity import SettingRepository, UserRepository


@dataclass(frozen=True, slots=True)
class Repositories:
    users: UserRepository
    settings: SettingRepository

    @classmethod
    def bind(cls, session: AsyncSession, access_guard: Callable[[], None]) -> "Repositories":
        return cls(
            users=UserRepository(session, access_guard=access_guard),
            settings=SettingRepository(session, access_guard=access_guard),
        )

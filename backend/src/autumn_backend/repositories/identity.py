"""身份与设置读取；身份修改由后续专用方法承接。"""

from uuid import UUID

from autumn_backend.db.models import Setting, User
from autumn_backend.repositories.base import RepositoryBase


class UserRepository(RepositoryBase):
    async def get(self, user_id: UUID) -> User | None:
        return await self.session.get(User, user_id)


class SettingRepository(RepositoryBase):
    async def get(self, key: str) -> Setting | None:
        return await self.session.get(Setting, key)

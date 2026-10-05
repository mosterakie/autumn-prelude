"""身份与设置读取；身份修改由后续专用方法承接。"""

from autumn_backend.db.models import Setting, User
from autumn_backend.repositories.base import RepositoryBase, UUIDRepository


class UserRepository(UUIDRepository[User]):
    model = User


class SettingRepository(RepositoryBase):
    async def get(self, key: str) -> Setting | None:
        return await self.session.get(Setting, key)

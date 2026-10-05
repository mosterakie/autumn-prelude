"""身份与设置读取；身份修改由后续专用方法承接。"""

from uuid import UUID

from sqlalchemy import select, update

from autumn_backend.db.models import AuthSession, Setting, User
from autumn_backend.errors import ConfigurationError
from autumn_backend.repositories.base import RepositoryBase, UUIDRepository


class UserRepository(UUIDRepository[User]):
    model = User


class AuthSessionRepository(UUIDRepository[AuthSession]):
    model = AuthSession

    async def for_user_for_update(self, session_id: UUID, user_id: UUID) -> AuthSession | None:
        return (
            await self.session.execute(
                select(AuthSession)
                .where(AuthSession.id == session_id, AuthSession.user_id == user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()


class SettingRepository(RepositoryBase):
    async def get(self, key: str) -> Setting | None:
        return await self.session.get(Setting, key)

    async def _acl_setting(self, *, lock: bool = False) -> Setting:
        statement = select(Setting).where(Setting.key == "content_acl_epoch")
        if lock:
            statement = statement.with_for_update()
        setting = (
            await self.session.execute(statement.execution_options(populate_existing=True))
        ).scalar_one_or_none()
        if setting is None or type(setting.value) is not int or setting.value < 0:
            raise ConfigurationError("全站权限版本未初始化或损坏")
        return setting

    async def get_acl_epoch(self) -> int:
        setting = await self._acl_setting()
        return int(setting.value)

    async def bump_acl_epoch(self) -> int:
        setting = await self._acl_setting(lock=True)
        value = int(setting.value) + 1
        await self.session.execute(
            update(Setting)
            .where(Setting.key == setting.key)
            .values(value=value, version=Setting.version + 1)
            .execution_options(synchronize_session=False)
        )
        return value

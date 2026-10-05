"""身份与设置读取；身份修改由后续专用方法承接。"""

from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from autumn_backend.db.enums import UserRole, UserStatus
from autumn_backend.db.models import AuthSession, Setting, User
from autumn_backend.errors import ConfigurationError
from autumn_backend.repositories.base import RepositoryBase, UUIDRepository


class UserRepository(UUIDRepository[User]):
    model = User

    async def by_email(self, email: str) -> User | None:
        return (
            await self.session.execute(
                select(User)
                .where(User.email_normalized == email)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    async def register(
        self, *, email: str, password_hash: str, display_name: str | None
    ) -> User | None:
        return (
            await self.session.execute(
                insert(User)
                .values(
                    email_normalized=email,
                    password_hash=password_hash,
                    display_name=display_name,
                    role=UserRole.MEMBER,
                    status=UserStatus.PENDING_VERIFICATION,
                )
                .on_conflict_do_nothing(constraint="uq_users_email_normalized")
                .returning(User)
            )
        ).scalar_one_or_none()

    async def lock_owner_bootstrap(self) -> bool:
        await self.session.execute(select(func.pg_advisory_xact_lock(426217019)))
        return (
            await self.session.execute(select(User.id).where(User.role == UserRole.OWNER).limit(1))
        ).first() is None


class AuthSessionRepository(UUIDRepository[AuthSession]):
    model = AuthSession

    async def by_token_hash(self, digest: str) -> AuthSession | None:
        return (
            await self.session.execute(
                select(AuthSession)
                .where(AuthSession.token_hash == digest)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    async def next_csrf_version(self, user_id: UUID) -> int:
        maximum = (
            await self.session.execute(
                select(func.max(AuthSession.csrf_version)).where(AuthSession.user_id == user_id)
            )
        ).scalar_one()
        return int(maximum or 0) + 1

    async def revoke_all(self, user_id: UUID) -> None:
        await self.session.execute(
            update(AuthSession)
            .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
            .values(
                revoked_at=func.clock_timestamp(),
                csrf_version=AuthSession.csrf_version + 1,
                version=AuthSession.version + 1,
            )
            .execution_options(synchronize_session=False)
        )

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
        return (
            await self.session.execute(
                select(Setting).where(Setting.key == key).execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

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

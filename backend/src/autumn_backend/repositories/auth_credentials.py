"""认证一次性令牌与站长因子；调用方遵循 user → session → token/factor 顺序。"""

from uuid import UUID

from sqlalchemy import func, select, update

from autumn_backend.db.enums import AuthTokenPurpose, JobStatus
from autumn_backend.db.models import AdminFactor, AuthToken, Job
from autumn_backend.repositories.base import RepositoryBase


class AuthCredentialRepository(RepositoryBase):
    async def invalidate_other_tokens(
        self, user_id: UUID, purpose: AuthTokenPurpose, consumed_id: UUID
    ) -> None:
        identifiers = (
            (
                await self.session.execute(
                    update(AuthToken)
                    .where(
                        AuthToken.user_id == user_id,
                        AuthToken.purpose == purpose,
                        AuthToken.id != consumed_id,
                        AuthToken.consumed_at.is_(None),
                    )
                    .values(consumed_at=func.clock_timestamp(), version=AuthToken.version + 1)
                    .returning(AuthToken.id)
                    .execution_options(synchronize_session=False)
                )
            )
            .scalars()
            .all()
        )
        for identifier in identifiers:
            await self.scrub_mail(identifier)

    async def token(
        self, digest: str, purpose: AuthTokenPurpose, *, lock: bool = False
    ) -> AuthToken | None:
        query = select(AuthToken).where(
            AuthToken.token_hash == digest, AuthToken.purpose == purpose
        )
        if lock:
            query = query.with_for_update()
        return (
            await self.session.execute(query.execution_options(populate_existing=True))
        ).scalar_one_or_none()

    async def factor(self, user_id: UUID, *, lock: bool = False) -> AdminFactor | None:
        query = select(AdminFactor).where(AdminFactor.user_id == user_id)
        if lock:
            query = query.with_for_update()
        return (
            await self.session.execute(query.execution_options(populate_existing=True))
        ).scalar_one_or_none()

    async def scrub_mail(self, token_id: UUID) -> None:
        predicate = (Job.kind == "auth.email") & (Job.payload["token_id"].astext == str(token_id))
        await self.session.execute(
            update(Job)
            .where(predicate)
            .values(
                payload=Job.payload.op("-")("ciphertext"),
                version=Job.version + 1,
            )
            .execution_options(synchronize_session=False)
        )
        await self.session.execute(
            update(Job)
            .where(predicate, Job.status == JobStatus.QUEUED)
            .values(
                status=JobStatus.CANCELLED,
            )
            .execution_options(synchronize_session=False)
        )

"""同一个 UoW 中的 Repository 使用同一个 AsyncSession。"""

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from autumn_backend.repositories.identity import SettingRepository, UserRepository
from autumn_backend.repositories.jobs import JobRepository
from autumn_backend.repositories.provider_calls import ProviderCallRepository
from autumn_backend.repositories.quota import QuotaBucketRepository, QuotaReservationRepository
from autumn_backend.repositories.runs import RunEventRepository, RunRepository


@dataclass(frozen=True, slots=True)
class Repositories:
    users: UserRepository
    settings: SettingRepository
    runs: RunRepository
    run_events: RunEventRepository
    quota_buckets: QuotaBucketRepository
    quota_reservations: QuotaReservationRepository
    jobs: JobRepository
    provider_calls: ProviderCallRepository

    @classmethod
    def bind(cls, session: AsyncSession, access_guard: Callable[[], None]) -> "Repositories":
        return cls(
            users=UserRepository(session, access_guard=access_guard),
            settings=SettingRepository(session, access_guard=access_guard),
            runs=RunRepository(session, access_guard=access_guard),
            run_events=RunEventRepository(session, access_guard=access_guard),
            quota_buckets=QuotaBucketRepository(session, access_guard=access_guard),
            quota_reservations=QuotaReservationRepository(session, access_guard=access_guard),
            jobs=JobRepository(session, access_guard=access_guard),
            provider_calls=ProviderCallRepository(session, access_guard=access_guard),
        )

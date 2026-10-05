"""同一个 UoW 中的 Repository 使用同一个 AsyncSession。"""

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from autumn_backend.repositories.actions import ActionRepository
from autumn_backend.repositories.audit import AuditEventRepository
from autumn_backend.repositories.comments import CommentRepository
from autumn_backend.repositories.conversations import ConversationRepository
from autumn_backend.repositories.identity import (
    AuthSessionRepository,
    SettingRepository,
    UserRepository,
)
from autumn_backend.repositories.jobs import JobRepository
from autumn_backend.repositories.memories import MemoryRepository
from autumn_backend.repositories.provider_calls import ProviderCallRepository
from autumn_backend.repositories.publications import PublicationRepository
from autumn_backend.repositories.quota import QuotaBucketRepository, QuotaReservationRepository
from autumn_backend.repositories.resources import ResourceRepository
from autumn_backend.repositories.runs import RunEventRepository, RunRepository


@dataclass(frozen=True, slots=True)
class Repositories:
    users: UserRepository
    auth_sessions: AuthSessionRepository
    actions: ActionRepository
    settings: SettingRepository
    runs: RunRepository
    run_events: RunEventRepository
    quota_buckets: QuotaBucketRepository
    quota_reservations: QuotaReservationRepository
    jobs: JobRepository
    provider_calls: ProviderCallRepository
    comments: CommentRepository
    publications: PublicationRepository
    resources: ResourceRepository
    conversations: ConversationRepository
    memories: MemoryRepository
    audit_events: AuditEventRepository

    @classmethod
    def bind(cls, session: AsyncSession, access_guard: Callable[[], None]) -> "Repositories":
        return cls(
            users=UserRepository(session, access_guard=access_guard),
            auth_sessions=AuthSessionRepository(session, access_guard=access_guard),
            actions=ActionRepository(session, access_guard=access_guard),
            settings=SettingRepository(session, access_guard=access_guard),
            runs=RunRepository(session, access_guard=access_guard),
            run_events=RunEventRepository(session, access_guard=access_guard),
            quota_buckets=QuotaBucketRepository(session, access_guard=access_guard),
            quota_reservations=QuotaReservationRepository(session, access_guard=access_guard),
            jobs=JobRepository(session, access_guard=access_guard),
            provider_calls=ProviderCallRepository(session, access_guard=access_guard),
            comments=CommentRepository(session, access_guard=access_guard),
            publications=PublicationRepository(session, access_guard=access_guard),
            resources=ResourceRepository(session, access_guard=access_guard),
            conversations=ConversationRepository(session, access_guard=access_guard),
            memories=MemoryRepository(session, access_guard=access_guard),
            audit_events=AuditEventRepository(session, access_guard=access_guard),
        )

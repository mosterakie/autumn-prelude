"""软删除会话的派生摘要失效，业务记录仍按永久策略保留。"""

from uuid import UUID

from sqlalchemy import update

from autumn_backend.db.enums import SummaryStatus
from autumn_backend.db.models import ConversationSummary
from autumn_backend.db.session import UnitOfWorkFactory
from autumn_backend.errors import ConflictError
from autumn_backend.jobs.queue import LeasedJob


class ConversationCleanupService:
    def __init__(self, uows: UnitOfWorkFactory) -> None:
        self.uows = uows

    async def execute(self, leased: LeasedJob) -> None:
        async with self.uows() as uow:
            probe = await uow.repositories.jobs.get_or_raise(leased.id)
            if probe.kind != "conversation.cleanup" or probe.actor_id is None:
                raise ConflictError("会话收敛绑定无效")
            try:
                conversation_id = UUID((probe.payload or {})["conversation_id"])
            except (KeyError, ValueError, TypeError) as error:
                raise ConflictError("会话收敛载荷无效") from error
            await uow.repositories.users.get_for_update_or_raise(probe.actor_id)
            conversation = await uow.repositories.conversations.for_user_for_update(
                conversation_id, probe.actor_id
            )
            await uow.repositories.jobs.require_lease(leased.id, leased.token)
            if conversation is None or conversation.deleted_at is None:
                raise ConflictError("仅收敛已删除的本人会话")
            await uow.session.execute(
                update(ConversationSummary)
                .where(
                    ConversationSummary.conversation_id == conversation.id,
                    ConversationSummary.status == SummaryStatus.ACTIVE,
                )
                .values(status=SummaryStatus.STALE, version=ConversationSummary.version + 1)
            )
            await uow.repositories.jobs.finish(
                leased.id, leased.token, result={"conversation_id": str(conversation.id)}
            )

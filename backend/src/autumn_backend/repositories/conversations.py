"""会话的固定模式和可变元数据。"""

from uuid import UUID

from sqlalchemy import func

from autumn_backend.db.enums import ConversationMode
from autumn_backend.db.models import Conversation
from autumn_backend.errors import ConflictError
from autumn_backend.repositories.base import VersionedRepository
from autumn_backend.repositories.constraints import database_errors


class ConversationRepository(VersionedRepository[Conversation]):
    model = Conversation
    mutable_fields = frozenset({"title", "deleted_at"})

    async def create(
        self, *, user_id: UUID, mode: ConversationMode, title: str = ""
    ) -> Conversation:
        conversation = Conversation(user_id=user_id, mode=mode, title=title)
        with database_errors():
            self.session.add(conversation)
            await self.session.flush()
        return conversation

    async def rename(
        self, conversation_id: UUID, *, expected_version: int, title: str
    ) -> Conversation:
        conversation = await self.get_for_update_or_raise(conversation_id)
        if conversation.deleted_at is not None:
            raise ConflictError("会话已删除")
        return await self._update_versioned(conversation_id, expected_version, {"title": title})

    async def soft_delete(self, conversation_id: UUID, *, expected_version: int) -> Conversation:
        return await self._update_versioned(
            conversation_id, expected_version, {"deleted_at": func.clock_timestamp()}
        )

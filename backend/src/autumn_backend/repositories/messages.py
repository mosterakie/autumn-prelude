"""用户输入专用追加；不提供完成消息的通用正文修改接口。"""

from uuid import UUID

from sqlalchemy import select, update

from autumn_backend.db.enums import MessageRole, MessageStatus
from autumn_backend.db.models import Conversation, Message
from autumn_backend.errors import IdempotencyConflictError, InvalidInputError, NotFoundError
from autumn_backend.repositories.base import AppendOnlyRepository
from autumn_backend.repositories.constraints import database_errors


class MessageRepository(AppendOnlyRepository):
    async def append_assistant_complete(
        self, *, conversation_id: UUID, run_id: UUID, client_message_id: UUID, body: str
    ) -> Message:
        if not body.strip() or len(body) > 64000:
            raise InvalidInputError("最终回复必须非空且不超过 64000 字符")
        existing = await self.by_client_id(conversation_id, client_message_id)
        if existing is not None:
            if (
                existing.run_id != run_id
                or existing.role is not MessageRole.ASSISTANT
                or existing.body_text != body
                or existing.status is not MessageStatus.COMPLETE
            ):
                raise IdempotencyConflictError("最终消息标识冲突")
            return existing
        with database_errors():
            sequence = (
                await self.session.execute(
                    update(Conversation)
                    .where(Conversation.id == conversation_id)
                    .values(next_message_seq=Conversation.next_message_seq + 1)
                    .returning(Conversation.next_message_seq - 1)
                    .execution_options(synchronize_session=False)
                )
            ).scalar_one_or_none()
            if sequence is None:
                raise NotFoundError("会话不存在")
            record = Message(
                conversation_id=conversation_id,
                run_id=run_id,
                seq=sequence,
                role=MessageRole.ASSISTANT,
                status=MessageStatus.COMPLETE,
                client_message_id=client_message_id,
                body_text=body,
            )
            self.session.add(record)
            await self.session.flush()
        return record

    async def by_client_id(self, conversation_id: UUID, client_message_id: UUID) -> Message | None:
        return (
            await self.session.execute(
                select(Message).where(
                    Message.conversation_id == conversation_id,
                    Message.client_message_id == client_message_id,
                )
            )
        ).scalar_one_or_none()

    async def append_user(
        self, *, conversation_id: UUID, run_id: UUID, client_message_id: UUID, body: str
    ) -> Message:
        if not body.strip():
            raise InvalidInputError("消息不能为空")
        # 调用方持有 conversation 锁，DB 唯一约束仍是最后仲裁。
        if await self.by_client_id(conversation_id, client_message_id) is not None:
            raise IdempotencyConflictError("消息标识已用于另一请求")
        with database_errors():
            sequence = (
                await self.session.execute(
                    update(Conversation)
                    .where(Conversation.id == conversation_id)
                    .values(next_message_seq=Conversation.next_message_seq + 1)
                    .returning(Conversation.next_message_seq - 1)
                    .execution_options(synchronize_session=False)
                )
            ).scalar_one_or_none()
            if sequence is None:
                raise NotFoundError("会话不存在")
            record = Message(
                conversation_id=conversation_id,
                run_id=run_id,
                seq=sequence,
                role=MessageRole.USER,
                status=MessageStatus.COMPLETE,
                client_message_id=client_message_id,
                body_text=body,
            )
            self.session.add(record)
            await self.session.flush()
        return record

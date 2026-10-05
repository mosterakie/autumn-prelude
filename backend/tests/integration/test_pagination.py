from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from autumn_backend.db.enums import ConversationMode
from autumn_backend.db.models import Conversation
from autumn_backend.errors import InvalidInputError
from autumn_backend.repositories.conversations import ConversationRepository

pytestmark = pytest.mark.integration


async def test_equal_timestamps_stable_order_insert_between_pages_and_tenant_scope(
    session: AsyncSession, make_user: object
) -> None:
    user = make_user()
    other = make_user(email="other@example.com")
    await session.flush()
    moment = datetime(2026, 10, 5, tzinfo=UTC)
    rows = [
        Conversation(
            id=UUID(int=i), user_id=user.id, mode=ConversationMode.PUBLIC, created_at=moment
        )
        for i in range(1, 6)
    ]
    session.add_all(rows)
    session.add(Conversation(user_id=other.id, mode=ConversationMode.PUBLIC, created_at=moment))
    await session.flush()
    repository = ConversationRepository(session)
    first = await repository.for_user(user.id, limit=2)
    assert [row.id.int for row in first.items] == [5, 4]
    assert first.next_cursor is not None
    session.add(
        Conversation(
            user_id=user.id, mode=ConversationMode.PUBLIC, created_at=moment + timedelta(days=1)
        )
    )
    await session.flush()
    second = await repository.for_user(user.id, limit=2, cursor=first.next_cursor)
    third = await repository.for_user(user.id, limit=2, cursor=second.next_cursor)
    assert [row.id.int for row in second.items] == [3, 2]
    assert [row.id.int for row in third.items] == [1] and third.next_cursor is None
    assert all(row.user_id == user.id for row in [*first.items, *second.items, *third.items])
    with pytest.raises(InvalidInputError):
        await repository.for_user(user.id, limit=0)

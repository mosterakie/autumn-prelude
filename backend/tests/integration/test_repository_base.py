"""数据库仲裁 CAS，旧版本不能覆盖已提交的新内容。"""

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from autumn_backend.db.enums import ConversationMode
from autumn_backend.db.models import Conversation
from autumn_backend.errors import InvalidInputError, NotFoundError, OptimisticLockError
from autumn_backend.repositories.base import VersionedRepository

pytestmark = pytest.mark.integration


class ConversationProbe(VersionedRepository[Conversation]):
    model = Conversation
    mutable_fields = frozenset({"title"})


async def test_uuid_reads_and_missing_lock(session: AsyncSession, make_user: object) -> None:
    repository = ConversationProbe(session)
    assert await repository.get(uuid4()) is None
    assert await repository.get_for_update(uuid4()) is None
    with pytest.raises(NotFoundError):
        await repository.get_or_raise(uuid4())
    with pytest.raises(NotFoundError):
        await repository.get_for_update_or_raise(uuid4())


async def test_cas_refreshes_identity_map_and_rejects_stale_version(
    session: AsyncSession, make_user: object, make_conversation: object
) -> None:
    user = make_user()
    await session.flush()
    conversation = await make_conversation(user.id, mode=ConversationMode.PUBLIC)
    repository = ConversationProbe(session)
    old_version = conversation.version
    changed = await repository._update_versioned(conversation.id, old_version, {"title": "新标题"})
    assert changed is conversation
    assert conversation.version == old_version + 1
    assert (await repository.get_for_update_or_raise(conversation.id)).title == "新标题"
    with pytest.raises(OptimisticLockError):
        await repository._update_versioned(conversation.id, old_version, {"title": "旧客户端"})
    assert conversation.title == "新标题"
    with pytest.raises(OptimisticLockError):
        await repository._update_versioned(uuid4(), 0, {"title": "不存在"})
    for changes in ({"version": 99}, {"user_id": uuid4()}, {}):
        with pytest.raises(InvalidInputError):
            await repository._update_versioned(conversation.id, conversation.version, changes)

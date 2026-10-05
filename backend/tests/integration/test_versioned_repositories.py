from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from autumn_backend.db.enums import ConversationMode, MemoryKind, ResourceKind
from autumn_backend.db.models import ResourceVersion
from autumn_backend.errors import ConflictError, OptimisticLockError
from autumn_backend.repositories.conversations import ConversationRepository
from autumn_backend.repositories.memories import MemoryRepository
from autumn_backend.repositories.publications import PublicationProjection, PublicationRepository
from autumn_backend.repositories.resources import ResourceRepository, RevisionDraft

pytestmark = pytest.mark.integration


async def test_revision_keeps_published_snapshot_private_and_lifecycle_versions(
    session: AsyncSession, make_user: object
) -> None:
    user = make_user()
    await session.flush()
    resources, publications = ResourceRepository(session), PublicationRepository(session)
    resource = await resources.create(
        owner_id=user.id,
        kind=ResourceKind.ARTICLE,
        slug=str(uuid4()),
        draft=RevisionDraft(title="原题", body_text="原文"),
    )
    old_revision_id = resource.current_revision_id
    await publications.publish_under_resource_lock(
        resource_id=resource.id,
        revision_id=old_revision_id,
        expected_version=0,
        expected_acl_version=0,
        published_by=user.id,
        projection=PublicationProjection(frozenset({"title", "body"})),
    )
    changed = await resources.revise(
        resource.id,
        expected_version=0,
        created_by=user.id,
        draft=RevisionDraft(title="新私人标题", body_text="新私人正文"),
    )
    assert (changed.version, changed.acl_version) == (1, 1)
    assert (await session.get(ResourceVersion, old_revision_id)).body_text == "原文"
    assert (await publications.public_by_slug(resource.slug)).public_body == "原文"
    archived = await resources.set_archived(
        resource.id, expected_version=1, expected_acl_version=1, archived=True
    )
    assert (archived.version, archived.acl_version) == (2, 2)
    assert await publications.public_by_slug(resource.slug) is None
    restored = await resources.set_archived(
        resource.id, expected_version=2, expected_acl_version=2, archived=False
    )
    assert (restored.version, restored.acl_version) == (3, 3)
    assert await publications.public_by_slug(resource.slug) is None
    deleted = await resources.soft_delete(resource.id, expected_version=3, expected_acl_version=3)
    assert (deleted.version, deleted.acl_version) == (3, 4)
    restored = await resources.restore(resource.id, expected_version=3, expected_acl_version=4)
    assert (restored.version, restored.acl_version) == (3, 5)
    with pytest.raises(OptimisticLockError):
        await resources.revise(
            resource.id, expected_version=0, created_by=user.id, draft=RevisionDraft(title="旧写入")
        )


async def test_conversation_and_memory_cas(
    session: AsyncSession, make_user: object, make_conversation: object, make_run: object
) -> None:
    user = make_user()
    other = make_user(email="other@example.com")
    await session.flush()
    conversations = ConversationRepository(session)
    conversation = await conversations.create(user_id=user.id, mode=ConversationMode.OWNER)
    await conversations.rename(conversation.id, expected_version=0, title="新名称")
    assert conversation.mode == ConversationMode.OWNER and conversation.version == 1
    with pytest.raises(OptimisticLockError):
        await conversations.rename(conversation.id, expected_version=0, title="旧名称")
    memories = MemoryRepository(session)
    foreign_run = await make_run(await make_conversation(other.id))
    with pytest.raises(ConflictError):
        await memories.create(
            user_id=user.id, kind=MemoryKind.FACT, content_text="私密", origin_run_id=foreign_run.id
        )
    memory = await memories.create(
        user_id=user.id, kind=MemoryKind.PREFERENCE, content_text="中文回答"
    )
    await memories.revise(
        memory.id, expected_version=0, kind=MemoryKind.PREFERENCE, content_text="简短中文"
    )
    assert memory.version == 1
    with pytest.raises(OptimisticLockError):
        await memories.revise(
            memory.id, expected_version=0, kind=MemoryKind.FACT, content_text="旧修改"
        )
    await memories.soft_delete(memory.id, expected_version=1)
    with pytest.raises(ConflictError):
        await memories.revise(
            memory.id, expected_version=2, kind=MemoryKind.FACT, content_text="删除后写入"
        )

from uuid import uuid4

import pytest
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from autumn_backend.db.models import Comment
from autumn_backend.errors import ConflictError
from autumn_backend.repositories.comments import CommentRepository, request_hash

pytestmark = pytest.mark.integration


async def test_semantic_hash_and_original_identity_survive_edit(
    session: AsyncSession, make_user: object
) -> None:
    user = make_user()
    await session.flush()
    repository = CommentRepository(session)
    args = dict(author_id=user.id, client_id=uuid4(), body=' 秋序\r\nＡ\t"\\ ')
    first = await repository.create_or_get(**args)
    assert first.record.body == ' 秋序\nＡ\t"\\ '
    assert first.record.request_hash == request_hash(None, None, args["body"])
    assert not (await repository.create_or_get(**(args | {"body": first.record.body}))).created
    for body in (first.record.body.strip(), ' 秋序\nA\t"\\ '):
        with pytest.raises(ConflictError):
            await repository.create_or_get(**(args | {"body": body}))
    await session.execute(
        update(Comment).where(Comment.id == first.record.id).values(body="后来修改")
    )
    assert not (await repository.create_or_get(**args)).created


async def test_parent_invariants_include_guestbook_null_resource(
    session: AsyncSession, make_user: object, make_resource: object
) -> None:
    user = make_user()
    await session.flush()
    repository = CommentRepository(session)
    parent = (
        await repository.create_or_get(author_id=user.id, client_id=uuid4(), body="parent")
    ).record
    reply = (
        await repository.create_or_get(
            author_id=user.id, client_id=uuid4(), body="reply", parent_id=parent.id
        )
    ).record
    resource = await make_resource(user.id)
    for parent_id, resource_id in ((uuid4(), None), (reply.id, None), (parent.id, resource.id)):
        with pytest.raises(ConflictError):
            await repository.create_or_get(
                author_id=user.id,
                client_id=uuid4(),
                body="invalid",
                parent_id=parent_id,
                resource_id=resource_id,
            )
    await session.execute(
        update(Comment).where(Comment.id == parent.id).values(deleted_at=func.clock_timestamp())
    )
    with pytest.raises(ConflictError):
        await repository.create_or_get(
            author_id=user.id, client_id=uuid4(), body="invalid", parent_id=parent.id
        )


@pytest.mark.parametrize("body", ["\r\n", "空 格 Ａ", '"\\\t\b\f\n', "emoji 🌸", "\u2028\u2029"])
async def test_database_hash_matches_python_for_legacy_inserts(
    session: AsyncSession, make_user: object, body: str
) -> None:
    user = make_user()
    await session.flush()
    comment = Comment(author_id=user.id, client_id=uuid4(), body=body)
    session.add(comment)
    await session.flush()
    await session.refresh(comment)
    assert comment.request_hash == request_hash(None, None, body)

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from autumn_backend.db.enums import RunEventType
from autumn_backend.errors import ConflictError
from autumn_backend.repositories.constraints import ActiveRunConflictError
from autumn_backend.repositories.runs import RunEventRepository, RunRepository

pytestmark = pytest.mark.integration


async def test_replay_and_different_hash_and_busy_conversation(
    session: AsyncSession, make_user: object, make_conversation: object
) -> None:
    user = make_user()
    await session.flush()
    conversation = await make_conversation(user.id)
    repository = RunRepository(session)
    arguments = dict(
        user_id=user.id,
        conversation_id=conversation.id,
        idempotency_key="request-1",
        request_hash="hash-1",
        scope_epoch=0,
    )
    first = await repository.create_idempotent(**arguments)
    replay = await repository.create_idempotent(**arguments)
    assert first.created and not replay.created
    assert first.record.id == replay.record.id
    with pytest.raises(ConflictError):
        await repository.create_idempotent(**(arguments | {"request_hash": "other"}))
    with pytest.raises(ActiveRunConflictError):
        async with session.begin_nested():
            await repository.create_idempotent(**(arguments | {"idempotency_key": "request-2"}))


async def test_event_sequence_and_rollback_are_atomic(
    session: AsyncSession, make_user: object, make_conversation: object, make_run: object
) -> None:
    user = make_user()
    await session.flush()
    conversation = await make_conversation(user.id)
    run = await make_run(conversation)
    events = RunEventRepository(session)
    assert (await events.emit(run.id, RunEventType.RUN_STATUS, {"status": "queued"})).seq == 1
    with pytest.raises(ValueError):
        async with session.begin_nested():
            await events.emit(run.id, RunEventType.DONE)
            raise ValueError("rollback")
    assert (await events.emit(run.id, RunEventType.DONE)).seq == 2
    assert [event.seq for event in await events.after(run.id, 0)] == [1, 2]
    assert [event.seq for event in await events.after(run.id, 1)] == [2]

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from autumn_backend.db.enums import QuotaReservationStatus as Status
from autumn_backend.errors import ConflictError, InvalidInputError, QuotaExceededError
from autumn_backend.repositories.quota import QuotaBucketRepository, QuotaReservationRepository

pytestmark = pytest.mark.integration


@pytest.fixture
async def quota_case(
    session: AsyncSession, make_user: object, make_conversation: object, make_run: object
) -> tuple:
    user = make_user()
    await session.flush()
    conversation = await make_conversation(user.id)
    run = await make_run(conversation)
    start = datetime(2026, 10, 4, 16, tzinfo=UTC)
    buckets = QuotaBucketRepository(session)
    bucket = await buckets.get_or_create_for_update(user.id, start, start + timedelta(days=1))
    return user, run, bucket


async def test_reserve_replay_charge_refund_idempotent(
    session: AsyncSession, quota_case: tuple
) -> None:
    user, run, bucket = quota_case
    reservations = QuotaReservationRepository(session)
    args = dict(run_id=run.id, bucket_id=bucket.id, user_id=user.id, current_limit=10)
    assert (await reservations.reserve(**args)).created
    # 降低当前限额仍允许重放原预留，不重新占额。
    assert not (await reservations.reserve(**(args | {"current_limit": 0}))).created
    assert (bucket.used, bucket.reserved) == (0, 1)
    for _ in range(2):
        assert (await reservations.charge(run.id)).status == Status.CHARGED
    assert (bucket.used, bucket.reserved) == (1, 0)
    for _ in range(2):
        assert (await reservations.refund(run.id)).status == Status.REFUNDED
    assert (bucket.used, bucket.reserved) == (0, 0)
    assert (await reservations.charge(run.id)).status == Status.REFUNDED
    with pytest.raises(ConflictError):
        await reservations.release(run.id)


async def test_release_and_invalid_transitions(session: AsyncSession, quota_case: tuple) -> None:
    user, run, bucket = quota_case
    repository = QuotaReservationRepository(session)
    args = dict(run_id=run.id, bucket_id=bucket.id, user_id=user.id, current_limit=1)
    with pytest.raises(InvalidInputError):
        await repository.reserve(**args, amount=2)
    await repository.reserve(**args)
    with pytest.raises(ConflictError):
        await repository.refund(run.id)
    await repository.release(run.id)
    await repository.release(run.id)
    assert bucket.reserved == 0
    with pytest.raises(ConflictError):
        await repository.charge(run.id)


async def test_over_quota_rolls_back_new_reservation(
    session: AsyncSession, quota_case: tuple
) -> None:
    user, run, bucket = quota_case
    run_id, bucket_id = run.id, bucket.id
    repository = QuotaReservationRepository(session)
    with pytest.raises(QuotaExceededError):
        async with session.begin_nested():
            await repository.reserve(
                run_id=run_id, bucket_id=bucket_id, user_id=user.id, current_limit=0
            )
    assert await repository.for_run(run_id) is None
    bucket = await QuotaBucketRepository(session).get_for_update_or_raise(bucket_id)
    assert (bucket.used, bucket.reserved) == (0, 0)

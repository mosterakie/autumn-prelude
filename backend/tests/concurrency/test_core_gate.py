"""阶段 C 五项硬闸门和已实现模块的扩展并发回归。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from autumn_backend.db.enums import QuotaReservationStatus, RunEventType
from autumn_backend.db.models import Conversation, Job, QuotaBucket, QuotaReservation
from autumn_backend.db.session import UnitOfWork
from autumn_backend.errors import (
    ConflictError,
    LeaseLostError,
    OptimisticLockError,
    QuotaExceededError,
)
from autumn_backend.repositories.base import VersionedRepository
from tests.concurrency.conftest import ConcurrentCase, race

pytestmark = [pytest.mark.concurrency, pytest.mark.timeout(30)]
START = datetime(2026, 10, 4, 16, tzinfo=UTC)


@pytest.mark.parametrize("different_hash", [False, True])
async def test_same_run_idempotency_key(
    concurrent_case: ConcurrentCase, different_hash: bool
) -> None:
    case = concurrent_case

    async def create(uow: UnitOfWork, index: int) -> object:
        return await uow.repositories.runs.create_idempotent(
            user_id=case.user_id,
            conversation_id=case.conversation_ids[2],
            idempotency_key="same-key",
            request_hash=str(index) if different_hash else "same-hash",
            scope_epoch=0,
        )

    results = await race(case, create)
    if different_hash:
        assert sum(isinstance(result, ConflictError) for result in results) == 1
        assert sum(not isinstance(result, BaseException) for result in results) == 1
    else:
        assert all(not isinstance(result, BaseException) for result in results)
        assert results[0].record.id == results[1].record.id
        assert sum(result.created for result in results) == 1


async def test_first_bucket_created_once_for_two_runs(concurrent_case: ConcurrentCase) -> None:
    case = concurrent_case

    async def reserve(uow: UnitOfWork, index: int) -> object:
        bucket = await uow.repositories.quota_buckets.get_or_create_for_update(
            case.user_id, START, START + timedelta(days=1)
        )
        return await uow.repositories.quota_reservations.reserve(
            run_id=case.run_ids[index], bucket_id=bucket.id, user_id=case.user_id, current_limit=2
        )

    results = await race(case, reserve)
    assert all(not isinstance(result, BaseException) for result in results)
    async with case.uows() as uow:
        bucket = (
            await uow.session.execute(
                select(QuotaBucket).where(QuotaBucket.user_id == case.user_id)
            )
        ).scalar_one()
        assert bucket.reserved == 2 and bucket.used == 0
        assert (
            await uow.session.scalar(
                select(func.count())
                .select_from(QuotaReservation)
                .where(QuotaReservation.user_id == case.user_id)
            )
            == 2
        )


async def test_only_one_worker_claims_and_replaced_lease_cannot_finish(
    concurrent_case: ConcurrentCase,
) -> None:
    case = concurrent_case

    async def claim(uow: UnitOfWork, index: int) -> object:
        return await uow.repositories.jobs.claim(kinds=(case.job_kind,))

    results = await race(case, claim)
    assert sum(result is None for result in results) == 1
    claimed = next(result for result in results if result is not None)
    assert isinstance(claimed, Job) and claimed.lease_token is not None
    old_token = claimed.lease_token
    new_token = uuid4()
    async with case.uows() as uow:
        await uow.session.execute(
            update(Job).where(Job.id == case.job_id).values(lease_token=new_token)
        )
    with pytest.raises(LeaseLostError):
        async with case.uows() as uow:
            await uow.repositories.jobs.finish(case.job_id, old_token)
    async with case.uows() as uow:
        await uow.repositories.jobs.finish(case.job_id, new_token)


async def make_bucket(case: ConcurrentCase, offset: int = 0) -> object:
    start = START + timedelta(days=offset)
    async with case.uows() as uow:
        return (
            await uow.repositories.quota_buckets.get_or_create_for_update(
                case.user_id, start, start + timedelta(days=1)
            )
        ).id


async def test_same_run_reserves_once(concurrent_case: ConcurrentCase) -> None:
    case = concurrent_case
    bucket_id = await make_bucket(case)

    async def reserve(uow: UnitOfWork, index: int) -> object:
        return await uow.repositories.quota_reservations.reserve(
            run_id=case.run_ids[0], bucket_id=bucket_id, user_id=case.user_id, current_limit=10
        )

    results = await race(case, reserve)
    assert all(not isinstance(result, BaseException) for result in results)
    assert results[0].record.id == results[1].record.id
    assert sum(result.created for result in results) == 1
    async with case.uows() as uow:
        assert (await uow.repositories.quota_buckets.get_or_raise(bucket_id)).reserved == 1


async def test_same_run_different_buckets_conflicts_without_counting_twice(
    concurrent_case: ConcurrentCase,
) -> None:
    case = concurrent_case
    bucket_ids = [await make_bucket(case, offset) for offset in range(2)]

    async def reserve(uow: UnitOfWork, index: int) -> object:
        return await uow.repositories.quota_reservations.reserve(
            run_id=case.run_ids[0],
            bucket_id=bucket_ids[index],
            user_id=case.user_id,
            current_limit=10,
        )

    results = await race(case, reserve)
    assert sum(isinstance(result, ConflictError) for result in results) == 1
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    async with case.uows() as uow:
        assert (
            await uow.session.scalar(
                select(func.sum(QuotaBucket.reserved)).where(QuotaBucket.user_id == case.user_id)
            )
            == 1
        )
        assert (
            await uow.session.scalar(
                select(func.count())
                .select_from(QuotaReservation)
                .where(QuotaReservation.user_id == case.user_id)
            )
            == 1
        )


async def test_last_quota_slot_only_accepts_one_run(concurrent_case: ConcurrentCase) -> None:
    case = concurrent_case
    bucket_id = await make_bucket(case)

    async def reserve(uow: UnitOfWork, index: int) -> object:
        return await uow.repositories.quota_reservations.reserve(
            run_id=case.run_ids[index], bucket_id=bucket_id, user_id=case.user_id, current_limit=1
        )

    results = await race(case, reserve)
    assert sum(isinstance(result, QuotaExceededError) for result in results) == 1
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    async with case.uows() as uow:
        assert (await uow.repositories.quota_buckets.get_or_raise(bucket_id)).reserved == 1
        assert (
            await uow.session.scalar(
                select(func.count())
                .select_from(QuotaReservation)
                .where(QuotaReservation.user_id == case.user_id)
            )
            == 1
        )


async def test_concurrent_charge_and_refund_do_not_double_count(
    concurrent_case: ConcurrentCase,
) -> None:
    case = concurrent_case
    bucket_id = await make_bucket(case)
    async with case.uows() as uow:
        await uow.repositories.quota_reservations.reserve(
            run_id=case.run_ids[0], bucket_id=bucket_id, user_id=case.user_id, current_limit=1
        )

    async def charge(uow: UnitOfWork, index: int) -> object:
        return (await uow.repositories.quota_reservations.charge(case.run_ids[0])).status

    assert await race(case, charge) == [QuotaReservationStatus.CHARGED] * 2
    async with case.uows() as uow:
        bucket = await uow.repositories.quota_buckets.get_or_raise(bucket_id)
        assert (bucket.used, bucket.reserved) == (1, 0)

    async def refund(uow: UnitOfWork, index: int) -> object:
        return (await uow.repositories.quota_reservations.refund(case.run_ids[0])).status

    assert await race(case, refund) == [QuotaReservationStatus.REFUNDED] * 2
    async with case.uows() as uow:
        bucket = await uow.repositories.quota_buckets.get_or_raise(bucket_id)
        assert (bucket.used, bucket.reserved) == (0, 0)


async def test_concurrent_events_have_unique_contiguous_sequence(
    concurrent_case: ConcurrentCase,
) -> None:
    case = concurrent_case

    async def emit(uow: UnitOfWork, index: int) -> object:
        return (
            await uow.repositories.run_events.emit(
                case.run_ids[0], RunEventType.RUN_STATUS, {"participant": index}
            )
        ).seq

    results = await race(case, emit, count=8)
    assert sorted(results) == list(range(1, 9))
    async with case.uows() as uow:
        assert (
            await uow.repositories.runs.get_for_update_or_raise(case.run_ids[0])
        ).next_event_seq == 9


class ConversationCAS(VersionedRepository[Conversation]):
    model = Conversation
    mutable_fields = frozenset({"title"})


async def test_stale_content_cas_has_only_one_winner(concurrent_case: ConcurrentCase) -> None:
    case = concurrent_case

    async def edit(uow: UnitOfWork, index: int) -> object:
        return await ConversationCAS(uow.session)._update_versioned(
            case.conversation_ids[0], 0, {"title": str(index)}
        )

    results = await race(case, edit)
    assert sum(isinstance(result, OptimisticLockError) for result in results) == 1
    assert sum(not isinstance(result, BaseException) for result in results) == 1

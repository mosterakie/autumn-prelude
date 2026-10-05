from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from autumn_backend.db.enums import QuotaReservationStatus, ResourceKind
from autumn_backend.db.models import Comment, Publication, ResourceVersion
from autumn_backend.db.session import UnitOfWork
from autumn_backend.errors import ConflictError, OptimisticLockError
from autumn_backend.repositories.publications import PublicationProjection
from autumn_backend.repositories.resources import RevisionDraft
from tests.concurrency.conftest import ConcurrentCase, race
from tests.concurrency.test_core_gate import make_bucket

pytestmark = [pytest.mark.concurrency, pytest.mark.timeout(30)]


async def create_resource(case: ConcurrentCase) -> tuple:
    async with case.uows() as uow:
        resource = await uow.repositories.resources.create(
            owner_id=case.user_id,
            kind=ResourceKind.ARTICLE,
            slug=uuid4().hex,
            draft=RevisionDraft(title="title", body_text="body", private_note="private"),
        )
        return resource.id, resource.current_revision_id, resource.slug


async def test_comment_same_client_id_race_is_idempotent(concurrent_case: ConcurrentCase) -> None:
    case = concurrent_case
    client_id = uuid4()

    async def create(uow: UnitOfWork, index: int) -> object:
        return await uow.repositories.comments.create_or_get(
            author_id=case.user_id, client_id=client_id, body="留言\r\n" if index == 0 else "留言\n"
        )

    results = await race(case, create)
    assert all(not isinstance(result, BaseException) for result in results)
    assert results[0].record.id == results[1].record.id
    assert sum(result.created for result in results) == 1


async def test_reply_creation_and_parent_deletion_are_serialized(
    concurrent_case: ConcurrentCase,
) -> None:
    case = concurrent_case
    async with case.uows() as uow:
        parent_id = (
            await uow.repositories.comments.create_or_get(
                author_id=case.user_id, client_id=uuid4(), body="parent"
            )
        ).record.id

    async def operation(uow: UnitOfWork, index: int) -> object:
        if index == 0:
            return await uow.repositories.comments.create_or_get(
                author_id=case.user_id, client_id=uuid4(), body="reply", parent_id=parent_id
            )
        await uow.session.execute(
            update(Comment).where(Comment.id == parent_id).values(deleted_at=func.clock_timestamp())
        )
        return "deleted"

    results = await race(case, operation)
    assert results[1] == "deleted"
    assert not isinstance(results[0], BaseException) or isinstance(results[0], ConflictError)
    async with case.uows() as uow:
        replies = await uow.session.scalar(
            select(func.count()).select_from(Comment).where(Comment.parent_id == parent_id)
        )
        assert replies == (0 if isinstance(results[0], ConflictError) else 1)


async def test_same_preview_can_only_publish_once(concurrent_case: ConcurrentCase) -> None:
    case = concurrent_case
    resource_id, revision_id, slug = await create_resource(case)
    async with case.uows() as uow:
        epoch = await uow.repositories.settings.get_acl_epoch()

    async def publish(uow: UnitOfWork, index: int) -> object:
        return await uow.repositories.publications.publish_under_resource_lock(
            resource_id=resource_id,
            revision_id=revision_id,
            expected_version=0,
            expected_acl_version=0,
            published_by=case.user_id,
            projection=PublicationProjection(frozenset({"title", "body"})),
        )

    results = await race(case, publish)
    assert sum(isinstance(result, OptimisticLockError) for result in results) == 1
    assert sum(isinstance(result, Publication) for result in results) == 1
    async with case.uows() as uow:
        public = await uow.repositories.publications.public_by_slug(slug)
        assert public is not None and public.publication_no == 1
        assert await uow.repositories.settings.get_acl_epoch() == epoch + 1


async def test_different_resources_keep_local_numbers_and_global_epoch(
    concurrent_case: ConcurrentCase,
) -> None:
    case = concurrent_case
    resources = [await create_resource(case) for _ in range(2)]
    async with case.uows() as uow:
        epoch = await uow.repositories.settings.get_acl_epoch()

    async def publish(uow: UnitOfWork, index: int) -> object:
        resource_id, revision_id, _ = resources[index]
        return await uow.repositories.publications.publish_under_resource_lock(
            resource_id=resource_id,
            revision_id=revision_id,
            expected_version=0,
            expected_acl_version=0,
            published_by=case.user_id,
            projection=PublicationProjection(frozenset({"title"})),
        )

    results = await race(case, publish)
    assert all(isinstance(result, Publication) and result.publication_no == 1 for result in results)
    async with case.uows() as uow:
        assert await uow.repositories.settings.get_acl_epoch() == epoch + 2


async def test_revoke_and_old_publish_preview_cannot_both_succeed(
    concurrent_case: ConcurrentCase,
) -> None:
    case = concurrent_case
    resource_id, revision_id, slug = await create_resource(case)
    async with case.uows() as uow:
        await uow.repositories.publications.publish_under_resource_lock(
            resource_id=resource_id,
            revision_id=revision_id,
            expected_version=0,
            expected_acl_version=0,
            published_by=case.user_id,
            projection=PublicationProjection(frozenset({"title"})),
        )

    async def operation(uow: UnitOfWork, index: int) -> object:
        if index == 0:
            return await uow.repositories.publications.revoke(
                resource_id, expected_version=0, expected_acl_version=1
            )
        return await uow.repositories.publications.publish_under_resource_lock(
            resource_id=resource_id,
            revision_id=revision_id,
            expected_version=0,
            expected_acl_version=1,
            published_by=case.user_id,
            projection=PublicationProjection(frozenset({"title"})),
        )

    results = await race(case, operation)
    assert sum(isinstance(result, OptimisticLockError) for result in results) == 1
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    async with case.uows() as uow:
        public = await uow.repositories.publications.public_by_slug(slug)
        assert (public is None) == (results[0] is True)


async def test_private_revision_race_does_not_append_losing_revision(
    concurrent_case: ConcurrentCase,
) -> None:
    case = concurrent_case
    resource_id, _, _ = await create_resource(case)

    async def revise(uow: UnitOfWork, index: int) -> object:
        return await uow.repositories.resources.revise(
            resource_id,
            expected_version=0,
            created_by=case.user_id,
            draft=RevisionDraft(body_text=str(index)),
        )

    results = await race(case, revise)
    assert sum(isinstance(result, OptimisticLockError) for result in results) == 1
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    async with case.uows() as uow:
        assert (
            await uow.session.scalar(
                select(func.count())
                .select_from(ResourceVersion)
                .where(ResourceVersion.resource_id == resource_id)
            )
            == 2
        )


async def test_charge_and_release_race_has_consistent_final_counter(
    concurrent_case: ConcurrentCase,
) -> None:
    case = concurrent_case
    bucket_id = await make_bucket(case)
    async with case.uows() as uow:
        await uow.repositories.quota_reservations.reserve(
            run_id=case.run_ids[0], bucket_id=bucket_id, user_id=case.user_id, current_limit=1
        )

    async def settle(uow: UnitOfWork, index: int) -> object:
        method = (
            uow.repositories.quota_reservations.charge
            if index == 0
            else uow.repositories.quota_reservations.release
        )
        return (await method(case.run_ids[0])).status

    results = await race(case, settle)
    assert sum(isinstance(result, ConflictError) for result in results) == 1
    async with case.uows() as uow:
        reservation = await uow.repositories.quota_reservations.for_run(case.run_ids[0])
        bucket = await uow.repositories.quota_buckets.get_or_raise(bucket_id)
        assert reservation is not None
        assert bucket.reserved == 0 and bucket.used == (
            1 if reservation.status == QuotaReservationStatus.CHARGED else 0
        )

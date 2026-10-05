import pytest
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from autumn_backend.db.models import Setting
from autumn_backend.errors import (
    ConfigurationError,
    ConflictError,
    InvalidInputError,
    OptimisticLockError,
)
from autumn_backend.repositories.identity import SettingRepository
from autumn_backend.repositories.publications import (
    PublicationProjection,
    PublicationRepository,
    ResourceLockRepository,
)

pytestmark = pytest.mark.integration


async def test_publish_whitelist_versions_and_revoke_immediate(
    session: AsyncSession, make_user: object, make_resource: object, make_resource_version: object
) -> None:
    user = make_user()
    await session.flush()
    resource = await make_resource(user.id)
    revision = await make_resource_version(resource, private_note="private", tags=["private-tag"])
    repository = PublicationRepository(session)
    settings = SettingRepository(session)
    epoch = await settings.get_acl_epoch()
    arguments = dict(
        resource_id=resource.id,
        revision_id=revision.id,
        expected_version=0,
        expected_acl_version=0,
        published_by=user.id,
        projection=PublicationProjection(frozenset({"title", "body"})),
    )
    publication = await repository.publish_under_resource_lock(**arguments)
    assert (
        publication.publication_no == 1
        and publication.public_note is None
        and publication.public_tags == []
    )
    assert await repository.public_by_slug(resource.slug) is publication
    locked = await ResourceLockRepository(session).get_for_update_or_raise(resource.id)
    assert (locked.version, locked.acl_version) == (0, 1)
    assert await settings.get_acl_epoch() == epoch + 1
    with pytest.raises(OptimisticLockError):
        await repository.publish_under_resource_lock(**arguments)
    next_publication = await repository.publish_under_resource_lock(
        **(arguments | {"expected_acl_version": 1})
    )
    assert next_publication.publication_no == 2
    assert await repository.revoke(resource.id, expected_acl_version=2)
    assert await repository.public_by_slug(resource.slug) is None
    assert await settings.get_acl_epoch() == epoch + 3
    assert not await repository.revoke(resource.id, expected_acl_version=3)
    assert await settings.get_acl_epoch() == epoch + 3


async def test_invalid_projection_or_cross_resource_rejected(
    session: AsyncSession, make_user: object, make_resource: object, make_resource_version: object
) -> None:
    user = make_user()
    await session.flush()
    resource = await make_resource(user.id)
    other = await make_resource(user.id)
    revision = await make_resource_version(resource)
    other_revision = await make_resource_version(other)
    repository = PublicationRepository(session)
    arguments = dict(
        resource_id=resource.id,
        revision_id=revision.id,
        expected_version=0,
        expected_acl_version=0,
        published_by=user.id,
        projection=PublicationProjection(frozenset({"title"})),
    )
    with pytest.raises(ConflictError):
        await repository.publish_under_resource_lock(
            **(arguments | {"revision_id": other_revision.id})
        )
    for projection in (
        PublicationProjection(frozenset({"secret"})),
        PublicationProjection(frozenset({"tags"})),
        PublicationProjection(frozenset({"title"}), raw_download_enabled=True),
    ):
        with pytest.raises(InvalidInputError):
            await repository.publish_under_resource_lock(**(arguments | {"projection": projection}))
    await repository.publish_under_resource_lock(**arguments)
    second = await repository.publish_under_resource_lock(
        **(arguments | {"resource_id": other.id, "revision_id": other_revision.id})
    )
    assert second.publication_no == 1


@pytest.mark.parametrize("invalid", [None, True, -1, "0"])
async def test_epoch_missing_or_invalid_fails_closed(
    session: AsyncSession, invalid: object
) -> None:
    if invalid is None:
        await session.execute(delete(Setting).where(Setting.key == "content_acl_epoch"))
    else:
        await session.execute(
            update(Setting).where(Setting.key == "content_acl_epoch").values(value=invalid)
        )
    with pytest.raises(ConfigurationError):
        await SettingRepository(session).get_acl_epoch()

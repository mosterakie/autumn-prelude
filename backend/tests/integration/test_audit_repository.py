import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from autumn_backend.db.enums import AuditResult
from autumn_backend.db.models import AuditEvent
from autumn_backend.errors import InvalidInputError
from autumn_backend.repositories.audit import AuditEventRepository, AuditMetadata

pytestmark = pytest.mark.integration


async def test_audit_rolls_back_with_operation_and_versions_stay_separate(
    session: AsyncSession, make_user: object
) -> None:
    user = make_user()
    await session.flush()
    repository = AuditEventRepository(session)
    with pytest.raises(ValueError):
        async with session.begin_nested():
            await repository.record(
                event_type="resource.publish",
                result=AuditResult.SUCCEEDED,
                actor_id=user.id,
                before_version=5,
                after_version=5,
                metadata=AuditMetadata(before_acl_version=1, after_acl_version=2),
            )
            raise ValueError("abort")
    assert await session.scalar(select(func.count()).select_from(AuditEvent)) == 0
    event = await repository.record(
        event_type="resource.publish",
        result=AuditResult.SUCCEEDED,
        actor_id=user.id,
        before_version=5,
        after_version=5,
        metadata=AuditMetadata(
            before_acl_version=1, after_acl_version=2, changed_fields=("public_fields",)
        ),
    )
    assert (event.before_version, event.after_version) == (5, 5)
    assert event.metadata_json == {
        "before_acl_version": 1,
        "after_acl_version": 2,
        "changed_fields": ["public_fields"],
    }
    assert not hasattr(repository, "update") and not hasattr(repository, "delete")


async def test_audit_rejects_freeform_private_metadata(session: AsyncSession) -> None:
    with pytest.raises(InvalidInputError):
        await AuditEventRepository(session).record(
            event_type="resource.publish",
            result=AuditResult.DENIED,
            metadata=AuditMetadata(reason_code="完整私人正文"),
        )

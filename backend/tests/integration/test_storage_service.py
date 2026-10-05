import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from autumn_backend.db.enums import FileObjectStatus
from autumn_backend.errors import ConflictError, IdempotencyConflictError, NotFoundError
from autumn_backend.services.access import AuthorizationError
from autumn_backend.services.storage import StorageService
from autumn_backend.storage.local import LocalObjectStore
from tests.integration.service_cases import ServiceCase

pytestmark = pytest.mark.integration


def store_for_test() -> LocalObjectStore:
    return LocalObjectStore(
        Path(__file__).resolve().parents[3] / "work" / "storage-tests" / uuid4().hex
    )


async def ready_file(e_case: ServiceCase, service: StorageService):
    await service.upload(
        e_case.owner,
        idempotency_key="upload",
        filename="test.pdf",
        data=b"%PDF-1.7\nexample",
        media_type="application/pdf",
    )
    async with e_case.uows() as uow:
        job = await uow.repositories.jobs.claim(kinds=("storage.finalize",))
        assert job is not None and job.lease_token is not None
    return await service.finalize(e_case.owner, job.id, job.lease_token)


async def test_upload_finalize_retry_and_permission(e_case: ServiceCase) -> None:
    service = StorageService(e_case.uows, store_for_test())
    file = await ready_file(e_case, service)
    assert file.status is FileObjectStatus.READY
    assert await service.read_private(e_case.owner, file.id) == b"%PDF-1.7\nexample"
    again = await service.upload(
        e_case.owner,
        idempotency_key="upload",
        filename="test.pdf",
        data=b"%PDF-1.7\nexample",
        media_type="application/pdf",
    )
    assert again == file
    with pytest.raises(IdempotencyConflictError):
        await service.upload(
            e_case.owner,
            idempotency_key="upload",
            filename="test.pdf",
            data=b"%PDF-1.7\nchanged",
            media_type="application/pdf",
        )
    with pytest.raises(AuthorizationError):
        await service.upload(
            e_case.member,
            idempotency_key="upload",
            filename="test.pdf",
            data=b"%PDF-1.7\nexample",
            media_type="application/pdf",
        )


async def test_delete_blocks_access_before_physical_removal(
    e_case: ServiceCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = StorageService(e_case.uows, store_for_test())
    file = await ready_file(e_case, service)
    assert (await service.delete(e_case.owner, file.id)).status is FileObjectStatus.PENDING_DELETE
    with pytest.raises(NotFoundError):
        await service.read_private(e_case.owner, file.id)
    async with e_case.uows() as uow:
        job = await uow.repositories.jobs.claim(kinds=("storage.delete",))
        assert job is not None and job.lease_token is not None
    original = service.store.delete

    async def fail(key: str) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(service.store, "delete", fail)
    with pytest.raises(OSError):
        await service.run_delete(e_case.owner, job.id, job.lease_token)
    async with e_case.uows() as uow:
        assert (await uow.repositories.files.get_or_raise(file.id)).deleted_at is None
    monkeypatch.setattr(service.store, "delete", original)
    await service.run_delete(e_case.owner, job.id, job.lease_token)
    with pytest.raises(FileNotFoundError):
        await service.store.read(file.object_key)
    with pytest.raises(ConflictError):
        await service.store.stage(file.object_key, b"%PDF-1.7\nexample")
    with pytest.raises(ConflictError):
        await service.store.promote(file.object_key, file.sha256, file.byte_size)


async def test_orphan_cleanup_preserves_registered_staging(e_case: ServiceCase) -> None:
    store = store_for_test()
    service = StorageService(e_case.uows, store)
    file = await service.upload(
        e_case.owner,
        idempotency_key="registered",
        filename="test.pdf",
        data=b"%PDF-1.7\nexample",
        media_type="application/pdf",
    )
    orphan = f"objects/{uuid4().hex}.pdf"
    await store.stage(orphan, b"unused")
    old = (datetime.now(UTC) - timedelta(days=2)).timestamp()
    os.utime(store._path(orphan, staging=True), (old, old))
    os.utime(store._path(file.object_key, staging=True), (old, old))
    assert await service.cleanup_orphans(now=datetime.now(UTC)) == (orphan,)
    assert store._path(file.object_key, staging=True).exists()

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from autumn_backend.app import create_app
from autumn_backend.auth.service import AuthService
from autumn_backend.config import Environment, Settings
from autumn_backend.db.enums import ResourceKind
from autumn_backend.errors import NotFoundError
from autumn_backend.io_boundary import active_uows
from autumn_backend.repositories.publications import PublicationProjection
from autumn_backend.repositories.resources import RevisionDraft
from autumn_backend.services.actions import ActionService
from autumn_backend.services.public import PublicService
from autumn_backend.services.publication import PublicationService
from autumn_backend.services.storage import StorageService
from autumn_backend.storage.local import LocalObjectStore
from tests.integration.service_cases import ServiceCase
from tests.integration.test_storage_service import ready_file, store_for_test

pytestmark = pytest.mark.integration


async def test_public_projection_stays_pinned_and_revoke_returns_uniform_404(
    e_case: ServiceCase,
) -> None:
    service = PublicService(e_case.uows)
    app = create_app(Settings(environment=Environment.TEST))
    app.state.public = service
    async with e_case.uows() as uow:
        resource = await uow.repositories.resources.get_or_raise(e_case.resource_id)
        slug, version = resource.slug, resource.version
        await uow.repositories.resources.revise(
            resource.id,
            expected_version=version,
            draft=RevisionDraft(
                title="PRIVATE-NEW-TITLE", body_text="PRIVATE-NEW-BODY", private_note="PRIVATE-NOTE"
            ),
            created_by=e_case.owner.user_id,
        )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.get(f"/api/public/notes/{slug}")
        assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
        assert result.json()["data"]["body"] == "公共正文"
        assert "PRIVATE" not in result.text and "private_note" not in result.text
        assert "note" not in result.json()["data"]
        assert (await client.get("/api/public/notes")).json()["data"]["items"][0]["id"] == str(
            resource.id
        )
        assert (await client.get("/api/public/notes?tag=未公开标签")).json()["data"]["items"] == []
        async with e_case.uows() as uow:
            current = await uow.repositories.resources.get_or_raise(e_case.resource_id)
            await uow.repositories.publications.revoke(
                current.id,
                expected_version=current.version,
                expected_acl_version=current.acl_version,
            )
        withdrawn = await client.get(f"/api/public/sources/{e_case.publication_id}")
        unknown = await client.get(f"/api/public/sources/{uuid4()}")
        assert withdrawn.status_code == unknown.status_code == 404
        assert withdrawn.json()["error"] == unknown.json()["error"]
        assert (await client.get("/api/public/notes")).json()["data"]["items"] == []


async def test_public_download_rechecks_revoke_after_object_io(e_case: ServiceCase) -> None:
    store = store_for_test()
    storage = StorageService(e_case.uows, store)
    file = await ready_file(e_case, storage)
    async with e_case.uows() as uow:
        resource = await uow.repositories.resources.create(
            owner_id=e_case.owner.user_id,
            kind=ResourceKind.DOCUMENT,
            slug=uuid4().hex,
            draft=RevisionDraft(
                title="公开原件",
                file_object_key=file.object_key,
                file_sha256=file.sha256,
                media_type=file.media_type,
                byte_size=file.byte_size,
            ),
        )
        await uow.repositories.files.attach(file.id, resource.id)
        publication = await uow.repositories.publications.publish_under_resource_lock(
            resource_id=resource.id,
            revision_id=resource.current_revision_id,
            expected_version=resource.version,
            expected_acl_version=resource.acl_version,
            published_by=e_case.owner.user_id,
            projection=PublicationProjection(frozenset({"title"}), raw_download_enabled=True),
        )
    assert (await PublicService(e_case.uows, store).file(publication.id))[0] == b"%PDF-1.7\nexample"

    class RevokeDuringRead(LocalObjectStore):
        async def read(self, key: str) -> bytes:
            assert active_uows.get() == 0
            content = await store.read(key)
            async with e_case.uows() as uow:
                current = await uow.repositories.resources.get_or_raise(resource.id)
                await uow.repositories.publications.revoke(
                    current.id,
                    expected_version=current.version,
                    expected_acl_version=current.acl_version,
                )
            return content

    with pytest.raises(NotFoundError):
        await PublicService(e_case.uows, RevokeDuringRead(store.root)).file(publication.id)


async def test_publication_preview_requires_confirmation_and_revoke_is_immediate(
    e_case: ServiceCase,
) -> None:
    settings = Settings(environment=Environment.TEST)
    auth = AuthService(e_case.uows, settings)
    app = create_app(settings)
    app.state.auth, app.state.public = auth, PublicService(e_case.uows)
    app.state.actions, app.state.publications = (
        ActionService(e_case.uows),
        PublicationService(e_case.uows),
    )
    async with e_case.uows() as uow:
        user = await uow.repositories.users.get_for_update(e_case.owner.user_id)
        login = await auth._new_session(uow, user, step_up=e_case.owner.step_up_expires_at)
        resource = await uow.repositories.resources.get_or_raise(e_case.resource_id)
        version, acl = resource.version, resource.acl_version
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.cookies.set("autumn_session", login.token)
        headers = {
            "Origin": "http://localhost:3000",
            "X-CSRF-Token": login.session.csrf_token,
            "Idempotency-Key": uuid4().hex,
        }
        body = {
            "revision_id": str(e_case.revision_id),
            "public_fields": ["title", "body_text"],
            "expected_version": version,
            "expected_acl_version": acl,
        }
        preview = await client.post(
            f"/api/resources/{resource.id}/publication/preview", json=body, headers=headers
        )
        assert preview.status_code == 201
        assert preview.json()["data"]["status"] == "awaiting_confirmation"
        still_current = await client.get(f"/api/public/sources/{e_case.publication_id}")
        assert still_current.status_code == 200
        bad = await client.post(
            f"/api/resources/{resource.id}/publication/preview",
            json={**body, "public_fields": ["password_hash"]},
            headers={**headers, "Idempotency-Key": uuid4().hex},
        )
        assert bad.status_code == 422
        revoke = await client.post(
            f"/api/resources/{resource.id}/publication/revoke",
            json={"expected_version": version, "expected_acl_version": acl},
            headers={**headers, "Idempotency-Key": uuid4().hex},
        )
        assert revoke.status_code == 200
        assert (await client.get(f"/api/public/sources/{e_case.publication_id}")).status_code == 404

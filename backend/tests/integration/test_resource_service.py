from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from autumn_backend.app import create_app
from autumn_backend.auth.service import AuthService
from autumn_backend.config import Environment, Settings
from autumn_backend.db.enums import ResourceKind
from autumn_backend.db.models import Action, ResourceVersion
from autumn_backend.errors import IdempotencyConflictError, NotFoundError, OptimisticLockError
from autumn_backend.repositories.resources import RevisionDraft
from autumn_backend.services.resources import (
    CreateResource,
    DeleteResource,
    EditResource,
    ResourceService,
)
from tests.integration.service_cases import ServiceCase

pytestmark = pytest.mark.integration


async def test_resource_crud_retries_revisions_and_delete_revoke_atomically(
    e_case: ServiceCase,
) -> None:
    service = ResourceService(e_case.uows)
    key = uuid4().hex
    command = CreateResource(
        kind="article", title="新的手记", body_text="原稿", private_note="PRIVATE-NOTE"
    )
    created = await service.mutate(e_case.owner, key=key, command=command)
    resource_id = UUID(str(created["id"]))
    assert (await service.mutate(e_case.owner, key=key, command=command))["id"] == resource_id
    with pytest.raises(IdempotencyConflictError):
        await service.mutate(
            e_case.owner, key=key, command=command.model_copy(update={"title": "不同标题"})
        )
    edit = EditResource(expected_version=created["version"], body_text="第二版本")
    edit_key = uuid4().hex
    updated = await service.mutate(
        e_case.owner, key=edit_key, command=edit, resource_id=resource_id
    )
    assert updated["current_revision"]["private_note"] == "PRIVATE-NOTE"
    assert updated["version"] == created["version"] + 1
    assert updated["acl_version"] == created["acl_version"]
    assert (
        await service.mutate(e_case.owner, key=edit_key, command=edit, resource_id=resource_id)
    )["version"] == updated["version"]
    with pytest.raises(OptimisticLockError):
        await service.mutate(e_case.owner, key=uuid4().hex, command=edit, resource_id=resource_id)
    versions = await service.versions(e_case.owner, resource_id, limit=1, cursor=None)
    assert versions["next_cursor"] is not None
    older = await service.versions(
        e_case.owner, resource_id, limit=1, cursor=versions["next_cursor"]
    )
    # 隔离夹具共用外层事务，now() 的 created_at 会并列；复合游标依照 UUID 排序。
    # 验证两页不重复、原始版本保留，不假设并列时间的 UUID 恰好符合修订顺序。
    history = versions["items"] + older["items"]
    assert {item["revision_no"] for item in history} == {1, 2}
    assert {item["body_text"] for item in history} == {"原稿", "第二版本"}
    assert len({item["id"] for item in history}) == 2
    async with e_case.uows() as uow:
        assert (
            await uow.session.execute(
                select(func.count())
                .select_from(ResourceVersion)
                .where(ResourceVersion.resource_id == resource_id)
            )
        ).scalar_one() == 2
        assert (
            await uow.session.execute(
                select(func.count())
                .select_from(Action)
                .where(Action.target_resource_id == resource_id)
            )
        ).scalar_one() == 2
        published = await uow.repositories.resources.get_or_raise(e_case.resource_id)
        versions_to_delete = (published.version, published.acl_version)
    delete = DeleteResource(
        expected_version=versions_to_delete[0], expected_acl_version=versions_to_delete[1]
    )
    delete_key = uuid4().hex
    result = await service.mutate(
        e_case.owner, key=delete_key, command=delete, resource_id=e_case.resource_id
    )
    assert result["deleted"]
    assert (
        await service.mutate(
            e_case.owner, key=delete_key, command=delete, resource_id=e_case.resource_id
        )
        == result
    )
    async with e_case.uows() as uow:
        assert await uow.repositories.publications.current_for_resource(e_case.resource_id) is None
        assert await uow.repositories.settings.get_acl_epoch() > e_case.owner.scope_epoch


async def test_private_resource_and_revision_isolation(e_case: ServiceCase) -> None:
    service = ResourceService(e_case.uows)
    with pytest.raises(NotFoundError):
        await service.read(e_case.member, e_case.resource_id)
    async with e_case.uows() as uow:
        assert e_case.member.user_id is not None
        other = await uow.repositories.resources.create(
            owner_id=e_case.member.user_id,
            kind=ResourceKind.BOOKMARK,
            slug=uuid4().hex,
            draft=RevisionDraft(title="别人的资料", url="https://example.com"),
        )
    with pytest.raises(NotFoundError):
        await service.read(e_case.owner, other.id)
    with pytest.raises(NotFoundError):
        await service.versions(e_case.owner, other.id, limit=20, cursor=None)
    with pytest.raises(NotFoundError):
        await service.file(e_case.owner, e_case.resource_id, other.current_revision_id)
    page = await service.list(e_case.owner, kind="article", limit=1, cursor=None)
    assert all(item["id"] != other.id for item in page["items"])


async def test_resource_http_identity_and_write_guard(e_case: ServiceCase) -> None:
    settings = Settings(environment=Environment.TEST)
    auth = AuthService(e_case.uows, settings)
    app = create_app(settings)
    app.state.auth, app.state.resources = auth, ResourceService(e_case.uows)
    async with e_case.uows() as uow:
        user = await uow.repositories.users.get_for_update(e_case.owner.user_id)
        assert user is not None
        login = await auth._new_session(uow, user, step_up=e_case.owner.step_up_expires_at)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get(f"/api/resources/{e_case.resource_id}")).status_code == 401
        client.cookies.set("autumn_session", login.token)
        body = {"kind": "bookmark", "title": "网页收藏", "url": "https://example.com"}
        key = uuid4().hex
        assert (
            await client.post("/api/resources", json=body, headers={"Idempotency-Key": key})
        ).status_code == 403
        headers = {
            "Idempotency-Key": key,
            "Origin": "http://localhost:3000",
            "X-CSRF-Token": login.session.csrf_token,
        }
        result = await client.post("/api/resources", json=body, headers=headers)
        assert result.status_code == 201
        assert (await client.post("/api/resources", json=body, headers=headers)).json()["data"][
            "id"
        ] == result.json()["data"]["id"]
        unknown = await client.get(f"/api/resources/{uuid4()}")
        assert unknown.status_code == 404
        assert (await client.get("/api/resources?limit=101")).status_code == 422

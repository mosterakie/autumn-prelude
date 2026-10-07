"""导入接口基础链路：真实 PostgreSQL；外部抓取/嵌入使用受控端口。"""

import io
import zipfile
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from autumn_backend.app import create_app
from autumn_backend.auth.service import AuthService
from autumn_backend.config import Environment, Settings
from autumn_backend.db.models import KnowledgeIndex
from autumn_backend.knowledge.web import WebPage
from autumn_backend.repositories.resources import RevisionDraft
from autumn_backend.services.knowledge import KnowledgeService
from autumn_backend.services.knowledge_imports import KnowledgeImportService
from autumn_backend.services.resources import ResourceService
from autumn_backend.services.storage import DOCX, StorageService
from autumn_backend.services.tasks import TaskService
from autumn_backend.workers.bootstrap import configured_worker
from tests.integration.test_knowledge_service import Embeddings
from tests.integration.test_storage_service import store_for_test

pytestmark = pytest.mark.integration


class Web:
    def __init__(self):
        self.calls = []

    async def fetch(self, url):
        from autumn_backend.io_boundary import active_uows

        assert active_uows.get() == 0
        self.calls.append(url)
        return WebPage(url, "测试网页", "可检索的网页正文")


async def setup(e_case, *, member=False):
    settings = Settings(environment=Environment.TEST, _env_file=None)
    app = create_app(settings)
    auth = AuthService(e_case.uows, settings)
    storage = StorageService(e_case.uows, store_for_test())
    embedder, web = Embeddings(), Web()
    knowledge = KnowledgeService(e_case.uows, storage, embedder, fetcher=web)
    imports = KnowledgeImportService(e_case.uows, storage, knowledge)
    app.state.auth, app.state.knowledge_imports = auth, imports
    app.state.resources, app.state.tasks = (
        ResourceService(e_case.uows, storage),
        TaskService(e_case.uows),
    )
    actor = e_case.member if member else e_case.owner
    async with e_case.uows() as uow:
        user = await uow.repositories.users.get_for_update_or_raise(actor.user_id)
        login = await auth._new_session(uow, user, step_up=actor.step_up_expires_at)
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    client.cookies.set(settings.cookie_name, login.token)
    client.headers.update(
        {
            "Origin": "http://localhost:3000",
            "X-CSRF-Token": login.session.csrf_token,
            "Idempotency-Key": uuid4().hex,
        }
    )
    worker = configured_worker(e_case.uows, storage, knowledge=knowledge)
    worker.maintenance = None
    return client, worker, imports, login.session.actor, embedder, web


@pytest.mark.parametrize("mode", ["bookmark_only", "knowledge_only", "bookmark_and_knowledge"])
async def test_url_modes_idempotency_status_and_refresh(e_case, mode):
    client, worker, _imports, _actor, embedder, web = await setup(e_case)
    async with client:
        body = {
            "url": "https://example.com/source",
            "mode": mode,
            "title": "自定义标题",
            "tags": ["研究"],
        }
        accepted = await client.post("/api/knowledge/urls", json=body)
        assert accepted.status_code == 202, accepted.text
        data = accepted.json()["data"]
        resource_id, job_id = data["resource"]["id"], data["job"]["id"]
        assert data["resource"]["publication"] is None
        assert (data["bookmark"] is not None) == (mode == "bookmark_and_knowledge")
        assert data["resource"]["kind"] == ("bookmark" if mode == "bookmark_only" else "webpage")
        assert web.calls == embedder.inputs == []
        repeated = await client.post("/api/knowledge/urls", json=body)
        assert repeated.json()["data"]["job"]["id"] == job_id
        assert (
            await client.post("/api/knowledge/urls", json={**body, "mode": "other"})
        ).status_code == 422
        assert (
            await client.post("/api/knowledge/urls", json={**body, "title": "不同标题"})
        ).status_code == 409
        assert await worker.run_once()
        finished = (await client.get(f"/api/jobs/{job_id}")).json()["data"]
        assert finished["status"] == "succeeded" and finished["progress"] is None
        assert "payload" not in finished and "lease_token" not in finished
        resource = (await client.get(f"/api/resources/{resource_id}")).json()["data"]
        assert resource["publication"] is None
        async with e_case.uows() as uow:
            indexes = await uow.session.scalar(
                select(func.count())
                .select_from(KnowledgeIndex)
                .where(KnowledgeIndex.resource_id == UUID(resource_id))
            )
        if mode == "bookmark_only":
            assert indexes == 0 and web.calls == embedder.inputs == []
        else:
            assert indexes == 1 and len(web.calls) == 1 and embedder.inputs
            assert resource["current_revision"]["body_text"] == "可检索的网页正文"
            async with e_case.uows() as uow:
                await uow.repositories.resources.revise(
                    UUID(resource_id),
                    expected_version=resource["version"],
                    created_by=_actor.user_id,
                    draft=RevisionDraft(
                        title="自定义标题",
                        url=body["url"],
                        body_text="可检索的网页正文",
                        tags=("研究",),
                        private_note="私人阅读笔记",
                    ),
                )
            resource = (await client.get(f"/api/resources/{resource_id}")).json()["data"]
            refreshed = await client.post(
                f"/api/resources/{resource_id}/refresh",
                headers={"Idempotency-Key": uuid4().hex},
                json={"expected_version": resource["version"]},
            )
            assert refreshed.status_code == 202, refreshed.text
            assert await worker.run_once()
            refreshed_resource = (await client.get(f"/api/resources/{resource_id}")).json()["data"]
            assert refreshed_resource["current_revision"]["private_note"] == "私人阅读笔记"
            assert (await client.get(f"/api/jobs/{refreshed.json()['data']['job']['id']}")).json()[
                "data"
            ]["status"] == "succeeded"


async def test_docx_upload_parse_index_and_private_download(e_case):
    client, worker, _imports, _actor, embedder, web = await setup(e_case)
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>私人知识段落</w:t></w:r></w:p></w:body></w:document>',
        )
    async with client:
        files = {"file": ("sample.docx", content.getvalue(), DOCX)}
        response = await client.post(
            "/api/knowledge/files", files=files, data={"title": "研究文档"}
        )
        assert response.status_code == 202, response.text
        data = response.json()["data"]
        assert "object_key" not in response.text and data["resource"]["publication"] is None
        assert (
            await client.post("/api/knowledge/files", files=files, data={"title": "研究文档"})
        ).json()["data"]["job"]["id"] == data["job"]["id"]
        assert await worker.run_once()
        finished = (await client.get(f"/api/jobs/{data['job']['id']}")).json()["data"]
        assert finished["status"] == "succeeded", finished
        resource = (await client.get(f"/api/resources/{data['resource']['id']}")).json()["data"]
        revision = resource["current_revision"]
        assert revision["body_text"] == "私人知识段落" and "私人知识段落" in embedder.inputs
        raw = await client.get(f"/api/resources/{resource['id']}/versions/{revision['id']}/file")
        assert raw.status_code == 200 and raw.content == content.getvalue()
        assert web.calls == []


async def test_import_auth_validation_limits_and_queued_cancel(e_case, monkeypatch):
    client, worker, _imports, _actor, _embedder, _web = await setup(e_case)
    body = {"url": "https://example.com/source", "mode": "knowledge_only"}
    async with client:
        assert (
            await client.post("/api/knowledge/urls", json=body, headers={"X-CSRF-Token": "bad"})
        ).status_code == 403
        assert (
            await client.post("/api/knowledge/urls", json={**body, "url": "http://127.0.0.1/"})
        ).status_code == 422
        assert (
            await client.post(
                "/api/knowledge/files",
                files={"file": ("old.doc", b"old", "application/octet-stream")},
            )
        ).status_code == 422
        assert (
            await client.post(
                "/api/knowledge/files",
                content=b"",
                headers={"Content-Length": str(21 * 1024 * 1024)},
            )
        ).status_code == 413
        monkeypatch.setattr("autumn_backend.api.upload_limit.MAX_UPLOAD_BODY", 128)

        async def oversized():
            yield b"x" * 256

        assert (
            await client.post(
                "/api/knowledge/files",
                content=oversized(),
                headers={"Content-Type": "multipart/form-data; boundary=test"},
            )
        ).status_code == 413
        accepted = (await client.post("/api/knowledge/urls", json=body)).json()["data"]
        job_id = accepted["job"]["id"]
        assert (await client.get(f"/api/jobs/{uuid4()}")).status_code == 404
        assert (await client.post(f"/api/jobs/{job_id}/cancel")).json()["data"][
            "status"
        ] == "cancelled"
        assert not await worker.run_once()
        client.cookies.clear()
        assert (await client.post("/api/knowledge/urls", json=body)).status_code == 401
    member, *_ = await setup(e_case, member=True)
    async with member:
        assert (await member.post("/api/knowledge/urls", json=body)).status_code == 403
        assert (await member.get(f"/api/jobs/{job_id}")).status_code == 403


async def test_waiting_auth_resume_and_stale_fetch_does_not_overwrite(e_case):
    client, worker, _imports, actor, embedder, web = await setup(e_case)
    async with client:
        accepted = (
            await client.post(
                "/api/knowledge/urls",
                json={"url": "https://example.com/", "mode": "knowledge_only"},
            )
        ).json()["data"]
        job_id, resource_id = accepted["job"]["id"], accepted["resource"]["id"]
        async with e_case.uows() as uow:
            session = await uow.repositories.auth_sessions.get_for_update_or_raise(
                actor.auth_session_id
            )
            session.step_up_expires_at = await uow.repositories.users.database_time() - timedelta(
                seconds=1
            )
        assert await worker.run_once()
        async with e_case.uows() as uow:
            job = await uow.repositories.jobs.get_or_raise(UUID(job_id))
            assert job.status.value == "waiting_auth"
            session = await uow.repositories.auth_sessions.get_for_update_or_raise(
                actor.auth_session_id
            )
            session.step_up_expires_at = await uow.repositories.users.database_time() + timedelta(
                minutes=10
            )
        dto = (await client.get(f"/api/jobs/{job_id}")).json()["data"]
        assert dto["can_retry"] and web.calls == []
        assert (
            await client.post(
                f"/api/jobs/{job_id}/retry", json={"expected_version": dto["version"]}
            )
        ).status_code == 202

        async def fetch_and_edit(url):
            async with e_case.uows() as uow:
                resource = await uow.repositories.resources.get_for_update_or_raise(
                    UUID(resource_id)
                )
                await uow.repositories.resources.revise(
                    resource.id,
                    expected_version=resource.version,
                    draft=RevisionDraft(title="我编辑的新版本", body_text="不要覆盖"),
                    created_by=actor.user_id,
                )
            return WebPage(url, "旧标题", "旧抓取正文")

        web.fetch = fetch_and_edit
        assert await worker.run_once()
        failed = (await client.get(f"/api/jobs/{job_id}")).json()["data"]
        assert failed["status"] == "failed" and failed["error"]["code"] == "IMPORT_STALE"
        resource = (await client.get(f"/api/resources/{resource_id}")).json()["data"]
        assert resource["current_revision"]["body_text"] == "不要覆盖" and embedder.inputs == []

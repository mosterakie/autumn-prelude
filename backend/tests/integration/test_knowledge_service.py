import io
import zipfile
from uuid import uuid4

import pytest
from pypdf import PdfWriter

from autumn_backend.db.enums import RunStatus
from autumn_backend.db.models import ResourceVersion
from autumn_backend.errors import (
    ConflictError,
    InvalidInputError,
    NotFoundError,
    OptimisticLockError,
)
from autumn_backend.io_boundary import active_uows
from autumn_backend.knowledge.text import extract
from autumn_backend.knowledge.web import WebPage, validate_url
from autumn_backend.services.access import AuthorizationError
from autumn_backend.services.knowledge import KnowledgeService
from autumn_backend.services.storage import DOCX, PDF, StorageService
from tests.integration.service_cases import ServiceCase
from tests.integration.test_storage_service import store_for_test

pytestmark = pytest.mark.integration


class Embeddings:
    provider, model = "test", "fixed-1024"

    def __init__(self):
        self.inputs = []

    async def embed(self, texts, *, external_idempotency_key):
        assert active_uows.get() == 0
        assert external_idempotency_key.startswith("autumn-")
        self.inputs.extend(texts)
        return [[1.0] + [0.0] * 1023 for _ in texts]


class Web:
    async def fetch(self, url):
        assert active_uows.get() == 0
        return WebPage(url, "网页标题", "网页正文")


def service_for(e_case):
    return KnowledgeService(
        e_case.uows, StorageService(e_case.uows, store_for_test()), Embeddings(), fetcher=Web()
    )


async def build(e_case, service, publication=None):
    await service.request_index(e_case.owner, e_case.resource_id, publication_id=publication)
    async with e_case.uows() as uow:
        job = await uow.repositories.jobs.claim(kinds=("knowledge.ingest",))
        assert job is not None and job.lease_token is not None
    return await service.build_index(e_case.owner, job.id, job.lease_token)


async def test_index_batches_each_have_an_independent_ledger(e_case: ServiceCase):
    class Batched(Embeddings):
        batch_size = 2

        def __init__(self):
            super().__init__()
            self.keys = []

        async def embed(self, texts, *, external_idempotency_key):
            assert len(texts) <= self.batch_size
            self.keys.append(external_idempotency_key)
            return await super().embed(texts, external_idempotency_key=external_idempotency_key)

    from sqlalchemy import select

    from autumn_backend.db.enums import ProviderCallStatus
    from autumn_backend.db.models import ProviderCall

    embedder = Batched()
    service = KnowledgeService(e_case.uows, service_for(e_case).storage, embedder)
    await build(e_case, service)
    assert len(set(embedder.keys)) == 2 and len(embedder.inputs) == 3
    async with e_case.uows() as uow:
        calls = (
            await uow.session.scalars(select(ProviderCall).where(ProviderCall.provider == "test"))
        ).all()
        assert len(calls) == 2 and all(
            call.status is ProviderCallStatus.SUCCEEDED for call in calls
        )


async def test_private_public_indexes_and_current_citation(e_case: ServiceCase) -> None:
    service = service_for(e_case)
    await build(e_case, service)
    await build(e_case, service, e_case.publication_id)
    async with e_case.uows() as uow:
        public = await e_case.run(uow)
        private = await e_case.run(uow, owner=True)
    public_hits = await service.retrieve(e_case.member, public.id, "正文")
    private_hits = await service.retrieve(e_case.owner, private.id, "备注")
    assert {hit.text for hit in public_hits} == {"公开标题", "公共正文"}
    assert "私密备注" in {hit.text for hit in private_hits}
    citation = await service.citation(e_case.member, public.id, public_hits[0].id)
    assert citation == public_hits[0]
    async with e_case.uows() as uow:
        fetched_at = await uow.repositories.users.database_time()
    page = WebPage("https://example.com/source", "联网标题", "联网摘要")
    with pytest.raises(AuthorizationError):
        await service.record_web_sources(e_case.member, public.id, (page,), fetched_at=fetched_at)
    web_sources = await service.record_web_sources(
        e_case.owner, private.id, (page,), fetched_at=fetched_at
    )
    assert (await service.citation(e_case.owner, private.id, web_sources[0].id)).text == "联网摘要"
    async with e_case.uows() as uow:
        await uow.repositories.publications.revoke(
            e_case.resource_id, expected_version=0, expected_acl_version=1
        )
    with pytest.raises(NotFoundError):
        await service.citation(e_case.member, public.id, citation.id)
    # H7 起同一逻辑外部调用禁止自动重放；新检索仍必须看到撤回后的当前范围。
    with pytest.raises(ConflictError):
        await service.retrieve(e_case.member, public.id, "正文")
    assert await service.retrieve(e_case.member, public.id, "撤回后的正文") == ()


async def test_document_web_ingest_and_no_ocr(e_case: ServiceCase) -> None:
    service = service_for(e_case)
    document = io.BytesIO()
    with zipfile.ZipFile(document, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>知识段落</w:t></w:r></w:p></w:body></w:document>',
        )
    file = await service.storage.upload(
        e_case.owner,
        idempotency_key="docx",
        filename="sample.docx",
        data=document.getvalue(),
        media_type=DOCX,
    )
    async with e_case.uows() as uow:
        job = await uow.repositories.jobs.claim(kinds=("storage.finalize",))
        assert job is not None and job.lease_token is not None
    await service.storage.finalize(e_case.owner, job.id, job.lease_token)
    result = await service.ingest_file(e_case.owner, file.id, title="知识文档")
    assert await service.ingest_file(e_case.owner, file.id, title="知识文档") == result
    web = await service.ingest_web(
        e_case.owner, url="https://example.com/article", idempotency_key="web"
    )
    assert (
        await service.ingest_web(
            e_case.owner, url="https://example.com/article", idempotency_key="web"
        )
        == web
    )
    async with e_case.uows() as uow:
        revision = await uow.session.get(ResourceVersion, result.revision_id)
        assert revision.body_text == "知识段落" and revision.file_object_key == file.object_key
    for _ in range(2):
        async with e_case.uows() as uow:
            ingest_job = await uow.repositories.jobs.claim(kinds=("knowledge.ingest",))
            assert ingest_job is not None and ingest_job.lease_token is not None
        await service.build_index(e_case.owner, ingest_job.id, ingest_job.lease_token)
    async with e_case.uows() as uow:
        owner_run = await e_case.run(uow, owner=True)
    hits = await service.retrieve(e_case.owner, owner_run.id, "知识段落", limit=20)
    assert any(hit.text == "知识段落" and hit.locator["paragraph"] == 1 for hit in hits)
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    data = io.BytesIO()
    writer.write(data)
    with pytest.raises(InvalidInputError):
        extract(data.getvalue(), PDF)
    with pytest.raises(InvalidInputError):
        validate_url("http://127.0.0.1/secret")


async def test_index_permission_change_during_external_io_discards_results(
    e_case: ServiceCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = service_for(e_case)
    await service.request_index(
        e_case.owner, e_case.resource_id, publication_id=e_case.publication_id
    )
    async with e_case.uows() as uow:
        job = await uow.repositories.jobs.claim(kinds=("knowledge.ingest",))
        assert job is not None and job.lease_token is not None
    original = service.embedder.embed

    async def revoke(texts, *, external_idempotency_key=None):
        vectors = await original(
        texts,
        external_idempotency_key=external_idempotency_key,
       )

    monkeypatch.setattr(service.embedder, "embed", revoke)
    with pytest.raises(OptimisticLockError):
        await service.build_index(e_case.owner, job.id, job.lease_token)
    async with e_case.uows() as uow:
        assert not await uow.repositories.knowledge.allowed_indexes(
            owner_id=None, provider="test", model="fixed-1024"
        )


async def test_history_dependency_closure_is_revalidated(e_case: ServiceCase) -> None:
    service = service_for(e_case)
    await build(e_case, service, e_case.publication_id)
    async with e_case.uows() as uow:
        first = await e_case.run(uow)
    await service.retrieve(e_case.member, first.id, "正文")
    await service.capture_dependencies(e_case.member, first.id)
    async with e_case.uows() as uow:
        first = await uow.repositories.runs.get_for_update_or_raise(first.id)
        message = await uow.repositories.messages.append_user(
            conversation_id=first.conversation_id,
            run_id=first.id,
            client_message_id=uuid4(),
            body="引用过上述来源",
        )
        first.status = RunStatus.SUCCEEDED
        first.finished_at = await uow.repositories.users.database_time()
        await uow.session.flush()
        second = (
            await uow.repositories.runs.create_idempotent(
                user_id=first.user_id,
                conversation_id=first.conversation_id,
                idempotency_key=uuid4().hex,
                request_hash=uuid4().hex,
                scope_epoch=first.scope_epoch,
                auth_session_id=e_case.member.auth_session_id,
            )
        ).record
    assert await service.capture_dependencies(
        e_case.member, second.id, message_ids=(message.id,)
    ) == ("引用过上述来源",)
    async with e_case.uows() as uow:
        assert len(await uow.repositories.knowledge.sources(second.id)) == 2
        await uow.repositories.publications.revoke(
            e_case.resource_id, expected_version=0, expected_acl_version=1
        )
    with pytest.raises(ConflictError):
        await service.capture_dependencies(e_case.member, second.id, message_ids=(message.id,))

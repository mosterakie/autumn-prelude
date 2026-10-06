from uuid import uuid4

import pytest
from sqlalchemy import select

from autumn_backend.db.models import KnowledgeChunk, KnowledgeIndex
from autumn_backend.repositories.jobs import JobSpec
from autumn_backend.workers.bootstrap import configured_worker
from tests.integration.service_cases import ServiceCase
from tests.integration.test_knowledge_service import service_for

pytestmark = pytest.mark.integration


async def test_index_worker_public_projection_and_invalid_index_cleanup(
    e_case: ServiceCase,
) -> None:
    service = service_for(e_case)
    worker = configured_worker(e_case.uows, service.storage, knowledge=service)
    await service.request_index(
        e_case.owner, e_case.resource_id, publication_id=e_case.publication_id
    )
    assert await worker.run_once()
    async with e_case.uows() as uow:
        texts = list(await uow.session.scalars(select(KnowledgeChunk.content_text)))
        assert "私密备注" not in str(texts) and "公共正文" in texts
        resource = await uow.repositories.resources.get_for_update_or_raise(e_case.resource_id)
        await uow.repositories.resources.soft_delete(
            resource.id,
            expected_version=resource.version,
            expected_acl_version=resource.acl_version,
        )
        await uow.repositories.jobs.enqueue(
            JobSpec(
                kind="knowledge.cleanup",
                idempotency_key=uuid4().hex,
                resource_id=resource.id,
                payload={},
            )
        )
    assert await worker.run_once()
    async with e_case.uows() as uow:
        assert not list(
            await uow.session.scalars(
                select(KnowledgeIndex.id).where(KnowledgeIndex.is_active.is_(True))
            )
        )

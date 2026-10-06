from uuid import uuid4

import pytest

from autumn_backend.db.enums import JobStatus, RunStatus
from autumn_backend.errors import LeaseLostError
from autumn_backend.services.access import AuthorizationError
from autumn_backend.services.quota import QuotaService
from autumn_backend.services.runtime import RuntimeService
from tests.integration.service_cases import ServiceCase
from tests.integration.test_agent_runtime_service import accepted_job
from tests.integration.test_knowledge_service import build, service_for

pytestmark = pytest.mark.integration


async def test_epoch_invalidates_old_context_and_only_current_lease_can_stop(
    e_case: ServiceCase,
) -> None:
    knowledge = service_for(e_case)
    await build(e_case, knowledge, e_case.publication_id)
    job_id, token = await accepted_job(e_case)
    runtime = RuntimeService(e_case.uows)
    ticket = await runtime.open(job_id, token)
    await knowledge.capture_dependencies(ticket.actor, ticket.run_id)
    await knowledge.retrieve(ticket.actor, ticket.run_id, "正文")
    async with e_case.uows() as uow:
        await uow.repositories.publications.revoke(
            e_case.resource_id, expected_version=0, expected_acl_version=1
        )
    with pytest.raises(AuthorizationError):
        await runtime.guard(ticket)
    with pytest.raises(LeaseLostError):
        await runtime.stop(job_id, uuid4(), code="ACL_CONTEXT_INVALIDATED")
    await runtime.stop(job_id, token, code="ACL_CONTEXT_INVALIDATED")
    async with e_case.uows() as uow:
        run = await uow.repositories.runs.get_or_raise(ticket.run_id)
        assert run.status is RunStatus.FAILED and run.error_code == "ACL_CONTEXT_INVALIDATED"
        assert run.execution_generation == ticket.generation + 1
        assert run.config_snapshot["context_manifest"]["complete"] is False
        assert (await uow.repositories.jobs.get_or_raise(job_id)).status is JobStatus.FAILED
        assert await uow.repositories.knowledge.sources(run.id)
        assert not await uow.repositories.knowledge.sources(
            run.id, context_generation=run.execution_generation
        )
    quota = await QuotaService(e_case.uows).current(e_case.member)
    assert (quota.used, quota.reserved) == (0, 0)

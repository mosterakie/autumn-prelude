import pytest

from autumn_backend.agent.checkpoints import postgres_saver
from autumn_backend.db.enums import JobStatus
from autumn_backend.jobs.queue import LeasedJob
from autumn_backend.workers.handlers import agent_runtime, run_handlers
from tests.integration.service_cases import ServiceCase
from tests.integration.test_agent_runtime import Model
from tests.integration.test_agent_runtime_service import accepted_job
from tests.integration.test_knowledge_service import service_for

pytestmark = pytest.mark.integration


async def test_dispatch_with_official_postgres_saver_reopens_without_private_body(
    e_case: ServiceCase,
    database_url: str,
) -> None:
    job_id, token = await accepted_job(e_case)
    schema = "autumn_checkpoints_test_h"
    async with postgres_saver(database_url, schema=schema, initialize=True) as saver:
        runtime = agent_runtime(
            e_case.uows, service_for(e_case), Model({"kind": "reply", "text": "回复正文"}), saver
        )
        await run_handlers(runtime)["run.dispatch"](LeasedJob(job_id, "run.dispatch", token))
    async with postgres_saver(database_url, schema=schema) as restored:
        saved = [item async for item in restored.alist(None)]
        assert saved and any(str(job_id) not in str(item.checkpoint) for item in saved)
        assert "回复正文" not in str(saved) and "请根据获准资料回答" not in str(saved)
    async with e_case.uows() as uow:
        assert (await uow.repositories.jobs.get_or_raise(job_id)).status is JobStatus.SUCCEEDED

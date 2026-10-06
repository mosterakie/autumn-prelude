from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from autumn_backend.agent.context import ContextLoader
from autumn_backend.db.models import AuthSession
from autumn_backend.errors import NotFoundError
from autumn_backend.services.chats import ChatService
from autumn_backend.services.runtime import RuntimeService
from tests.integration.service_cases import ServiceCase
from tests.integration.test_agent_runtime_service import accepted_job
from tests.integration.test_knowledge_service import service_for

pytestmark = pytest.mark.integration


async def test_auth_restore_rebinds_database_identity_and_preserves_budget(
    e_case: ServiceCase,
) -> None:
    job_id, token = await accepted_job(e_case, owner=True)
    runtime = RuntimeService(e_case.uows)
    ticket = await runtime.open(job_id, token)
    knowledge = service_for(e_case)
    await ContextLoader(runtime, knowledge).load(ticket)
    await runtime.reserve(ticket, kind="tool")
    async with e_case.uows() as uow:
        old = await uow.repositories.auth_sessions.for_user_for_update(
            ticket.actor.auth_session_id, ticket.actor.user_id
        )
        now = await uow.repositories.users.database_time()
        old.revoked_at = now
        current = AuthSession(
            user_id=ticket.actor.user_id,
            token_hash=uuid4().hex,
            auth_version=old.auth_version,
            idle_expires_at=now + timedelta(hours=2),
            absolute_expires_at=now + timedelta(days=1),
            step_up_expires_at=now + timedelta(minutes=15),
        )
        uow.session.add(current)
        await uow.session.flush()
        actor = replace(
            ticket.actor, auth_session_id=current.id, step_up_expires_at=current.step_up_expires_at
        )
    await runtime.stop(job_id, token, code="SESSION_EXPIRED")
    chats = ChatService(e_case.uows)
    with pytest.raises(NotFoundError):
        await chats.resume(e_case.member, ticket.run_id, wait_id=None, answer=None, resume=True)
    await chats.resume(actor, ticket.run_id, wait_id=None, answer=None, resume=True)
    async with e_case.uows() as uow:
        job = await uow.repositories.jobs.claim(kinds=("run.resume",))
        assert job is not None and job.lease_token is not None
    restored = await runtime.open(job.id, job.lease_token)
    assert restored.actor.auth_session_id == current.id
    assert restored.generation == ticket.generation + 2
    assert restored.record.usage.tool_calls == 1
    assert (await ContextLoader(runtime, knowledge).load(restored)).history == ()
    async with e_case.uows() as uow:
        reservation = await uow.repositories.quota_reservations.for_run(restored.run_id)
        assert reservation.status.value == "reserved"

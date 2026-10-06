from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from autumn_backend.db.enums import ConversationMode, ProviderCallStatus, RunSourceType
from autumn_backend.db.models import AuthSession, ProviderCall
from autumn_backend.errors import LeaseLostError
from autumn_backend.knowledge.web import WebPage
from autumn_backend.policies.facts import SearchMode
from autumn_backend.services.access import AuthorizationError
from autumn_backend.services.context import TaskFence
from autumn_backend.services.runs import AcceptRunCommand, RunService
from autumn_backend.services.runtime import RuntimeService
from autumn_backend.services.web_search import SearchResult, WebSearchService
from tests.integration.service_cases import ServiceCase
from tests.integration.test_agent_runtime_service import accepted_job
from tests.integration.test_knowledge_service import service_for

pytestmark = pytest.mark.integration


async def web_ticket(e_case, message="请联网搜索秋序，引用来源"):
    async with e_case.uows() as uow:
        conversation = await uow.repositories.conversations.create(
            user_id=e_case.owner.user_id, mode=ConversationMode.OWNER
        )
    accepted = await RunService(e_case.uows).accept_run(
        e_case.owner,
        AcceptRunCommand(
            conversation_id=conversation.id,
            client_message_id=uuid4(),
            idempotency_key=uuid4().hex,
            message=message,
            search_mode=SearchMode.WEB,
        ),
    )
    async with e_case.uows() as uow:
        job = await uow.repositories.jobs.claim(kinds=("run.dispatch",))
        assert job is not None and job.run_id == accepted.run_id and job.lease_token is not None
    ticket = await RuntimeService(e_case.uows).open(job.id, job.lease_token)
    await service_for(e_case).capture_dependencies(ticket.actor, ticket.run_id)
    return ticket


class Search:
    provider, model = "test", "basic"

    def __init__(self, callback=None):
        self.count, self.callback = 0, callback

    async def search(self, query, *, limit, external_idempotency_key):
        self.count += 1
        if self.callback:
            await self.callback()
        return SearchResult(
            (WebPage("https://example.com/source", "来源", "摘要"),), "search-test", 1
        )


async def test_web_search_requires_current_owner_and_atomic_sources(e_case: ServiceCase):
    provider = Search()
    service = WebSearchService(e_case.uows, provider)
    job_id, token = await accepted_job(e_case)
    member = await RuntimeService(e_case.uows).open(job_id, token)
    await service_for(e_case).capture_dependencies(member.actor, member.run_id)
    with pytest.raises(AuthorizationError):
        await service.search(
            member.actor,
            member.run_id,
            "秋序",
            fence=TaskFence(job_id, token, member.generation),
            step=1,
        )
    assert provider.count == 0
    ticket = await web_ticket(e_case)
    fence = TaskFence(ticket.job_id, ticket.token, ticket.generation)
    with pytest.raises(LeaseLostError):
        await service.search(
            ticket.actor,
            ticket.run_id,
            "秋序",
            fence=TaskFence(ticket.job_id, uuid4(), ticket.generation),
            step=1,
        )
    assert provider.count == 0
    result = await service.search(ticket.actor, ticket.run_id, "秋序", fence=fence, step=1)
    assert result[0].text == "摘要" and provider.count == 1
    async with e_case.uows() as uow:
        sources = await uow.repositories.knowledge.sources(
            ticket.run_id, context_generation=ticket.generation
        )
        calls = (
            await uow.session.scalars(
                select(ProviderCall).where(ProviderCall.run_id == ticket.run_id)
            )
        ).all()
        assert len(sources) == 1 and sources[0].source_type is RunSourceType.WEB
        assert (
            len(calls) == 1
            and calls[0].status is ProviderCallStatus.SUCCEEDED
            and calls[0].search_units == 1
        )


async def test_late_search_drops_sources_if_owner_verification_expires(e_case: ServiceCase):
    ticket = await web_ticket(e_case)

    async def expire():
        async with e_case.uows() as uow:
            session = await uow.session.get(AuthSession, ticket.actor.auth_session_id)
            session.step_up_expires_at = await uow.repositories.users.database_time() - timedelta(
                seconds=1
            )

    provider = Search(expire)
    with pytest.raises(AuthorizationError):
        await WebSearchService(e_case.uows, provider).search(
            ticket.actor,
            ticket.run_id,
            "秋序",
            fence=TaskFence(ticket.job_id, ticket.token, ticket.generation),
            step=1,
        )
    async with e_case.uows() as uow:
        assert not await uow.repositories.knowledge.sources(
            ticket.run_id, context_generation=ticket.generation
        )
        call = (
            await uow.session.scalars(
                select(ProviderCall).where(ProviderCall.run_id == ticket.run_id)
            )
        ).one()
        assert call.status is ProviderCallStatus.DISPATCHED

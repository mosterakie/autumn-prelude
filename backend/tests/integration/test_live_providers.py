"""显式选择后才产生少量真实费用；只允许独立测试库，业务事务全部回滚。"""

import os

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url

from autumn_backend.agent.checkpoints import postgres_saver
from autumn_backend.config import Settings
from autumn_backend.db.enums import ProviderCallPurpose, ProviderCallStatus, RunStatus
from autumn_backend.db.models import Message, ProviderCall
from autumn_backend.providers.dashscope import DashScopeEmbedder
from autumn_backend.providers.deepseek import DeepSeekModel
from autumn_backend.providers.http import ProviderError, Transport
from autumn_backend.providers.tavily import TavilySearch
from autumn_backend.services.knowledge import KnowledgeService
from autumn_backend.services.web_search import WebSearchService
from autumn_backend.workers.handlers import agent_runtime
from tests.integration.service_cases import ServiceCase
from tests.integration.test_agent_runtime_service import accepted_job
from tests.integration.test_knowledge_service import build, service_for
from tests.integration.test_web_search_service import web_ticket

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("AUTUMN_LIVE_PROVIDER_TESTS") != "1", reason="真实付费验收需要显式启用"
    ),
]


class BoundedTransport(Transport):
    """每项用例最多两次模型调用；供应商错误只打印稳定代码。"""

    def __init__(self, client):
        super().__init__(client, timeout=45)
        self.counts = {"chat/completions": 0, "embeddings": 0, "search": 0}

    async def post(self, url, key, payload):
        kind = next(name for name in self.counts if url.endswith(name))
        self.counts[kind] += 1
        assert self.counts[kind] <= {"chat/completions": 2, "embeddings": 2, "search": 1}[kind]
        if kind == "chat/completions":
            payload = {**payload, "max_tokens": min(payload["max_tokens"], 512)}
        try:
            result = await super().post(url, key, payload)
        except ProviderError as error:
            print("Live provider:", kind, error.code)
            raise
        print("Live provider:", kind, "HTTP 200")
        if kind == "chat/completions":
            import json

            plan = json.loads(result["choices"][0]["message"]["content"])
            print("Live plan:", plan.get("kind"), plan.get("call", {}).get("name"))
        return result


def live_ports(client, database_url):
    assert (make_url(database_url).database or "").startswith("autumn_test_")
    settings = Settings()
    assert (
        settings.llm_api_key
        and settings.embedding_api_key
        and settings.embedding_base_url
        and settings.search_api_key
    )
    transport = BoundedTransport(client)
    model = DeepSeekModel(
        transport, settings.llm_api_key, base_url=settings.llm_base_url, model=settings.llm_model
    )
    embedder = DashScopeEmbedder(
        transport,
        settings.embedding_api_key,
        base_url=settings.embedding_base_url,
        model=settings.embedding_model,
    )
    search = TavilySearch(transport, settings.search_api_key)
    return transport, model, embedder, search


async def test_live_member_knowledge_reply_and_ledger(e_case: ServiceCase, database_url):
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        transport, model, embedder, _ = live_ports(client, database_url)
        knowledge = KnowledgeService(e_case.uows, service_for(e_case).storage, embedder)
        await build(e_case, knowledge, e_case.publication_id)
        job_id, token = await accepted_job(
            e_case,
            message="请调用一次 search_knowledge 检索公开标题与公共正文，然后根据返回资料用一句话回答并引用 source_id。不要调用其他工具，不要重复检索。",
        )
        # 验收输入明确要求一次检索，正式回复仍由真实模型自行规划。
        from autumn_backend.services.runtime import RuntimeService

        ticket = await RuntimeService(e_case.uows).open(job_id, token)
        async with postgres_saver(database_url, schema="autumn_checkpoints_test_h") as saver:
            try:
                outcome = await agent_runtime(e_case.uows, knowledge, model, saver).execute(
                    job_id, token
                )
                assert outcome.status is RunStatus.SUCCEEDED
                async with e_case.uows() as uow:
                    calls = (
                        await uow.session.scalars(
                            select(ProviderCall).where(ProviderCall.run_id == ticket.run_id)
                        )
                    ).all()
                    sources = await uow.repositories.knowledge.sources(
                        ticket.run_id, context_generation=ticket.generation
                    )
                    message = await uow.session.get(Message, outcome.message_id)
                    assert sources and message.body_text and "私密备注" not in message.body_text
                    assert all(call.status is ProviderCallStatus.SUCCEEDED for call in calls)
                    assert {call.purpose for call in calls} == {
                        ProviderCallPurpose.CHAT,
                        ProviderCallPurpose.EMBEDDING,
                    }
                    assert all(
                        call.external_request_id and call.input_tokens
                        for call in calls
                        if call.purpose is ProviderCallPurpose.CHAT
                    )
                assert transport.counts == {"chat/completions": 2, "embeddings": 2, "search": 0}
            finally:
                await saver.adelete_thread(ticket.thread_id)


async def test_live_owner_web_reply_and_ledger(e_case: ServiceCase, database_url):
    ticket = await web_ticket(
        e_case,
        "请调用一次 search_web 搜索 FastAPI 官方文档，limit 设为 1，然后用一句话说明 FastAPI 是什么并引用返回的 source_id。不要调用其他工具，不要重复搜索。",
    )
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        transport, model, embedder, search = live_ports(client, database_url)
        knowledge = KnowledgeService(e_case.uows, service_for(e_case).storage, embedder)
        async with postgres_saver(database_url, schema="autumn_checkpoints_test_h") as saver:
            try:
                outcome = await agent_runtime(
                    e_case.uows,
                    knowledge,
                    model,
                    saver,
                    web_search=WebSearchService(e_case.uows, search),
                ).execute(ticket.job_id, ticket.token)
                assert outcome.status is RunStatus.SUCCEEDED
                async with e_case.uows() as uow:
                    calls = (
                        await uow.session.scalars(
                            select(ProviderCall).where(ProviderCall.run_id == ticket.run_id)
                        )
                    ).all()
                    sources = await uow.repositories.knowledge.sources(
                        ticket.run_id, context_generation=ticket.generation
                    )
                    assert sources and all(
                        call.status is ProviderCallStatus.SUCCEEDED for call in calls
                    )
                    assert {call.purpose for call in calls} == {
                        ProviderCallPurpose.CHAT,
                        ProviderCallPurpose.SEARCH,
                    }
                assert transport.counts == {"chat/completions": 2, "embeddings": 0, "search": 1}
            finally:
                await saver.adelete_thread(ticket.thread_id)

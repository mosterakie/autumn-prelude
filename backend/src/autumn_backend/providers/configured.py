"""进程级真实供应商工厂；关闭 HTTP 连接，不为缺少配置提供假回复。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

from autumn_backend.config import Settings
from autumn_backend.providers.dashscope import DashScopeEmbedder
from autumn_backend.providers.deepseek import DeepSeekModel
from autumn_backend.providers.http import Transport
from autumn_backend.providers.mail import SMTPMailer
from autumn_backend.providers.tavily import TavilySearch


@dataclass(frozen=True, slots=True)
class Providers:
    model: DeepSeekModel | None
    embedder: DashScopeEmbedder | None
    search: TavilySearch | None
    mailer: SMTPMailer | None = None


@asynccontextmanager
async def providers(settings: Settings) -> AsyncIterator[Providers]:
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        transport = Transport(client, timeout=settings.provider_timeout_seconds)
        model = (
            DeepSeekModel(
                transport,
                settings.llm_api_key,
                base_url=settings.llm_base_url,
                model=settings.llm_model,
            )
            if settings.llm_api_key and settings.llm_api_key.get_secret_value().strip()
            else None
        )
        embedder = None
        if settings.embedding_api_key and settings.embedding_api_key.get_secret_value().strip():
            if not settings.embedding_base_url:
                raise ValueError("嵌入服务缺少 HTTPS 地址")
            embedder = DashScopeEmbedder(
                transport,
                settings.embedding_api_key,
                base_url=settings.embedding_base_url,
                model=settings.embedding_model,
            )
        search = (
            TavilySearch(transport, settings.search_api_key)
            if settings.search_api_key and settings.search_api_key.get_secret_value().strip()
            else None
        )
        mailer = SMTPMailer(settings) if settings.mail_enabled else None
        yield Providers(model, embedder, search, mailer)

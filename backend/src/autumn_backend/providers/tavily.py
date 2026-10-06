"""Tavily 基础搜索；只返回有限摘要，不自动抓取结果网页。"""

from pydantic import SecretStr

from autumn_backend.errors import InvalidInputError
from autumn_backend.knowledge.web import WebPage, validate_url
from autumn_backend.providers.http import ProviderError, Transport, request_id, units
from autumn_backend.services.web_search import SearchResult


class TavilySearch:
    provider, model = "tavily", "basic"

    def __init__(self, transport: Transport, key: SecretStr) -> None:
        self.transport, self.key = transport, key

    async def search(
        self, query: str, *, limit: int, external_idempotency_key: str
    ) -> SearchResult:
        if not query.strip() or len(query) > 8000 or not 1 <= limit <= 5:
            raise ProviderError("provider_search_input_invalid")
        result = await self.transport.post(
            "https://api.tavily.com/search",
            self.key,
            {
                "query": query,
                "max_results": limit,
                "search_depth": "basic",
                "topic": "general",
                "auto_parameters": False,
                "include_answer": False,
                "include_raw_content": False,
                "include_images": False,
                "include_usage": True,
            },
        )
        try:
            pages = []
            for item in result["results"][:limit]:
                try:
                    url = validate_url(item["url"])
                except InvalidInputError:
                    continue
                text = item["content"]
                title = item.get("title", "")
                if not isinstance(text, str) or not isinstance(title, str):
                    raise ProviderError("provider_search_response_invalid")
                if text.strip():
                    pages.append(WebPage(url, title[:300], text[:8000]))
            return SearchResult(
                tuple(pages),
                external_request_id=request_id(result.get("request_id")),
                search_units=units(result.get("usage", {}).get("credits")),
            )
        except (KeyError, TypeError, AttributeError):
            raise ProviderError("provider_search_response_invalid") from None

"""百炼 OpenAI 兼容嵌入；每次 HTTP 调用最多 10 段、固定 1024 维。"""

from pydantic import SecretStr

from autumn_backend.providers.http import ProviderError, Transport, endpoint
from autumn_backend.services.knowledge import validate_vectors


class DashScopeEmbedder:
    provider, batch_size = "dashscope", 10

    def __init__(
        self,
        transport: Transport,
        key: SecretStr,
        *,
        base_url: str,
        model: str = "text-embedding-v4",
    ) -> None:
        self.transport, self.key, self.model = transport, key, model
        self.url = endpoint(base_url, "/embeddings")

    async def embed(
        self, texts: tuple[str, ...], *, external_idempotency_key: str
    ) -> list[list[float]]:
        if not 1 <= len(texts) <= self.batch_size or any(not text.strip() for text in texts):
            raise ProviderError("provider_embedding_input_invalid")
        result = await self.transport.post(
            self.url,
            self.key,
            {
                "model": self.model,
                "input": list(texts),
                "dimensions": 1024,
                "encoding_format": "float",
            },
        )
        try:
            ordered = sorted(result["data"], key=lambda item: item["index"])
            if [item["index"] for item in ordered] != list(range(len(texts))):
                raise ProviderError("provider_embedding_order_invalid")
            vectors = [[float(value) for value in item["embedding"]] for item in ordered]
            validate_vectors(vectors, len(texts))
            return vectors
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ProviderError("provider_embedding_invalid") from None

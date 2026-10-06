"""DeepSeek JSON 计划适配器；本地取消不表示上游停止计费。"""

import asyncio
import json
from typing import Any

from pydantic import SecretStr

from autumn_backend.providers.http import ProviderError, Transport, endpoint, request_id, units
from autumn_backend.services.execution import ModelResult


class DeepSeekModel:
    provider = "deepseek"

    def __init__(
        self,
        transport: Transport,
        key: SecretStr,
        *,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-flash",
    ) -> None:
        self.transport, self.key, self.model = transport, key, model
        self.url = endpoint(base_url, "/chat/completions")
        self.pending: dict[str, asyncio.Task[Any]] = {}

    async def generate(
        self, prompt: str, *, max_output_tokens: int, external_idempotency_key: str
    ) -> ModelResult:
        if not 1 <= max_output_tokens <= 16384 or not external_idempotency_key:
            raise ProviderError("provider_request_invalid")
        # 由服务端构建的系统约束放在 system role，资料保持在 user role 的隔离区。
        document = json.loads(prompt)
        system = document.pop("system")
        trusted = {name: document.pop(name) for name in ("tools", "output_schema")}
        task = asyncio.current_task()
        assert task is not None
        if external_idempotency_key in self.pending:
            raise ProviderError("provider_request_duplicate")
        self.pending[external_idempotency_key] = task
        try:
            result = await self.transport.post(
                self.url,
                self.key,
                {
                    "model": self.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": system + "\n" + json.dumps(trusted, ensure_ascii=False),
                        },
                        {"role": "user", "content": json.dumps(document, ensure_ascii=False)},
                    ],
                    "response_format": {"type": "json_object"},
                    "thinking": {"type": "disabled"},
                    "max_tokens": max_output_tokens,
                    "stream": False,
                },
            )
            try:
                choice = result["choices"][0]
                text = choice["message"]["content"]
                usage = result.get("usage", {})
                if choice["finish_reason"] != "stop" or not isinstance(text, str) or not text:
                    raise ProviderError("provider_output_incomplete")
                return ModelResult(
                    text,
                    external_request_id=request_id(result.get("id")),
                    input_tokens=units(usage.get("prompt_tokens")),
                    output_tokens=units(usage.get("completion_tokens")),
                )
            except (KeyError, IndexError, TypeError, AttributeError):
                raise ProviderError("provider_response_invalid") from None
        finally:
            self.pending.pop(external_idempotency_key, None)

    async def cancel(self, *, external_idempotency_key: str) -> None:
        task = self.pending.get(external_idempotency_key)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

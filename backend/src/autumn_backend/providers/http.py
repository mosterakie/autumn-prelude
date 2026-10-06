"""有界、无重试的供应商 HTTP；错误不携带凭据、请求或响应正文。"""

import asyncio
import json
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from autumn_backend.io_boundary import require_outside_uow


class ProviderError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def endpoint(base: str, path: str) -> str:
    parsed = urlsplit(base)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("供应商地址必须是无凭据的 HTTPS 地址")
    return base.rstrip("/") + path


class Transport:
    def __init__(self, client: httpx.AsyncClient, *, timeout: float = 45) -> None:
        self.client, self.timeout = client, timeout

    async def post(self, url: str, key: SecretStr, payload: dict[str, Any]) -> dict[str, Any]:
        require_outside_uow()
        try:
            async with asyncio.timeout(self.timeout):
                return await self._post(url, key, payload)
        except TimeoutError:
            raise ProviderError("provider_transport_unknown") from None

    async def _post(self, url: str, key: SecretStr, payload: dict[str, Any]) -> dict[str, Any]:
        require_outside_uow()
        try:
            async with self.client.stream(
                "POST",
                url,
                headers={"Authorization": "Bearer " + key.get_secret_value()},
                json=payload,
                timeout=self.timeout,
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise ProviderError(f"provider_http_{response.status_code}")
                data = bytearray()
                async for block in response.aiter_bytes():
                    data.extend(block)
                    if len(data) > 2_000_000:
                        raise ProviderError("provider_response_too_large")
                result = json.loads(data)
                if not isinstance(result, dict):
                    raise ProviderError("provider_response_invalid")
                return result
        except ProviderError:
            raise
        except httpx.HTTPError:
            raise ProviderError("provider_transport_unknown") from None
        except (ValueError, UnicodeError):
            raise ProviderError("provider_response_invalid") from None


def units(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ProviderError("provider_usage_invalid")
    return value


def request_id(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 200:
        raise ProviderError("provider_request_id_invalid")
    return value

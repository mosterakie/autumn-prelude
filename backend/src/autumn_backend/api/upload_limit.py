"""在 multipart 解析之前限制上传请求体，包含没有 Content-Length 的流式请求。"""

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from autumn_backend.api.errors import error_response
from autumn_backend.services.storage import MAX_FILE_BYTES

MAX_UPLOAD_BODY = MAX_FILE_BYTES + 64 * 1024


class UploadBodyLimit:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] != "/api/knowledge/files":
            await self.app(scope, receive, send)
            return
        length = Headers(scope=scope).get("content-length")
        if length is not None:
            try:
                oversized = int(length) > MAX_UPLOAD_BODY
            except ValueError:
                oversized = True
            if oversized:
                response = error_response(Request(scope), status=413, code="UPLOAD_TOO_LARGE")
                await response(scope, receive, send)
                return
        size = 0

        async def limited_receive() -> Message:
            nonlocal size
            message = await receive()
            if message["type"] == "http.request":
                size += len(message.get("body", b""))
                if size > MAX_UPLOAD_BODY:
                    raise HTTPException(status_code=413)
            return message

        await self.app(scope, limited_receive, send)

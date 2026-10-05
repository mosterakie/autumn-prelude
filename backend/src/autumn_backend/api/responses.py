"""统一 JSON 输出；请求编号由服务端产生，不采信客户端传入值。"""

from datetime import UTC, datetime
from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from starlette.responses import JSONResponse


def success(request: Request, data: Any, *, status: int = 200) -> JSONResponse:
    return JSONResponse(
        jsonable_encoder(
            {"data": data, "request_id": request.state.request_id},
            custom_encoder={
                datetime: lambda value: value.astimezone(UTC).isoformat().replace("+00:00", "Z")
            },
        ),
        status_code=status,
    )

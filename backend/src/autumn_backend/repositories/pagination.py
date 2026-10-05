"""仅用于 created_at DESC, id DESC 的复合游标，不接受其它排序键。"""

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import UUID

from sqlalchemy import Select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from autumn_backend.db.base import Base
from autumn_backend.errors import InvalidInputError


@dataclass(frozen=True, slots=True)
class Cursor:
    created_at: datetime
    id: UUID

    def __post_init__(self) -> None:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise InvalidInputError("游标时间必须包含时区")

    def encode(self) -> str:
        value = json.dumps(
            {"v": 1, "created_at": self.created_at.astimezone(UTC).isoformat(), "id": str(self.id)},
            separators=(",", ":"),
        )
        return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, encoded: str) -> "Cursor":
        try:
            if len(encoded) > 512 or not re.fullmatch(r"[A-Za-z0-9_-]+", encoded):
                raise ValueError("invalid encoding")
            decoded = base64.b64decode(
                encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
            )
            payload = json.loads(decoded)
            if (
                not isinstance(payload, dict)
                or set(payload) != {"v", "created_at", "id"}
                or type(payload["v"]) is not int
                or payload["v"] != 1
            ):
                raise ValueError("invalid shape")
            if not isinstance(payload["created_at"], str) or not isinstance(payload["id"], str):
                raise ValueError("invalid field type")
            return cls(datetime.fromisoformat(payload["created_at"]), UUID(payload["id"]))
        except (ValueError, TypeError, OverflowError, binascii.Error) as error:
            raise InvalidInputError("游标无效，请从第一页重新读取") from error


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: list[T]
    next_cursor: str | None


class CursorRecord(Protocol):
    created_at: datetime
    id: UUID


async def fetch_page[T: Base](
    session: AsyncSession,
    model: type[T],
    scoped_statement: Select[Any],
    *,
    limit: int = 20,
    cursor: str | None = None,
) -> Page[T]:
    """scoped_statement 必须由具体 Repository 先加归属/可见性过滤。"""
    if not 1 <= limit <= 100:
        raise InvalidInputError("分页大小必须在 1 到 100 之间")
    table = model.__table__
    statement = scoped_statement.order_by(None).order_by(
        table.c.created_at.desc(), table.c.id.desc()
    )
    if cursor is not None:
        boundary = Cursor.decode(cursor)
        statement = statement.where(
            tuple_(table.c.created_at, table.c.id) < tuple_(boundary.created_at, boundary.id)
        )
    # Select 的类型参数在 SQLAlchemy 2.0/2.1 不同；结果实体由 model 参数限定。
    rows = cast(list[T], list((await session.scalars(statement.limit(limit + 1))).all()))
    items = rows[:limit]
    next_cursor = (
        Cursor(cast(CursorRecord, items[-1]).created_at, cast(CursorRecord, items[-1]).id).encode()
        if len(rows) > limit
        else None
    )
    return Page(items, next_cursor)

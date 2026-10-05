import base64
import json
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from autumn_backend.errors import InvalidInputError
from autumn_backend.repositories.pagination import Cursor

pytestmark = pytest.mark.unit


def encode_payload(payload: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def test_cursor_round_trip_normalizes_utc() -> None:
    moment = datetime(2026, 10, 5, 12, tzinfo=timezone(timedelta(hours=8)))
    original = Cursor(moment, uuid4())
    decoded = Cursor.decode(original.encode())
    assert decoded == original and decoded.created_at.tzinfo == UTC


@pytest.mark.parametrize(
    "encoded",
    [
        "",
        "!",
        "a",
        "é",
        "x" * 513,
        "e30",
        encode_payload([]),
        encode_payload({"v": 1, "created_at": "2026-10-05", "id": str(uuid4())}),
        encode_payload({"v": 1, "created_at": "invalid", "id": str(uuid4())}),
        encode_payload({"v": 1, "created_at": "2026-10-05T00:00:00Z", "id": "invalid"}),
        encode_payload({"v": True, "created_at": "2026-10-05T00:00:00Z", "id": str(uuid4())}),
        encode_payload({"v": 1, "created_at": 123, "id": str(uuid4())}),
    ],
)
def test_malformed_cursor_is_domain_bad_input(encoded: str) -> None:
    with pytest.raises(InvalidInputError):
        Cursor.decode(encoded)

"""纯值校验；不读取时钟、配置或数据库。"""

from datetime import datetime
from uuid import UUID


def aware_datetime(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def optional_datetime(value: datetime | None, name: str) -> None:
    if value is not None:
        aware_datetime(value, name)


def nonnegative_integer(value: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


def uuid_value(value: UUID, name: str) -> None:
    if not isinstance(value, UUID):
        raise ValueError(f"{name} must be a UUID")


def boolean_value(value: bool, name: str) -> None:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a bool")

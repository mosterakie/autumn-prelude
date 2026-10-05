"""结构化日志与脱敏。

定位（文档 §2 模块边界）：日志脱敏；**审计不等于日志**。
真实审计写入 ``audit_events`` 表，由 ``repositories`` 层承担，不在这里。
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

from autumn_backend.config import Settings, get_settings

# 一律脱敏的字段名（大小写不敏感，按子串匹配）。
_SENSITIVE_KEY_PARTS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "cookie",
    "authorization",
    "api_key",
    "apikey",
    "csrf",
    "totp",
    "otp",
    "session_id",
    "credit_card",
)

_REDACTED = "[redacted]"


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def redact_sensitive(
    _logger: Any,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> Mapping[str, Any]:
    """structlog processor：递归脱敏敏感字段。"""
    return _redact_mapping(event_dict)


def _redact_mapping(value: MutableMapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        if _is_sensitive(key):
            result[key] = _REDACTED
        elif isinstance(item, Mapping):
            result[key] = _redact_mapping(dict(item))
        elif isinstance(item, list | tuple):
            result[key] = [
                _redact_mapping(dict(entry)) if isinstance(entry, Mapping) else entry
                for entry in item
            ]
        else:
            result[key] = item
    return result


def _shared_processors() -> list[Any]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        redact_sensitive,
    ]


def configure_logging(settings: Settings | None = None) -> None:
    """初始化标准库 logging 与 structlog。重复调用是幂等的。"""
    resolved = settings or get_settings()

    renderer: Any = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if resolved.log_json
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[*_shared_processors(), renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[resolved.log_level]
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=logging.getLevelNamesMapping()[resolved.log_level],
        force=True,
    )


def get_logger(name: str | None = None, **initial_context: Any) -> structlog.stdlib.BoundLogger:
    """取得已绑定基础上下文的 logger。"""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    if initial_context:
        logger = logger.bind(**initial_context)
    return logger

"""pytest 共享夹具。

三层测试目录（架构文档 §2.1）：
- ``tests/unit``         无 I/O 的纯单元测试
- ``tests/integration``  需要真实 PostgreSQL 的集成测试
- ``tests/concurrency``  需要真实 PostgreSQL 的并发验收（阶段 C 硬闸门）

此处只提供**不依赖数据库**的通用夹具；真实数据库夹具在阶段 C1 加入
（那时才允许引入 testcontainers / 独立测试库的编排代码）。
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from autumn_backend.app import create_app
from autumn_backend.config import Environment, Settings, get_settings


@pytest.fixture(autouse=True)
def _isolated_settings_cache() -> Iterator[None]:
    """每个测试用独立的配置缓存，避免跨用例污染 ``get_settings()``。"""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    """测试用配置：不连数据库、不启用 JSON 日志、使用独立 Cookie 名。"""
    return Settings(
        environment=Environment.TEST,
        _env_file=None,  # type: ignore[call-arg]  # 测试不读 backend/.env
        database_url="postgresql+asyncpg://autumn:autumn@127.0.0.1:5432/autumn_test",
        cookie_name="autumn_session_test",
        cookie_secure=False,
        log_level="DEBUG",
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def real_database_url() -> str:
    """真实 PostgreSQL 连接串。

    阶段 C1 起，integration / concurrency 用例必须使用它；
    **禁止**用 SQLite 或 Mock Repository 替代（无法证明 ON CONFLICT /
    FOR UPDATE / SKIP LOCKED / Partial Unique Index / 隔离级别）。
    """
    url = os.environ.get("AUTUMN_TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "未设置 AUTUMN_TEST_DATABASE_URL："
            "真实 PostgreSQL 用例需要独立测试库（阶段 C1 起为硬闸门）"
        )
    return url

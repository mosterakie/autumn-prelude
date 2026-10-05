"""FastAPI 应用工厂。

定位（架构文档 §2）：api 只做请求解析、ActorContext 构造、调用 service、
SSE 与错误映射；不直接访问 ORM，不直接调 provider。

阶段 A1 只提供：
- 应用工厂与 lifespan（初始化日志、按需建立数据库引擎）
- 存活与就绪探针
阶段 F1 在此基础上接入 deps / errors / 统一响应与各业务路由。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from autumn_backend import __version__
from autumn_backend.config import Settings, get_settings
from autumn_backend.db.session import create_engine, create_session_factory
from autumn_backend.observability.logging import configure_logging, get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时准备配置、日志与（可选的）数据库引擎；关闭时释放连接池。"""
    settings: Settings = app.state.settings
    configure_logging(settings)

    engine: AsyncEngine | None = None
    # 本地开发与单元测试不强制要求数据库可达；生产必须连上才算启动成功。
    if settings.is_production or settings.environment.value == "dev":
        engine = create_engine(settings)
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)

    logger.info(
        "app.startup",
        version=__version__,
        environment=settings.environment.value,
        database_engine=engine is not None,
    )
    try:
        yield
    finally:
        if engine is not None:
            await engine.dispose()
        logger.info("app.shutdown", version=__version__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """构造 FastAPI 应用。测试可传入自定义 Settings。"""
    resolved = settings or get_settings()

    app = FastAPI(
        title="秋序 Autumn Prelude API",
        version=__version__,
        docs_url="/docs" if not resolved.is_production else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not resolved.is_production else None,
        lifespan=lifespan,
    )
    app.state.settings = resolved

    @app.get("/healthz", tags=["health"], summary="存活探针")
    async def healthz() -> dict[str, str]:
        """只表示进程存活，不检查依赖。"""
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", tags=["health"], summary="就绪探针")
    async def readyz() -> dict[str, Any]:
        """报告进程就绪状态与配置概况（不含任何密钥）。"""
        return {
            "status": "ok",
            "environment": resolved.environment.value,
            "database_configured": bool(resolved.async_database_url),
        }

    return app


app = create_app()

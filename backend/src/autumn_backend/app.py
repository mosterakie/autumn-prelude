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
from starlette.middleware.cors import CORSMiddleware

from autumn_backend import __version__
from autumn_backend.api.actions import router as action_router
from autumn_backend.api.auth import router as auth_router
from autumn_backend.api.chats import router as chat_router
from autumn_backend.api.comments import router as comment_router
from autumn_backend.api.errors import install_error_handlers
from autumn_backend.api.events import router as event_router
from autumn_backend.api.middleware import install_request_middleware
from autumn_backend.api.provider_reconciliation import router as reconciliation_router
from autumn_backend.api.public import router as public_router
from autumn_backend.api.quota import router as quota_router
from autumn_backend.api.resources import router as resource_router
from autumn_backend.api.tasks import router as task_router
from autumn_backend.auth.service import AuthService
from autumn_backend.config import Settings, get_settings
from autumn_backend.db.session import UnitOfWorkFactory, create_engine, create_session_factory
from autumn_backend.observability.logging import configure_logging, get_logger
from autumn_backend.services.actions import ActionService
from autumn_backend.services.chats import ChatService
from autumn_backend.services.comments import CommentService
from autumn_backend.services.events import EventService
from autumn_backend.services.provider_reconciliation import ProviderReconciliationService
from autumn_backend.services.public import PublicService
from autumn_backend.services.publication import PublicationService
from autumn_backend.services.quota import QuotaService
from autumn_backend.services.resources import ResourceService
from autumn_backend.services.runs import RunService
from autumn_backend.services.storage import StorageService
from autumn_backend.services.tasks import TaskService
from autumn_backend.storage.local import LocalObjectStore

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时准备配置、日志与（可选的）数据库引擎；关闭时释放连接池。"""
    settings: Settings = app.state.settings
    configure_logging(settings)

    engine: AsyncEngine | None = None
    # Engine 延迟连接；测试通过独立 UoW 注入，永不连接默认业务数据库。
    if not settings.is_test:
        engine = create_engine(settings)
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        app.state.uows = UnitOfWorkFactory(app.state.session_factory)
        app.state.auth = AuthService(app.state.uows, settings)
        app.state.storage = StorageService(app.state.uows, LocalObjectStore(settings.storage_root))
        app.state.resources = ResourceService(app.state.uows, app.state.storage)
        app.state.public = PublicService(app.state.uows, app.state.storage.store)
        app.state.actions = ActionService(app.state.uows)
        app.state.tasks = TaskService(app.state.uows)
        app.state.provider_reconciliation = ProviderReconciliationService(app.state.uows)
        app.state.publications = PublicationService(app.state.uows)
        app.state.chats = ChatService(app.state.uows)
        app.state.runs = RunService(app.state.uows, settings=settings)
        app.state.events = EventService(app.state.uows)
        app.state.comments = CommentService(app.state.uows)
        app.state.quota = QuotaService(app.state.uows, settings=settings)

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
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved.trusted_origins),
        allow_credentials=True,
        allow_methods=["GET", "HEAD", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-CSRF-Token", "Idempotency-Key", "Last-Event-ID"],
        expose_headers=["X-Request-ID", "Retry-After"],
    )
    install_error_handlers(app)
    install_request_middleware(app)
    app.include_router(auth_router)
    app.include_router(chat_router)
    app.include_router(event_router)
    app.include_router(resource_router)
    app.include_router(task_router)
    app.include_router(reconciliation_router)
    app.include_router(public_router)
    app.include_router(comment_router)
    app.include_router(action_router)
    app.include_router(quota_router)

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

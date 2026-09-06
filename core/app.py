"""应用工厂：uvicorn core.app:app 启动（127.0.0.1 环回监听）。

启动序列（spec-04 §2.6 起点简化版）：
  单实例锁(#41) → 数据目录/凭据准备 → DB migration → 路由装配 → 就绪。
"""
from __future__ import annotations

import logging
import sys
import time

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core import __version__
from core.api import CsrfMiddleware, SecurityHeadersMiddleware, api
from core.auth import router as auth_router
from core.conv import router as conv_router
from core.config import Settings, settings_from_override
from core.db import Connections, migrate
from core.security import InstanceLock

log = logging.getLogger("core")


def _setup_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def create_app(settings_override: dict | None = None) -> FastAPI:
    settings = settings_from_override(settings_override or {})
    _setup_logging(settings)

    app = FastAPI(
        title="AI Agent Trading core",
        version=__version__,
        docs_url=None if settings.env == "prod" else "/docs",
        redoc_url=None,
        openapi_url=None if settings.env == "prod" else "/openapi.json",
    )

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrate(settings.resolved_db_path())

    app.state.settings = settings
    app.state.db = Connections(settings.resolved_db_path())
    app.state.started_at = time.time()
    app.state.ws_clients = set()   # 站内通知（web）在线连接

    # #41 单实例锁（systemd 单实例运行，锁失败即拒绝启动并告警）
    app.state.instance_acquired = False
    if settings.single_instance_lock:
        lock = InstanceLock(settings.resolved_lock_path())
        if not lock.acquire():
            sys.exit(1)
        app.state.instance_lock = lock
        app.state.instance_acquired = True
    else:
        app.state.instance_lock = None
    log.info("core 启动：%s env=%s data_dir=%s lock=%s",
             __version__, settings.env, settings.data_dir, app.state.instance_acquired)

    # 登录速率限制器挂到 app.state（auth 端点读取）
    from core.security import LoginRateLimiter

    app.state.login_limiter = LoginRateLimiter(
        max_attempts=settings.login_rate_max,
        window_s=settings.login_rate_window_s,
        lock_s=settings.login_lock_seconds,
    )

    # 中间件注册（后注册先执行：CSRF 校验在安全头外层之前）
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(CsrfMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[] if settings.env == "prod" else ["http://localhost:5173",
                                                        "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api)
    app.include_router(auth_router)
    app.include_router(conv_router)

    @app.get("/")
    def root():
        return {"service": "ai-agent-trading-core", "version": __version__,
                "docs": "/docs" if settings.env != "prod" else None}

    return app


app = create_app()

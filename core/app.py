"""应用工厂：uvicorn core.app:app 启动（127.0.0.1 环回监听）。

启动序列（spec-04 §2.6 起点简化版）：
  单实例锁(#41) → 数据目录/凭据准备 → DB migration → 路由装配 → 就绪。
"""
from __future__ import annotations

import logging
import sys
import time
import asyncio

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core import __version__
from core.account_routes import router as account_router
from core.api import CsrfMiddleware, SecurityHeadersMiddleware, api
from core.approval_routes import router as approval_router
from core.auth import router as auth_router
from core.conv import router as conv_router
from core.config import Settings, settings_from_override
from core.db import Connections, migrate
from core.exit_tracking_routes import router as exit_tracking_router
from core.kb_routes import router as kb_router
from core.tasks_routes import router as tasks_router
from core.scheduler_routes import router as scheduler_router
from core.analytics_routes import router as analytics_router
from core.report_routes import router as report_router
from core.security import InstanceLock
from core.strategy_profile_routes import router as strategy_profile_router
from core.strategy_memory_routes import router as strategy_memory_router
from core.strategy_versions_routes import router as strategy_versions_router
from core.validation_windows_routes import router as validation_windows_router
from core.capability_center_routes import router as capability_center_router
from core.quality_routes import router as quality_router
from core.performance_routes import router as performance_router
from core.llm_routes import router as llm_router
from core.market_routes import router as market_router

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
    app.include_router(account_router)
    app.include_router(report_router)
    app.include_router(approval_router)
    app.include_router(kb_router)
    app.include_router(tasks_router)
    app.include_router(scheduler_router)
    app.include_router(analytics_router)
    app.include_router(strategy_profile_router)
    app.include_router(exit_tracking_router)
    app.include_router(strategy_memory_router)
    app.include_router(strategy_versions_router)
    app.include_router(validation_windows_router)
    app.include_router(capability_center_router)
    app.include_router(quality_router)
    app.include_router(performance_router)
    app.include_router(llm_router)
    app.include_router(market_router)

    if settings.eod_auto_settle:
        # EOD 结算自动触发（spec-04 §2.2 第 2 项）：core 常驻内唯一结算触发点
        from datetime import datetime as _dt

        from core.settle_scheduler import EodSettleTrigger

        def _hm(value: str):
            return _dt.strptime(value, "%H:%M").time()

        trigger = EodSettleTrigger(
            app.state,
            earliest=_hm(settings.eod_settle_earliest),
            retry_until=_hm(settings.eod_settle_retry_until),
        )

        @app.on_event("startup")
        async def _start_eod_settle():
            # spec-04 §2.6 启动第 4 步：进入 tick 前先快进补齐缺失交易日（幂等补跑）
            try:
                await asyncio.to_thread(trigger.catchup_missed)
            except Exception:  # noqa: BLE001
                log.exception("EOD 快进回放启动失败（继续进入 tick 循环）")
            app.state.settle_task = asyncio.create_task(
                trigger.run_forever(settings.eod_settle_tick_s)
            )
            log.info("EOD 结算自动触发已启用：最早 %s，重试至 %s，tick %ss",
                     settings.eod_settle_earliest, settings.eod_settle_retry_until,
                     settings.eod_settle_tick_s)

        @app.on_event("shutdown")
        async def _stop_eod_settle():
            task = getattr(app.state, "settle_task", None)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    if settings.market_data_enabled:
        # spec-03 §3.5 交易时段采集器常驻（web 部署版进程级在线保障：交易时段不中断）
        from core.collector import MarketCollector

        app.state.collector = MarketCollector(app.state)

        @app.on_event("startup")
        async def _start_collector():
            app.state.collector_task = asyncio.create_task(
                app.state.collector.run_forever(3)
            )
            log.info("spec-03 数据服务已启用：源=%s，采集间隔 3s",
                     app.state.collector.configured)

        @app.on_event("shutdown")
        async def _stop_collector():
            task = getattr(app.state, "collector_task", None)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    if settings.scheduler_auto_tick:
        # spec-04 §2.2/§2.5 常驻空闲巡检：非交易时段且资源余量足时执行可延迟任务
        from core.scheduler import SchedulerEngine

        app.state.scheduler_engine = SchedulerEngine(
            app.state, deferrable_limit=settings.scheduler_deferrable_limit)

        @app.on_event("startup")
        async def _start_scheduler():
            app.state.scheduler_task = asyncio.create_task(
                app.state.scheduler_engine.run_forever(settings.scheduler_tick_s)
            )
            log.info("调度器常驻巡检已启用：tick %ss，可延迟上限 %s",
                     settings.scheduler_tick_s, settings.scheduler_deferrable_limit)

        @app.on_event("shutdown")
        async def _stop_scheduler():
            task = getattr(app.state, "scheduler_task", None)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    @app.get("/")
    def root():
        return {"service": "ai-agent-trading-core", "version": __version__,
                "docs": "/docs" if settings.env != "prod" else None}

    return app


app = create_app()

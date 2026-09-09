"""性能监控路由（spec-04 §9，P3）：现状快照 + 进程内实时态。

- GET /api/performance/live   实时任务/回执链/审批/结算新鲜度快照
数值来自 core.performance_live.snapshot（持久行）并补 app.state 实时态：
uptime、ws 在线、引擎桩延迟（engine_stub_delay_ms）。时序曲线无独立埋点，不做伪图。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from core import performance_live
from core.auth import require_session

router = APIRouter(prefix="/api", tags=["performance"])

SessionDep = Annotated[dict, Depends(require_session)]


@router.get("/performance/live")
def live_performance(request: Request, session: SessionDep):
    state = request.app.state
    started_ts = getattr(state, "started_at", None)
    out = performance_live.snapshot(state, started_ts=started_ts)
    settings = getattr(state, "settings", None)
    out["process"] = {
        "ws_clients": len(getattr(state, "ws_clients", set())),
        "engine_stub_delay_ms": int(getattr(settings, "engine_stub_delay_ms", 0)),
    }
    return out

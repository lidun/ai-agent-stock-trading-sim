"""市场与数据质量监控只读路由（spec-02 §7 / spec-03，替换 P2 占位页数据源）。

- GET /api/quality/monitor?days=60   数据质量监控聚合（结算/成交/卖出跟踪/账户态）
实现 core/quality_monitor.monitor，纯读既有引擎落库表；无数据返回空聚合而非伪值。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from core import quality_monitor
from core.auth import require_session

router = APIRouter(prefix="/api", tags=["quality"])

SessionDep = Annotated[dict, Depends(require_session)]


@router.get("/quality/monitor")
def quality_monitor_endpoint(request: Request, session: SessionDep,
                             days: int = Query(default=60, ge=1, le=365)):
    out = quality_monitor.monitor(request.app.state, recent_days=days)
    settings = getattr(request.app.state, "settings", None)
    if settings is not None:
        out["eod_auto_settle"] = bool(getattr(settings, "eod_auto_settle", False))
    return out

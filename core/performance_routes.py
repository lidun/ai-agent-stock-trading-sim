"""性能监控路由（spec-04 §9，P3）：现状快照 + 进程内实时态。

- GET /api/performance/live   实时任务/回执链/审批/结算新鲜度快照
- GET /api/usage/daily        近 N 日 LLM 费用日聚合（spec-02 §11，元/日）
- GET /api/usage/group        近 N 日按 Agent×任务类型费用归属（元/Agent/任务类型三维）
- GET /api/usage/ledger       最近费用台账（对账抽样）
- GET /api/usage/pricing      单价表（provider_pricing，按 provider/model 过滤）
- POST /api/usage/pricing     配置单价（spec-02 §11：价格变动仅改表，不写死）
数值来自 core.performance_live.snapshot（持久行）并补 app.state 实时态：
uptime、ws 在线、引擎桩延迟（engine_stub_delay_ms）。时序曲线无独立埋点，不做伪图。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from core import perf_records, performance_live
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


@router.get("/usage/daily")
def usage_daily(request: Request, session: SessionDep, days: int = 14):
    return {"days": perf_records.daily_summary(
        request.app.state, days=max(1, min(int(days), 60)))}


@router.get("/usage/group")
def usage_group(request: Request, session: SessionDep, days: int = 14):
    return {"rows": perf_records.agent_task_summary(
        request.app.state, days=max(1, min(int(days), 60)))}


@router.get("/usage/ledger")
def usage_ledger(request: Request, session: SessionDep, limit: int = 50,
                 agent_id: str = "", task_type: str = ""):
    return {"rows": perf_records.ledger(
        request.app.state, limit=limit, agent_id=agent_id, task_type=task_type)}


@router.get("/usage/pricing")
def usage_pricing_get(request: Request, session: SessionDep,
                      provider: str = "", model: str = ""):
    return {"rows": perf_records.pricing_list(
        request.app.state, provider=provider, model=model)}


@router.post("/usage/pricing")
def usage_pricing_upsert(request: Request, session: SessionDep,
                         payload: dict | None = Body(default=None)):
    actor = session["session"]["username"]
    body = payload or {}
    try:
        price = perf_records.pricing_upsert(
            request.app.state, actor=actor,
            provider=body.get("provider", ""),
            model=body.get("model", ""),
            input_per_1k=float(body.get("input_per_1k") or 0),
            output_per_1k=float(body.get("output_per_1k") or 0),
            cache_read_per_1k=(float(body["cache_read_per_1k"])
                               if body.get("cache_read_per_1k") is not None else None),
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "price": price}

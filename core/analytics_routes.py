"""策略分析路由（spec-06 §6.4 P2 数据源：资金曲线/指标摘要卡/策略演进）。

- GET /api/agents/{agent_id}/equity-curve?range=all|1m|3m   净值曲线（各角色账户）+ 沪深300 基准
- GET /api/agents/{agent_id}/metrics                         指标卡：累计收益率/最大回撤/卖出信号胜率
- GET /api/agents/{agent_id}/evolution                       策略演进账本（账户角色 + 试运行验收结论）
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from core import analytics
from core.auth import require_session

router = APIRouter(prefix="/api", tags=["analytics"])

SessionDep = Annotated[dict, Depends(require_session)]


def _ok(state, agent_id: str, fn, **kw):
    try:
        return fn(state, agent_id, **kw)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/agents/{agent_id}/equity-curve")
def agent_equity_curve(agent_id: str, request: Request, session: SessionDep,
                       range: str = "all"):
    if range not in ("all", "1m", "3m"):
        raise HTTPException(status_code=400, detail="range 须为 all|1m|3m")
    return _ok(request.app.state, agent_id, analytics.equity_curve, span=range)


@router.get("/agents/{agent_id}/metrics")
def agent_metrics(agent_id: str, request: Request, session: SessionDep):
    return _ok(request.app.state, agent_id, analytics.metrics)


@router.get("/agents/{agent_id}/evolution")
def agent_evolution(agent_id: str, request: Request, session: SessionDep):
    return _ok(request.app.state, agent_id, analytics.evolution)

"""EVOQUANT 验证窗台账只读路由（spec-05 §4.2/§4.3，spec-06 策略页数据源）。

- GET /api/agents/{agent_id}/validation-windows            窗台账（新→旧）
- GET /api/agents/{agent_id}/validation-windows/{version_no}  单版本验证窗

写方=引擎（EodSettleTrigger 每会话日推进 + 满窗确定性判定，见 core.evoquant），
不开放 HTTP 写路由（与 strategy_versions/strategy_memory 同策略）。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from core import evoquant
from core.auth import require_session
from core.db import state_conn

router = APIRouter(prefix="/api", tags=["validation-windows"])

SessionDep = Annotated[dict, Depends(require_session)]


def _agent_exists(state, agent_id: str) -> None:
    row = state_conn(state).execute(
        "SELECT 1 FROM agents WHERE id=?", (agent_id,)
    ).fetchone()
    if row is None:
        raise LookupError("Agent 不存在")


@router.get("/agents/{agent_id}/validation-windows")
def windows_list(agent_id: str, request: Request, session: SessionDep):
    state = request.app.state
    try:
        _agent_exists(state, agent_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    items = evoquant.list_windows(state, agent_id)
    return {"agent_id": agent_id, "total": len(items), "items": items}


@router.get("/agents/{agent_id}/validation-windows/{version_no}")
def windows_get(agent_id: str, version_no: str, request: Request,
                session: SessionDep):
    w = evoquant.get_window(request.app.state, agent_id, version_no)
    if w is None:
        raise HTTPException(status_code=404, detail="该版本无验证窗记录")
    return {"window": w}

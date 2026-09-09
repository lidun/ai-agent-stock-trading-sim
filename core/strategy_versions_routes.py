"""策略版本演进只读路由（spec-02 §9 / spec-06 策略页数据源）。

- GET /api/agents/{agent_id}/strategy-versions?status=&limit=   演进史（新→旧）
- GET /api/agents/{agent_id}/strategy-versions/{version_no}     单版本快照
写方=引擎 EVOQUANT 优化流（core.strategy_versions.checkpoint/activate/rollback），
不开放 HTTP 写路由（与 strategy_memory 同策略）。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from core import strategy_versions
from core.auth import require_session

router = APIRouter(prefix="/api", tags=["strategy-versions"])

SessionDep = Annotated[dict, Depends(require_session)]


@router.get("/agents/{agent_id}/strategy-versions")
def versions_list(agent_id: str, request: Request, session: SessionDep,
                  status: str = Query(default=""), limit: int = Query(default=200)):
    try:
        return strategy_versions.list_versions(
            request.app.state, agent_id, status=status, limit=limit)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/agents/{agent_id}/strategy-versions/{version_no}")
def versions_get(agent_id: str, version_no: str, request: Request,
                 session: SessionDep):
    try:
        v = strategy_versions.get_version(request.app.state, agent_id, version_no)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if v is None:
        raise HTTPException(status_code=404, detail="版本不存在")
    return {"version": v}

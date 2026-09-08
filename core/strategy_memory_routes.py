"""演进记忆只读路由（spec-06 §6.4 策略演进卡；spec-02 §3.1 type=strategy）。

- GET /api/agents/{agent_id}/strategy-memory?version_no=&limit=  演进记忆（ts 降序）
公开写方=引擎 EVOQUANT 优化流（spec-05 §4.1），本片只读（壳）。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from core import strategy_memory
from core.auth import require_session

router = APIRouter(prefix="/api", tags=["strategy-memory"])

SessionDep = Annotated[dict, Depends(require_session)]


@router.get("/agents/{agent_id}/strategy-memory")
def agent_strategy_memory(agent_id: str, request: Request, session: SessionDep,
                          version_no: str = Query(default=""),
                          limit: int = Query(default=100, ge=1, le=200)):
    try:
        return strategy_memory.list_strategy_memory(
            request.app.state, agent_id, version_no=version_no, limit=limit)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

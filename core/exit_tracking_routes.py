"""卖出跟踪只读路由（spec-06 §6.4 P3 卖出跟踪列表，#15）。

- GET /api/agents/{agent_id}/exit-trackings?status=tracking|done  卖出验证列表
引擎侧登记/推进/结论判定不动；本片仅读展示。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from core import exit_tracking
from core.auth import require_session

router = APIRouter(prefix="/api", tags=["exit-tracking"])

SessionDep = Annotated[dict, Depends(require_session)]


@router.get("/agents/{agent_id}/exit-trackings")
def agent_exit_trackings(agent_id: str, request: Request, session: SessionDep,
                         status: str = ""):
    try:
        return exit_tracking.list_trackings(request.app.state, agent_id, status=status)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

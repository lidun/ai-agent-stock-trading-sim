"""策略章程只读路由（spec-06 §6.4 策略理念区块；spec-05 §4.1 双层结构语义）。

- GET /api/agents/{agent_id}/strategy-profile            当前章程（active 全量 + 版本摘要）
- GET /api/agents/{agent_id}/strategy-profile/versions/{version_no}   单版本全量（展开读取）
写入口后置：写入方=策略发布/管理 Agent（spec-05 §5）；本片仅读。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from core import strategy_profile
from core.auth import require_session

router = APIRouter(prefix="/api", tags=["strategy-profile"])

SessionDep = Annotated[dict, Depends(require_session)]


def _ok(state, agent_id: str, fn, **kw):
    try:
        return fn(state, agent_id, **kw)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/agents/{agent_id}/strategy-profile")
def agent_strategy_profile(agent_id: str, request: Request, session: SessionDep):
    return _ok(request.app.state, agent_id, strategy_profile.profile)


@router.get("/agents/{agent_id}/strategy-profile/versions/{version_no}")
def agent_strategy_profile_version(agent_id: str, version_no: str,
                                   request: Request, session: SessionDep):
    return _ok(request.app.state, agent_id, strategy_profile.version_detail,
               version_no=version_no)

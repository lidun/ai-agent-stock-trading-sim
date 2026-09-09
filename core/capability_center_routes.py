"""能力配置中心只读路由（spec-05 §2，统一域展示）。

- GET /api/capabilities?type=&status=&keyword=&limit=        能力目录（含在绑数）
- GET /api/capabilities/{capability_id}                     详情 + 绑定留痕
- GET /api/agents/{agent_id}/capability-bindings?include_unbound=
-                                                             Agent 绑定清单
- POST /api/capabilities/{capability_id}/unbind {agent_id}   管理侧解绑（审计，spec-05 §2.4）
下发入口=管理 Agent + spec-04 审批流（§2.3）；解绑为管理侧维护操作（spec-06 §6.8）。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from core import capability_center
from core.auth import require_session

router = APIRouter(prefix="/api", tags=["capabilities"])

SessionDep = Annotated[dict, Depends(require_session)]


@router.get("/capabilities")
def list_capabilities(request: Request, session: SessionDep,
                      type: str = Query(default=""),
                      status: str = Query(default=""),
                      keyword: str = Query(default=""),
                      limit: int = Query(default=200, ge=1, le=500)):
    if type and type not in capability_center.CAP_TYPES:
        raise HTTPException(status_code=400, detail=f"能力类型不合法：{type}")
    if status and status not in capability_center._STATUS:
        raise HTTPException(status_code=400, detail=f"状态不合法：{status}")
    try:
        return capability_center.catalog(
            request.app.state, capability_type=type, status=status,
            keyword=keyword, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/capabilities/{capability_id}")
def get_capability(capability_id: str, request: Request, session: SessionDep):
    try:
        return capability_center.detail(request.app.state, capability_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/agents/{agent_id}/capability-bindings")
def agent_capability_bindings(agent_id: str, request: Request, session: SessionDep,
                              include_unbound: bool = Query(default=False)):
    try:
        return capability_center.agent_bindings(
            request.app.state, agent_id, include_unbound=include_unbound)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/capabilities/{capability_id}/unbind")
def unbind_capability(capability_id: str, request: Request, session: SessionDep,
                      payload: dict | None = Body(default=None)):
    actor = session["session"]["username"]
    agent_id = (payload or {}).get("agent_id", "")
    if not agent_id:
        raise HTTPException(status_code=400, detail="须指定待解绑 Agent（agent_id）")
    try:
        out = capability_center.unbind(
            request.app.state, capability_id=capability_id,
            agent_id=agent_id, audit_actor=actor)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, **out}

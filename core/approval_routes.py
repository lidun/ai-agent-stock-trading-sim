"""审批域路由（spec-04 §4 审批流落地 / spec-06 §6.10 审批中心数据源）。

- GET /api/approvals              审批单列表（status/agent_id 过滤，新→旧）
- GET /api/approvals/{id}         单张详情
- POST /api/approvals             提交申请（确定性短路结果以 ok:false+reason 返回，
                                  pending_full 附待决单入口提示，spec-04 §4.2 B7）
- PATCH /api/approvals/{id}/decision  通过/驳回（效果器生效；迟到/二次决仅留痕）

管理 Agent LLM 未接入：决定方=登录用户（decided_by=user），评审人工执行。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from core import approval
from core.auth import require_session
from core.chatstore import get_agent

router = APIRouter(prefix="/api", tags=["approvals"])

SessionDep = Annotated[dict, Depends(require_session)]


def _ensure_agent(state, agent_id: str):
    row = get_agent(state, agent_id)
    if row is None:
        raise HTTPException(status_code=404,
                            detail="Agent 不存在（审批单须挂某个 Agent）")
    return row


@router.get("/approvals")
def approvals_list(request: Request, session: SessionDep,
                   agent_id: str | None = None, status: str | None = None,
                   limit: int = 100):
    if status and status not in approval.APPROVAL_STATUS:
        raise HTTPException(status_code=400, detail=f"未知状态：{status}")
    return {
        "approvals": approval.list_approvals(
            request.app.state, agent_id=agent_id, status=status, limit=limit),
    }


@router.get("/approvals/{approval_id}")
def approvals_detail(approval_id: str, request: Request, session: SessionDep):
    ap = approval.get_approval(request.app.state, approval_id)
    if ap is None:
        raise HTTPException(status_code=404, detail="审批单不存在")
    return {"approval": ap}


@router.post("/approvals")
def approvals_submit(request: Request, session: SessionDep,
                     payload: dict | None = Body(default=None)):
    actor = session["session"]["username"]
    body = payload or {}
    _ensure_agent(request.app.state, body.get("agent_id", ""))
    try:
        out = approval.submit_approval(
            request.app.state, type_=body.get("type", ""),
            agent_id=body.get("agent_id", ""), payload=body.get("payload", {}),
            reason=body.get("reason", ""), requested_by=actor,
            expires_seconds=body.get("expires_seconds"),
            cooldown_exempt=bool(body.get("cooldown_exempt", False)),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return out


@router.patch("/approvals/{approval_id}/decision")
def approvals_decide(approval_id: str, request: Request, session: SessionDep,
                     payload: dict | None = Body(default=None)):
    actor = session["session"]["username"]
    body = payload or {}
    try:
        out = approval.decide_approval(
            request.app.state, approval_id,
            decision=body.get("decision", ""), reason=body.get("reason", ""),
            decided_by=actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if out.get("reason") == "not_found":
        raise HTTPException(status_code=404, detail="审批单不存在")
    return out

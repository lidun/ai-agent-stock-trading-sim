"""上下文装配路由（spec-02 §7.0 模板 / §7.1 assemble）。"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Request

from core import context_assembly
from core.auth import require_session

router = APIRouter(prefix="/api", tags=["context"])

SessionDep = Annotated[dict, Depends(require_session)]


@router.get("/context/template")
def get_template(request: Request, session: SessionDep, agent_id: str):
    tpl = context_assembly.active_template(request.app.state, agent_id)
    return {"agent_id": agent_id, "template": tpl,
            "route_table": context_assembly.route_table(request.app.state, agent_id)}


@router.post("/context/template")
def post_template(request: Request, session: SessionDep,
                  payload: dict = Body(...)):
    actor = session["session"]["username"]
    return context_assembly.create_template(
        request.app.state, payload.get("agent_id", ""),
        route_table_=payload.get("route_table"),
        block_order=payload.get("block_order"),
        created_by=payload.get("created_by", "manager"), actor=actor)


@router.post("/context/assemble")
def post_assemble(request: Request, session: SessionDep,
                  payload: dict = Body(...)):
    return context_assembly.assemble(
        request.app.state, payload.get("agent_id", ""),
        payload.get("task_intent", ""),
        payload.get("task_payload") or {},
        top_k=int(payload.get("top_k", 5)))

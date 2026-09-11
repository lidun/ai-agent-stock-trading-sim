"""记忆检索路由（spec-02 §3.2 retrieve/get_full）。"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query, Request

from core import memory_retrieval, memory_rollout
from core.auth import require_session

router = APIRouter(prefix="/api", tags=["memory"])

SessionDep = Annotated[dict, Depends(require_session)]


@router.get("/memory/retrieve")
def memory_retrieve(request: Request, session: SessionDep,
                    agent_id: str, query: str = "",
                    intent: str = "", top_k: int = 5):
    return memory_retrieval.retrieve(
        request.app.state, agent_id, query, intent=intent, top_k=top_k)


@router.get("/memory/backlog")
def memory_backlog(request: Request, session: SessionDep,
                   agent_id: str = "", limit: int = 200):
    return {"items": memory_retrieval.card_backlog(
        request.app.state, agent_id=agent_id, limit=limit)}


@router.get("/memory/inspections")
def memory_inspections(request: Request, session: SessionDep,
                       agent_id: str, limit: int = 100):
    return {"items": memory_rollout.list_inspections(
        request.app.state, agent_id, limit=limit)}


@router.post("/memory/inspect")
def memory_inspect(request: Request, session: SessionDep,
                   payload: dict = Body(...)):
    actor = session["session"]["username"]
    return memory_rollout.inspect_period(
        request.app.state, payload.get("agent_id", ""),
        payload.get("period", "day"), payload.get("period_key", ""),
        sample_ratio=payload.get("sample_ratio"), actor=actor)


@router.post("/memory/rollout")
def memory_rollout_(request: Request, session: SessionDep,
                    payload: dict = Body(...)):
    actor = session["session"]["username"]
    return memory_rollout.roll_market_notes(
        request.app.state, agent_id=payload.get("agent_id", ""),
        before=payload.get("before", ""),
        limit=int(payload.get("limit", 1000)), actor=actor)


@router.get("/memory/{memory_id}/full")
def memory_full(request: Request, session: SessionDep, memory_id: str,
                agent_id: str = Query(...)):
    return memory_retrieval.get_full(request.app.state, agent_id, memory_id)

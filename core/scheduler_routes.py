"""调度器只读/触发路由（spec-04 §2.2 tick / §7.2 闸门状态）。"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Request

from core import scheduler
from core.auth import require_session

router = APIRouter(prefix="/api", tags=["scheduler"])

SessionDep = Annotated[dict, Depends(require_session)]


@router.get("/scheduler/status")
def scheduler_status(request: Request, session: SessionDep):
    return scheduler.status(request.app.state)


@router.post("/scheduler/tick")
def scheduler_tick(request: Request, session: SessionDep,
                    payload: dict | None = Body(default=None)):
    body = payload or {}
    actor = session["session"]["username"]
    return scheduler.tick(
        request.app.state, deferrable_limit=int(body.get("deferrable_limit", 10)),
        actor=actor)

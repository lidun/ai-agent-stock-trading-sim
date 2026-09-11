"""调度任务域路由（spec-04 §2.1/§2.5 最小实现）。

- GET  /api/tasks        任务列表（status/task_type/agent_id/deferrable/limit）
- POST /api/tasks        手动入队（幂等；tick 循环未落地前的补录入口）
- POST /api/tasks/run    认领并执行到期可延迟任务（空闲窗口调用点）

写操作经 CSRF 校验并审计 task.* 留痕。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from core import tasks
from core.auth import require_session

router = APIRouter(prefix="/api", tags=["tasks"])

SessionDep = Annotated[dict, Depends(require_session)]


@router.get("/tasks")
def task_list(request: Request, session: SessionDep, status: str = "",
              task_type: str = "", agent_id: str = "",
              deferrable: str = "", limit: int = 100):
    is_def = None
    if deferrable != "":
        is_def = deferrable.lower() in ("1", "true", "yes")
    return {"tasks": tasks.list_tasks(
        request.app.state, status=status, task_type=task_type,
        agent_id=agent_id, is_deferrable=is_def, limit=limit)}


@router.post("/tasks")
def task_enqueue(request: Request, session: SessionDep,
                 payload: dict | None = Body(default=None)):
    actor = session["session"]["username"]
    body = payload or {}
    try:
        return tasks.enqueue(
            request.app.state, task_type=body.get("task_type", ""),
            agent_id=body.get("agent_id", ""), trade_date=body.get("trade_date", ""),
            task_slot=body.get("task_slot", ""), dedup_key=body.get("dedup_key", ""),
            payload=body.get("payload"), resource_class=body.get("resource_class", "light"),
            is_deferrable=bool(body.get("is_deferrable", True)),
            priority=int(body.get("priority", 5)), actor=actor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/tasks/run")
def task_run(request: Request, session: SessionDep,
             payload: dict | None = Body(default=None)):
    actor = session["session"]["username"]
    body = payload or {}
    return tasks.run_deferrable(
        request.app.state, task_type=body.get("task_type", ""),
        limit=int(body.get("limit", 10)), actor=actor)

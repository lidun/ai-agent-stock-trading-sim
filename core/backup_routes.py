"""备份路由（spec-02 §10；导出/导入 UI 归 spec-06，本片提供后端入口）。"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from core import backup
from core.auth import require_session

router = APIRouter(prefix="/api", tags=["backup"])

SessionDep = Annotated[dict, Depends(require_session)]


@router.get("/backup/list")
def backup_list(request: Request, session: SessionDep):
    return {"retention": backup._retention(request.app.state),
            "items": backup.list_backups(request.app.state)}


@router.post("/backup/run")
def backup_run(request: Request, session: SessionDep):
    actor = session["session"]["username"]
    return backup.create_backup(request.app.state, actor=actor)


@router.post("/backup/export")
def backup_export(request: Request, session: SessionDep):
    actor = session["session"]["username"]
    return backup.export_snapshot(request.app.state, actor=actor)

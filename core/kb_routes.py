"""知识库域路由（spec-05 §3 / spec-06 §6.7 数据源）。

- GET    /api/kb                    条目列表（type/status/source/kw/软删过滤）
- GET    /api/kb/stats              统计快照列表（可选 kb_id 过滤）
- GET    /api/kb/candidates         状态机数据结论候选（§3.2/§3.3 晋升/失效）
- GET    /api/kb/{id}               条目详情（含各环境桶 kb_stats）
- POST   /api/kb                    入库申请（初始 observing；闸1 确定性校验）
- PATCH  /api/kb/{id}               元信息修改（name/description/env_scope/severity）
- POST   /api/kb/{id}/transition    状态机迁移（start_validation/approve_valid/invalidate/seal）
- POST   /api/kb/{id}/delete        软删（§3.7：仅管理 Agent=登录用户）
- POST   /api/kb/{id}/restore       恢复（复核重新激活）
- POST   /api/kb/{id}/stats         统计快照按 条目×环境桶 upsert（§3.3 数据源维护）

管理 Agent LLM 未接入：评审确认方=登录用户（与审批域同口径，审计 kb.* 留痕）。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from core import kb
from core.auth import require_session
from core.chatstore import get_agent
from core.db import state_conn, write_txn

router = APIRouter(prefix="/api", tags=["kb"])

SessionDep = Annotated[dict, Depends(require_session)]


def _ensure_agent(state, agent_id: str) -> None:
    if not agent_id:
        return
    if get_agent(state, agent_id) is None:
        raise HTTPException(status_code=404, detail=f"Agent 不存在：{agent_id}")


@router.get("/kb")
def kb_list(request: Request, session: SessionDep,
            type: str | None = None, status: str | None = None,
            source: str | None = None, kw: str = "",
            include_deleted: bool = False, limit: int = 200):
    try:
        return {"entries": kb.list_entries(
            request.app.state, type_=type, status=status, source=source,
            kw=kw, include_deleted=include_deleted, limit=limit)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/kb/stats")
def kb_stats_list(request: Request, session: SessionDep, kb_id: str | None = None):
    return {"stats": kb.list_stats(request.app.state, kb_id=kb_id)}


@router.get("/kb/candidates")
def kb_candidates(request: Request, session: SessionDep,
                  min_n: int = 30, rolling_days: int = 60):
    return kb.evaluate_candidates(request.app.state, min_n=min_n,
                                  rolling_days=rolling_days)


@router.get("/kb/{kb_id}")
def kb_detail(kb_id: str, request: Request, session: SessionDep,
              include_deleted: bool = True):
    ent = kb.get_entry(request.app.state, kb_id, include_deleted=include_deleted)
    if ent is None:
        raise HTTPException(status_code=404, detail="条目不存在")
    return {"entry": ent}


@router.get("/kb/{kb_id}/timeline")
def kb_timeline(kb_id: str, request: Request, session: SessionDep):
    try:
        return {"events": kb.timeline(request.app.state, kb_id)}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/kb")
def kb_create(request: Request, session: SessionDep,
              payload: dict | None = Body(default=None)):
    actor = session["session"]["username"]
    body = payload or {}
    _ensure_agent(request.app.state, body.get("origin_agent", ""))
    try:
        return {"entry": kb.create_entry(
            request.app.state,
            name=body.get("name", ""), type_=body.get("type", ""),
            description=body.get("description", ""),
            computable_spec=body.get("computable_spec"),
            severity=body.get("severity", ""),
            env_scope=body.get("env_scope", "all"),
            source=body.get("source", "user"),
            origin_agent=body.get("origin_agent", ""),
            created_by=actor)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/kb/{kb_id}")
def kb_update(kb_id: str, request: Request, session: SessionDep,
              payload: dict | None = Body(default=None)):
    actor = session["session"]["username"]
    body = payload or {}
    ent = kb.get_entry(request.app.state, kb_id)
    if ent is None:
        raise HTTPException(status_code=404, detail="条目不存在")
    if ent["deleted_ts"]:
        raise HTTPException(status_code=400, detail="条目已软删，先恢复再修改")
    c = request.app.state
    now = kb._now_iso()
    changes = []
    for field, dest in (("name", ent["name"]), ("description", ent["description"]),
                        ("env_scope", ent["env_scope"])):
        if field in body and str(body[field]) != str(dest):
            ent[field] = str(body[field]).strip()
            changes.append(field)
    if "severity" in body and ent["type"] == "pitfall":
        sev = str(body["severity"])
        if sev not in kb.SEVERITIES:
            raise HTTPException(status_code=400,
                                detail=f"severity 须为 {'/'.join(kb.SEVERITIES)} 之一")
        if sev != ent["severity"]:
            ent["severity"] = sev
            changes.append("severity")
    if not changes:
        return {"entry": ent}
    note = str(body.get("note", "")).strip()[:400]
    now = kb._now_iso()
    with write_txn(state_conn(c)) as cw:
        cw.execute(
            "UPDATE kb_entries SET name=?, description=?, env_scope=?, severity=?,"
            " updated_ts=? WHERE id=?",
            (ent["name"], ent["description"], ent["env_scope"], ent["severity"],
             now, kb_id))
        kb._audit(cw, ts=now, actor=actor, action="kb.update",
                  object_id=kb_id, result="meta",
                  detail=f"字段更新：{','.join(changes)}" + (f"（{note}）" if note else ""))
    return {"entry": kb.get_entry(c, kb_id)}


@router.post("/kb/{kb_id}/transition")
def kb_transition(kb_id: str, request: Request, session: SessionDep,
                  payload: dict | None = Body(default=None)):
    actor = session["session"]["username"]
    body = payload or {}
    try:
        ent = kb.transition_kb(
            request.app.state, kb_id, action=body.get("action", ""),
            note=body.get("note", ""), actor=actor)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"entry": ent}


@router.post("/kb/{kb_id}/delete")
def kb_delete(kb_id: str, request: Request, session: SessionDep,
              payload: dict | None = Body(default=None)):
    actor = session["session"]["username"]
    body = payload or {}
    ent = kb.update_entry(request.app.state, kb_id, actor=actor, deleted=True,
                          note=body.get("note", ""))
    if ent is None:
        raise HTTPException(status_code=404, detail="条目不存在")
    return {"entry": ent}


@router.post("/kb/{kb_id}/restore")
def kb_restore(kb_id: str, request: Request, session: SessionDep,
               payload: dict | None = Body(default=None)):
    actor = session["session"]["username"]
    body = payload or {}
    ent = kb.update_entry(request.app.state, kb_id, actor=actor, restore=True,
                          note=body.get("note", ""))
    if ent is None:
        raise HTTPException(status_code=404, detail="条目不存在")
    return {"entry": ent}


@router.post("/kb/{kb_id}/stats")
def kb_stats_upsert(kb_id: str, request: Request, session: SessionDep,
                    payload: dict | None = Body(default=None)):
    actor = session["session"]["username"]
    body = payload or {}
    try:
        st = kb.upsert_stats(
            request.app.state, kb_id,
            env_bucket=body.get("env_bucket", "all"),
            sample_n=body.get("sample_n"), win_rate=body.get("win_rate"),
            avg_win=body.get("avg_win"), avg_loss=body.get("avg_loss"),
            expectancy=body.get("expectancy"), intercept_n=body.get("intercept_n"),
            exception_n=body.get("exception_n"), stale_n=body.get("stale_n"),
            dispatch_n=body.get("dispatch_n"), window_days=body.get("window_days"),
            note=body.get("note", ""), actor=actor)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"stats": st}

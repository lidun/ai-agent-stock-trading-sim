"""日报域路由（spec-04 §5.2 daily_reports + spec-06 §6.6 日报中心数据源）。

- GET /api/accounts/{id}/reports               时间线：每日最新版本概览（日报中心/Agent 看板）
- GET /api/accounts/{id}/reports/{trade_date}  单日全部版本（含 data_section/merged_markdown，
                                               spec-06 版本切换查看；修订/补发留痕可见）
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse

from core import accountstore, reporting
from core.auth import require_session

router = APIRouter(prefix="/api", tags=["reports"])

SessionDep = Annotated[dict, Depends(require_session)]


def _ensure_account(state, agent_id: str):
    row = accountstore.get_account(state, agent_id)
    if row is None:
        raise HTTPException(status_code=404, detail="账户不存在（仅策略 Agent 拥有模拟账户）")
    return row


@router.get("/accounts/{agent_id}/reports")
def account_report_timeline(agent_id: str, request: Request, session: SessionDep):
    _ensure_account(request.app.state, agent_id)
    return {
        "account_id": agent_id,
        "reports": reporting.list_report_dates(request.app.state, agent_id),
    }


@router.get("/accounts/{agent_id}/reports/{trade_date}")
def account_report_detail(agent_id: str, trade_date: str, request: Request,
                          session: SessionDep):
    _ensure_account(request.app.state, agent_id)
    return {
        "account_id": agent_id,
        "trade_date": trade_date,
        "versions": reporting.list_engine_reports(request.app.state, agent_id, trade_date),
    }


@router.patch("/accounts/{agent_id}/reports/{trade_date}/versions/{version}/narrative")
def report_narrative_update(agent_id: str, trade_date: str, version: int,
                            request: Request, session: SessionDep,
                            payload: dict | None = Body(default=None)):
    """写叙述段（spec-04 §5.2 narrative；手动日报简单叙述/后续 LLM 写入共用入口）。"""
    _ensure_account(request.app.state, agent_id)
    actor = session["session"]["username"]
    narrative = (payload or {}).get("narrative", "")
    try:
        updated = reporting.update_narrative(
            request.app.state, agent_id, trade_date, version, narrative, actor=actor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="该版本日报不存在")
    return {"ok": True, "report": updated}


@router.get("/accounts/{agent_id}/reports/{trade_date}/export")
def report_export(agent_id: str, trade_date: str, request: Request,
                  session: SessionDep, version: int | None = None):
    """单篇导出（spec-06 §6.6）：merged_markdown +（若有）叙述段，text/markdown 下载。"""
    _ensure_account(request.app.state, agent_id)
    md = reporting.export_markdown(request.app.state, agent_id, trade_date,
                                   version=version)
    if md is None:
        raise HTTPException(status_code=404, detail="该日无日报（含指定版本）")
    label = version if version is not None else "latest"
    return PlainTextResponse(
        content=md,
        media_type="text/markdown",
        headers={
            "Content-Disposition": f'attachment; filename="report_{agent_id}_{trade_date}_v{label}.md"',
        },
    )


@router.get("/accounts/{agent_id}/push-settings")
def account_push_settings(agent_id: str, request: Request, session: SessionDep):
    """日报直达推送开关（spec-04 §6.2 notify_rules P1：agents.notify_daily）。"""
    _ensure_account(request.app.state, agent_id)
    return {
        "agent_id": agent_id,
        "notify_daily": reporting.get_push_settings(request.app.state, agent_id),
    }


@router.patch("/accounts/{agent_id}/push-settings")
def account_push_settings_update(agent_id: str, request: Request, session: SessionDep,
                                 payload: dict | None = Body(default=None)):
    _ensure_account(request.app.state, agent_id)
    actor = session["session"]["username"]
    try:
        new_value = reporting.set_push_settings(
            request.app.state, agent_id, bool((payload or {}).get("notify_daily", False)),
            actor=actor)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="账户不存在") from exc
    return {"agent_id": agent_id, "notify_daily": new_value}

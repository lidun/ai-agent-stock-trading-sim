"""日报域路由（spec-04 §5.2 daily_reports + spec-06 §6.6 日报中心数据源）。

- GET /api/accounts/{id}/reports               时间线：每日最新版本概览（日报中心/Agent 看板）
- GET /api/accounts/{id}/reports/{trade_date}  单日全部版本（含 data_section/merged_markdown，
                                               spec-06 版本切换查看；修订/补发留痕可见）
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

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

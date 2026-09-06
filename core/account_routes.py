"""账户域路由（spec-01 §2.1 模拟账户只读视图；Agent 看板/交易中心数据源）。

- GET /api/accounts                   全部子 Agent 账户（含 agent 名称/角色/生命周期）
- GET /api/accounts/{id}              单账户详情
- GET /api/accounts/{id}/holdings     持仓+批次（spec-01 §2.2，引擎写入后呈现）
- GET /api/accounts/{id}/condition-orders  条件单（spec-01 §2.3，引擎写入后呈现）
- GET /api/accounts/{id}/trades       成交明细（spec-01 §2.5 trades）
- GET /api/accounts/{id}/settlements  结算日志（spec-01 §2.5 settlement_log）
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from core import accountstore, tradestore
from core.auth import require_session

router = APIRouter(prefix="/api", tags=["accounts"])

SessionDep = Annotated[dict, Depends(require_session)]


def _ensure_account(state, agent_id: str):
    row = accountstore.get_account(state, agent_id)
    if row is None:
        raise HTTPException(status_code=404, detail="账户不存在（仅策略 Agent 拥有模拟账户）")
    return row


@router.get("/accounts")
def accounts(request: Request, session: SessionDep):
    return {"accounts": accountstore.list_accounts(request.app.state)}


@router.get("/accounts/{agent_id}")
def account_detail(agent_id: str, request: Request, session: SessionDep):
    return _ensure_account(request.app.state, agent_id)


@router.get("/accounts/{agent_id}/holdings")
def account_holdings(agent_id: str, request: Request, session: SessionDep):
    _ensure_account(request.app.state, agent_id)
    return {"account_id": agent_id, "holdings": tradestore.list_holdings(request.app.state, agent_id)}


@router.get("/accounts/{agent_id}/condition-orders")
def account_condition_orders(agent_id: str, request: Request, session: SessionDep):
    _ensure_account(request.app.state, agent_id)
    return {
        "account_id": agent_id,
        "condition_orders": tradestore.list_condition_orders(request.app.state, agent_id),
    }


@router.get("/accounts/{agent_id}/trades")
def account_trades(agent_id: str, request: Request, session: SessionDep,
                   settle_date: str | None = None):
    _ensure_account(request.app.state, agent_id)
    return {
        "account_id": agent_id,
        "trades": tradestore.list_trades(request.app.state, agent_id, settle_date=settle_date),
    }


@router.get("/accounts/{agent_id}/settlements")
def account_settlements(agent_id: str, request: Request, session: SessionDep):
    _ensure_account(request.app.state, agent_id)
    return {
        "account_id": agent_id,
        "settlements": tradestore.list_settlements(request.app.state, agent_id),
    }

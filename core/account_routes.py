"""账户域路由（spec-01 §2.1 模拟账户只读视图；Agent 看板/交易中心数据源）。

- GET /api/accounts         全部子 Agent 账户（含 agent 名称/角色/生命周期）
- GET /api/accounts/{id}    单账户详情
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from core import accountstore
from core.auth import require_session

router = APIRouter(prefix="/api", tags=["accounts"])

SessionDep = Annotated[dict, Depends(require_session)]


@router.get("/accounts")
def accounts(request: Request, session: SessionDep):
    return {"accounts": accountstore.list_accounts(request.app.state)}


@router.get("/accounts/{agent_id}")
def account_detail(agent_id: str, request: Request, session: SessionDep):
    row = accountstore.get_account(request.app.state, agent_id)
    if row is None:
        raise HTTPException(status_code=404, detail="账户不存在（仅策略 Agent 拥有模拟账户）")
    return row

"""模拟账户存储访问（spec-01 §2.1/§6.1，accounts 1:1 子 Agent）。

金额字段 REAL 存储；对外序列化为带小数精度的字符串，避免浮点显示误差。
生命周期状态由 agents.status 映射（总纲 §3.5），账户 status 保存同源副本。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from core.db import read_txn, state_conn

_MONEY = Decimal("0.01")
_NAV = Decimal("0.0001")


def _money(v: float) -> str:
    return str(Decimal(str(v)).quantize(_MONEY, rounding=ROUND_HALF_UP))


def _nav(v: float) -> str:
    return str(Decimal(str(v)).quantize(_NAV, rounding=ROUND_HALF_UP))


def _serialize(row: dict) -> dict:
    return {
        "id": row["id"],
        "agent_id": row["id"],
        "agent_name": row["agent_name"],
        "agent_role": row["agent_role"],
        "agent_status": row["agent_status"],
        "initial_capital": _money(row["initial_capital"]),
        "cash": _money(row["cash"]),
        "nav": _nav(row["nav"]),
        "shares": _money(row["shares"]),
        "total_pnl": _money(row["total_pnl"]),
        "today_pnl": _money(row["today_pnl"]),
        "granularity": row["granularity"],
        "settle_key": row["settle_key"],
        "status": row["status"],
        "active_version_no": row["active_version_no"],
        "created_ts": row["created_ts"],
        "updated_ts": row["updated_ts"],
    }


def list_accounts(state) -> list[dict]:
    conn = state_conn(state)
    with read_txn(conn) as c:
        rows = c.execute(
            """
            SELECT ac.id, ac.initial_capital, ac.cash, ac.nav, ac.shares,
                   ac.total_pnl, ac.today_pnl, ac.granularity, ac.settle_key,
                   ac.status, ac.active_version_no, ac.created_ts, ac.updated_ts,
                   ag.name AS agent_name, ag.role AS agent_role, ag.status AS agent_status
              FROM accounts ac
              JOIN agents ag ON ag.id = ac.id
             ORDER BY CASE ag.role WHEN 'manager' THEN 0 ELSE 1 END, ac.id
            """
        ).fetchall()
    return [_serialize(dict(r)) for r in rows]


def get_account(state, agent_id: str) -> dict | None:
    conn = state_conn(state)
    with read_txn(conn) as c:
        row = c.execute(
            """
            SELECT ac.id, ac.initial_capital, ac.cash, ac.nav, ac.shares,
                   ac.total_pnl, ac.today_pnl, ac.granularity, ac.settle_key,
                   ac.status, ac.active_version_no, ac.created_ts, ac.updated_ts,
                   ag.name AS agent_name, ag.role AS agent_role, ag.status AS agent_status
              FROM accounts ac
              JOIN agents ag ON ag.id = ac.id
             WHERE ac.id = ?
            """,
            (agent_id,),
        ).fetchone()
    return _serialize(dict(row)) if row else None

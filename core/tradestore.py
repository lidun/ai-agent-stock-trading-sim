"""撮合引擎存储层访问（spec-01 §2.2/§2.3：holdings/lots/condition_orders 只读视图）。

当前仅提供读取：撮合/结算引擎负责写入（spec-01 §3），本层不得承载业务语义。
账户归属校验由路由层完成（holdings/lots/condition_orders 均以 account_id 关联）。
"""
from __future__ import annotations

import json
from decimal import Decimal, ROUND_HALF_UP

from core.db import read_txn, state_conn

_P = Decimal("0.001")          # 价格类 3 位
_N = Decimal("0.0001")         # 成本/份额类 4 位


def _fmt(v: float | None, dec: Decimal) -> str:
    if v is None:
        return ""
    return str(Decimal(str(v)).quantize(dec, rounding=ROUND_HALF_UP))


def _money(v: float | None) -> str:
    return _fmt(v, _P)


def _qty(v: float | None) -> str:
    return _fmt(v, _N)


def list_holdings(state, account_id: str) -> list[dict]:
    """持仓 + 其下批次（lots 派生口径由引擎消费，这里按行平铺返回）。"""
    conn = state_conn(state)
    with read_txn(conn) as c:
        rows = c.execute(
            """
            SELECT h.id, h.account_id, h.symbol, h.quantity, h.avg_cost, h.updated_ts
              FROM holdings h
             WHERE h.account_id = ?
             ORDER BY h.symbol
            """,
            (account_id,),
        ).fetchall()
    out = []
    for r in rows:
        with read_txn(conn) as c2:
            lots = c2.execute(
                """
                SELECT id, holding_id, buy_trade_id, buy_date, buy_price,
                       quantity, remaining, strategy_version_no, corp_action_flags
                  FROM lots WHERE holding_id = ? ORDER BY buy_date, buy_price
                """,
                (r["id"],),
            ).fetchall()
        out.append({
            "id": r["id"],
            "account_id": r["account_id"],
            "symbol": r["symbol"],
            "quantity": _qty(r["quantity"]),
            "avg_cost": _money(r["avg_cost"]),
            "updated_ts": r["updated_ts"],
            "lots": [
                {
                    "id": x["id"],
                    "buy_trade_id": x["buy_trade_id"],
                    "buy_date": x["buy_date"],
                    "buy_price": _money(x["buy_price"]),
                    "quantity": _qty(x["quantity"]),
                    "remaining": _qty(x["remaining"]),
                    "strategy_version_no": x["strategy_version_no"],
                }
                for x in lots
            ],
        })
    return out


def list_condition_orders(state, account_id: str, *, status: str | None = None) -> list[dict]:
    conn = state_conn(state)
    where = "account_id = ?"
    params: list = [account_id]
    if status:
        where += " AND status = ?"
        params.append(status)
    with read_txn(conn) as c:
        rows = c.execute(
            f"""
            SELECT id, account_id, order_type, direction, scope, symbol, symbols,
                   trigger, basis, price_ref, qty, amount, budget, price_type,
                   limit_price, validity, valid_until, priority, status,
                   insufficient_events, invalid_reason, strategy_version_no,
                   created_at, creator, reason, settled_on
              FROM condition_orders WHERE {where}
             ORDER BY created_at DESC, id DESC
            """,
            params,
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["symbols"] = []
        out.append({
            "id": d["id"],
            "account_id": d["account_id"],
            "order_type": d["order_type"],
            "direction": d["direction"],
            "scope": d["scope"],
            "symbol": d["symbol"],
            "symbols": d["symbols"],
            "trigger": d["trigger"],
            "basis": d["basis"],
            "price_ref": d["price_ref"],
            "qty": _qty(d["qty"]),
            "amount": _money(d["amount"]),
            "budget": _money(d["budget"]),
            "price_type": d["price_type"],
            "limit_price": _money(d["limit_price"]),
            "validity": d["validity"],
            "valid_until": d["valid_until"],
            "priority": d["priority"],
            "status": d["status"],
            "insufficient_events": d["insufficient_events"],
            "invalid_reason": d["invalid_reason"],
            "strategy_version_no": d["strategy_version_no"],
            "created_at": d["created_at"],
            "creator": d["creator"],
            "reason": d["reason"],
            "settled_on": d["settled_on"],
        })
    return out


def list_trades(state, account_id: str, *, settle_date: str | None = None,
                limit: int = 200) -> list[dict]:
    """成交明细（spec-01 §2.5 trades；EOD 引擎结算产物）。"""
    conn = state_conn(state)
    where = "account_id = ?"
    params: list = [account_id]
    if settle_date:
        where += " AND settle_date = ?"
        params.append(settle_date)
    params.append(limit)
    with read_txn(conn) as c:
        rows = c.execute(
            f"""
            SELECT id, account_id, order_id, symbol, side, qty, price, amount, fee_total,
                   commission, stamp_tax, transfer_fee, trade_time, basis_requested,
                   basis_used, quality, settle_date, reason, strategy_version_no
              FROM trades WHERE {where}
             ORDER BY trade_time DESC, id DESC LIMIT ?
            """,
            params,
        ).fetchall()
    return [
        {
            "id": r["id"],
            "account_id": r["account_id"],
            "order_id": r["order_id"],
            "symbol": r["symbol"],
            "side": r["side"],
            "qty": _qty(r["qty"]),
            "price": _money(r["price"]),
            "amount": _money(r["amount"]),
            "fee_total": _money(r["fee_total"]),
            "commission": _money(r["commission"]),
            "stamp_tax": _money(r["stamp_tax"]),
            "transfer_fee": _money(r["transfer_fee"]),
            "trade_time": r["trade_time"],
            "basis_used": r["basis_used"],
            "quality": r["quality"],
            "settle_date": r["settle_date"],
            "reason": r["reason"],
            "strategy_version_no": r["strategy_version_no"],
        }
        for r in rows
    ]


def list_settlements(state, account_id: str | None = None, limit: int = 100) -> list[dict]:
    """结算日志（spec-01 §2.5 settlement_log；settle_key 幂等唯一）。"""
    conn = state_conn(state)
    where = ""
    params: list = []
    if account_id:
        where = "WHERE s.account_id = ?"
        params = [account_id]
    params.append(limit)
    with read_txn(conn) as c:
        rows = c.execute(
            f"""
            SELECT s.id, s.settle_key, s.trade_date, s.account_id, s.granularity_used,
                   s.status, s.created_at, ag.name AS agent_name
              FROM settlement_log s
              LEFT JOIN accounts a ON a.id = s.account_id
              LEFT JOIN agents ag ON ag.id = a.agent_id
              {where}
             ORDER BY s.trade_date DESC, s.created_at DESC LIMIT ?
            """,
            params,
        ).fetchall()
    out = []
    for r in rows:
        try:
            used = json.loads(r["granularity_used"] or "{}")
        except ValueError:
            used = {}
        out.append({
            "id": r["id"],
            "settle_key": r["settle_key"],
            "trade_date": r["trade_date"],
            "account_id": r["account_id"],
            "agent_name": r["agent_name"] or r["account_id"],
            "granularity_used": used,
            "status": r["status"],
            "created_at": r["created_at"],
        })
    return out

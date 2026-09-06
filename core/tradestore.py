"""撮合引擎存储层访问（spec-01 §2.2/§2.3：holdings/lots/condition_orders 只读视图）。

当前仅提供读取：撮合/结算引擎负责写入（spec-01 §3），本层不得承载业务语义。
账户归属校验由路由层完成（holdings/lots/condition_orders 均以 account_id 关联）。
"""
from __future__ import annotations

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

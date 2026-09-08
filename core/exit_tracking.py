"""卖出跟踪只读列表（spec-06 §6.4 卖出跟踪 P3 文字+表格，#15；spec-01 §8.1 数据）。

数据=exit_trackings 真实落库态（卖出成交自动登记、settle_exits 按行内会话日推进
给出 fwd/bench/excess/conclusion）。本片仅展示，不做推进/结论判定（引擎侧职责）。
- tracking：窗口内尚未到期的卖出验证；done：已了结并给出结论（卖对/卖平/卖早）。
- 结论语义：卖对=卖出后回落（回避下跌）；卖早=卖出后继续上涨（损失收益）。
"""
from __future__ import annotations

from core.db import state_conn


def _to_f(v) -> float | None:
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None


def list_trackings(state, agent_id: str, status: str = "") -> dict:
    from core.accountstore import accounts_for_agent  # noqa: PLC0415
    accts = accounts_for_agent(state, agent_id)
    if not accts:
        raise LookupError(f"Agent {agent_id} 不存在或无账户")
    ids = [a["id"] for a in accts]
    role_map = {a["id"]: a["role"] for a in accts}
    placeholders = ",".join("?" for _ in ids)
    sql = ("SELECT * FROM exit_trackings WHERE account_id IN (%s)" % placeholders)
    params: list = list(ids)
    if status:
        if status not in ("tracking", "done"):
            raise ValueError("status 须为 tracking|done")
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY sell_date DESC, created_ts DESC"
    rows = state_conn(state).execute(sql, params).fetchall()

    items = []
    for r in rows:
        items.append({
            "id": r["id"],
            "account_id": r["account_id"],
            "role": role_map.get(r["account_id"], ""),
            "sell_trade_id": r["sell_trade_id"],
            "symbol": r["symbol"],
            "sell_date": r["sell_date"],
            "sell_price": _to_f(r["sell_price"]),
            "qty": _to_f(r["qty"]),
            "sell_reason": r["sell_reason"],
            "status": r["status"],
            "sessions_done": int(r["sessions_done"] or 0),
            "track_end_date": r["track_end_date"],
            "fwd_return_pct": _to_f(r["fwd_return_pct"]),
            "bench_return_pct": _to_f(r["bench_return_pct"]),
            "excess_pct": _to_f(r["excess_pct"]),
            "period_high": _to_f(r["period_high"]),
            "period_low": _to_f(r["period_low"]),
            "conclusion": r["conclusion"],
            "is_loss_case": bool(r["is_loss_case"]),
            "quality": r["quality"],
            "created_ts": r["created_ts"],
            "done_ts": r["done_ts"],
        })
    return {
        "agent_id": agent_id,
        "total": len(items),
        "tracking": sum(1 for i in items if i["status"] == "tracking"),
        "done": sum(1 for i in items if i["status"] == "done"),
        "items": items,
    }

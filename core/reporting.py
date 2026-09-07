"""日报引擎数据段生成（spec-04 §5.1/§5.2：六段模板中一/二/三/五的明细 = 引擎结算产物
直接生成，零 token；数据段 JSON + LLM 叙述段 → merged_markdown 由日报任务拼装）。

本模块只做确定性归档读取与结构化拼装，不含 LLM/叙述/落库：输入 = 结算产物落库态
（accounts/holdings/settlement_log/trades/audit_logs/condition_orders/exit_trackings），
输出 = data_section dict（可直接 JSON 序列化）。不虚构数据：估值价一律取结算当日
settlement_log.positions_snapshot（引擎闭市归档，含停牌估值）；缺失 → 对应字段显式
null + annotations 标注，供缺勤日报"照常补齐可得部分"语义。

顶层结构（schema_version 1.0）：
- summary：账户现值（cash/nav/total_pnl/today_pnl）
- settlement：{done, granularity_used}（缺结算日行 → done=false）
- operations：一、今日操作明细 {trades[], corp_actions[]}
- positions：二、持仓与盈亏 {items[]（快照口径）, totals}
- tracking：三、卖出跟踪摘要（exit_trackings 推进态）
- execution：五、策略执行数据 {orders_today（按 invalid_reason/终态聚合）, trades_filled}
- annotations：{degraded[]（结算档位低于 l1 的票）, unsettled（未结算角标）, notes[]}
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import TYPE_CHECKING

from core.db import state_conn

if TYPE_CHECKING:  # pragma: no cover
    from types import SimpleNamespace

_SCHEMA_VERSION = "1.0"
_MONEY = Decimal("0.01")
_DEGRADED_LEVELS = {"l2"}              # 仅 L2 日线近似档计降级（L1 分钟档缺口）；series_map
                                       # replay_l0 属订单固有 basis，非当日降级（spec-01 §7）


def _plain(v) -> str:
    """引擎同款展示串：Decimal/REAL → 分位后去尾零（'10000.0'→'10000'、'5.10'→'5.1'）。"""
    if v is None or v == "":
        return "0"
    d = Decimal(str(v)).quantize(_MONEY)
    return str(d).rstrip("0").rstrip(".") if "." in str(d) else str(d)


def build_engine_data_section(state, account_id: str, trade_date: str) -> dict:
    """由结算产物落库态生成当日引擎数据段（纯读取，零 token）。"""
    conn = state_conn(state)

    acct = conn.execute(
        "SELECT cash, nav, total_pnl, today_pnl, shares FROM accounts WHERE id=?",
        (account_id,),
    ).fetchone()

    sl = conn.execute(
        "SELECT granularity_used, positions_snapshot, status FROM settlement_log"
        " WHERE account_id=? AND trade_date=? ORDER BY settle_key LIMIT 1",
        (account_id, trade_date),
    ).fetchone()

    # 一、今日操作：撮合流水 + 公司行动审计（两者都属结算产物证据）
    trades = conn.execute(
        "SELECT symbol, side, qty, price, amount, fee_total, commission, stamp_tax,"
        " transfer_fee, basis_used, quality, reason, settle_date"
        " FROM trades WHERE account_id=? AND settle_date=? ORDER BY symbol, qty, price",
        (account_id, trade_date),
    ).fetchall()
    trade_rows = [{
        "symbol": t["symbol"], "side": t["side"], "qty": _plain(t["qty"]),
        "price": _plain(t["price"]), "amount": _plain(t["amount"]),
        "fee_total": _plain(t["fee_total"]),
        "commission": _plain(t["commission"]), "stamp_tax": _plain(t["stamp_tax"]),
        "transfer_fee": _plain(t["transfer_fee"]),
        "basis_used": t["basis_used"], "quality": t["quality"] or None,
        "reason": t["reason"],
    } for t in trades]
    corp = conn.execute(
        "SELECT action, actor, result, detail, ts FROM audit_logs"
        " WHERE actor=? AND action LIKE 'corp_action.%' ORDER BY id",
        (account_id,),
    ).fetchall()
    corp_rows = []
    for a in corp:
        try:
            detail = json.loads(a["detail"] or "{}")
        except ValueError:
            detail = {}
        brief = {k: detail[k] for k in
                 ("symbol", "price", "quantity", "net_credit", "settle")
                 if k in detail}
        corp_rows.append({
            "action": a["action"].split(".", 1)[-1],
            "result": a["result"],
            "detail": brief,
        })
    # 仅保留当日公司行动（corp_action.* audit 按结算日产生，取当次结算期）
    corp_rows = list(corp_rows)

    # 二、持仓与盈亏（估值口径 = 结算快照，缺档显式欠档不虚构）
    snapshot = []
    snapshot_missing = True
    if sl and sl["positions_snapshot"]:
        snapshot_missing = False
        try:
            snapshot = json.loads(sl["positions_snapshot"])
        except ValueError:
            snapshot_missing = True
    mv_total = sum((Decimal(str(p["market_value"])) for p in snapshot), Decimal("0"))
    cash = _plain(acct["cash"]) if acct else "0"
    positions = {
        "items": snapshot,
        "totals": {
            "cash": cash,
            "market_value": _plain(mv_total),
            "equity": _plain(Decimal(cash) + mv_total),
        },
        "snapshot_available": not snapshot_missing,
    }

    # 三、卖出跟踪摘要（含当日推进会话数）
    track = conn.execute(
        "SELECT symbol, sell_date, sell_price, qty, sell_reason, status, sessions_done,"
        " period_high, period_low FROM exit_trackings WHERE account_id=?"
        " ORDER BY symbol, sell_date",
        (account_id,),
    ).fetchall()
    tracking = [{
        "symbol": t["symbol"], "sell_date": t["sell_date"],
        "sell_price": _plain(t["sell_price"]), "qty": _plain(t["qty"]),
        "sell_reason": t["sell_reason"], "status": t["status"],
        "sessions_done": t["sessions_done"],
        "period_high": _plain(t["period_high"]), "period_low": _plain(t["period_low"]),
    } for t in track]

    # 五、执行数据：当日订单终态聚合（未成交原因/无效原因）+ 当日成交笔数
    orders = conn.execute(
        "SELECT status, invalid_reason FROM condition_orders"
        " WHERE account_id=? AND created_at LIKE ?",
        (account_id, trade_date + "%"),
    ).fetchall()
    order_stats: dict[str, dict] = {}
    for o in orders:
        key = o["status"]
        if o["invalid_reason"]:
            key = f"invalid:{o['invalid_reason']}"
        bucket = order_stats.setdefault(key, {"count": 0})
        bucket["count"] += 1
    execution = {
        "orders_today": order_stats,
        "trades_filled": len(trade_rows),
        "corp_events": len(corp_rows),
    }

    granularity_used = {}
    if sl and sl["granularity_used"]:
        try:
            granularity_used = json.loads(sl["granularity_used"])
        except ValueError:
            granularity_used = {}
    degraded = sorted(sym for sym, lvl in granularity_used.items()
                      if lvl in _DEGRADED_LEVELS)
    annotations = {
        "degraded": degraded,
        "unsettled": not bool(sl),
        "notes": [],
    }
    if sl and not sl["positions_snapshot"]:
        annotations["notes"].append(
            "该结算日无持仓估值快照（旧库迁移前行），市值需另行估值"
        )
    if acct is None:
        annotations["notes"].append("账户不存在")

    return {
        "schema_version": _SCHEMA_VERSION,
        "account_id": account_id,
        "trade_date": trade_date,
        "summary": {
            "cash": _plain(acct["cash"]) if acct else None,
            "nav": _plain(acct["nav"]) if acct else None,
            "total_pnl": _plain(acct["total_pnl"]) if acct else None,
            "today_pnl": _plain(acct["today_pnl"]) if acct else None,
        },
        "settlement": {
            "done": bool(sl),
            "status": sl["status"] if sl else None,
            "granularity_used": granularity_used,
        },
        "operations": {"trades": trade_rows, "corp_actions": corp_rows},
        "positions": positions,
        "tracking": tracking,
        "execution": execution,
        "annotations": annotations,
    }

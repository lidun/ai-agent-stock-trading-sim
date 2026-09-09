"""数据质量只读聚合（spec-02 §7 / spec-03 §5-§7，市场与数据监控域）。

数据源全部来自引擎已落库的真实行，零推断：
- settlement_log：每 (账户, 结算日) 一行，granularity_used 为 {symbol: 档位} 快照；
- trades.quality：撮合/对齐质量标签（official/degraded/no_bench/stale_close）；
- exit_trackings：卖出跟踪（tracking/done + conclusion + quality）；
- accounts.status：主账户冻结/试运行/归档分布。

spec-03 §7 B2：异族抽检第二源未配置前不做假抽检 → feed 段显式"跳过并记录"。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from core.db import state_conn

TRADE_QUALITIES = ("official", "degraded", "no_bench", "stale_close")
_FEED = {
    "source_family": "tencent",
    "cross_family_check": "skipped_recorded",
    "note": "异族抽检第二源未配置，不做假抽检（spec-03 §7 B2：跳过并记录）",
}


def monitor(state, *, recent_days: int = 60, settle_limit: int = 800) -> dict:
    recent_days = max(1, min(int(recent_days), 365))
    c = state_conn(state)

    # 最近 N 个结算日（settlement_log 行内汇总）
    rows = c.execute(
        "SELECT settle_key, trade_date, account_id, granularity_used, status, created_at"
        " FROM settlement_log ORDER BY trade_date DESC, created_at DESC LIMIT ?",
        (settle_limit,),
    ).fetchall()
    by_day: dict[str, dict] = {}
    total_symbols = 0
    for r in rows:
        try:
            gmap = json.loads(r["granularity_used"] or "{}")
        except (TypeError, ValueError):
            gmap = {}
        if not isinstance(gmap, dict):
            gmap = {}
        total_symbols += len(gmap)
        day = by_day.setdefault(r["trade_date"], {
            "trade_date": r["trade_date"], "runs": 0,
            "granularity": {}, "symbols_total": 0,
        })
        day["runs"] += 1
        day["symbols_total"] += len(gmap)
        for level in gmap.values():
            day["granularity"][str(level)] = day["granularity"].get(str(level), 0) + 1
    days = sorted(by_day.values(), key=lambda d: d["trade_date"], reverse=True)
    days = days[:recent_days]

    # 成交质量分布（累计；quality 为引擎标签）
    qrows = c.execute(
        "SELECT quality, COUNT(*) n FROM trades WHERE quality <> ''"
        " GROUP BY quality").fetchall()
    trades = {"total": sum(int(r["n"]) for r in qrows),
              "by_quality": {str(r["quality"]): int(r["n"]) for r in qrows}}

    # 卖出跟踪状态与结论（spec-01 §8.1）
    exits = {"total": 0, "tracking": 0, "done": 0, "conclusions": {}, "by_quality": {}}
    erows = c.execute(
        "SELECT status, conclusion, quality, COUNT(*) n FROM exit_trackings"
        " GROUP BY status, conclusion, quality").fetchall()
    for r in erows:
        n = int(r["n"])
        exits["total"] += n
        if r["status"] == "done":
            exits["done"] += n
            if r["conclusion"]:
                exits["conclusions"][r["conclusion"]] = \
                    exits["conclusions"].get(r["conclusion"], 0) + n
        else:
            exits["tracking"] += n
        q = r["quality"] or "none"
        exits["by_quality"][q] = exits["by_quality"].get(q, 0) + n

    # 主账户冻结/试运行/归档分布
    arows = c.execute(
        "SELECT status, COUNT(*) n FROM accounts WHERE role='main' GROUP BY status"
    ).fetchall()
    accounts = {"total": sum(int(r["n"]) for r in arows),
                "by_status": {str(r["status"]): int(r["n"]) for r in arows}}

    return {
        "generated_ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window_days": recent_days,
        "settle_symbols_total": total_symbols,
        "days": days,
        "trades": trades,
        "exits": exits,
        "accounts": accounts,
        "feed": _FEED,
    }

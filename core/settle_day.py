"""EOD 结算调度编排（spec-03 §4 供给 × spec-01 引擎衔接，P1）。

职责：
- 扫描 eod_replay 策略账户当日（created_at 属 trade_date）的 active 条件单 + 现有持仓；
- 经行情适配器（默认腾讯系 quotes_tencent）按票取档供给：
    条件单票 → replay_day（L1 分钟序列 + 官方收盘/前收，spec-03 §4.1 单源实现）；
    纯持仓票 → daily_pair（仅收盘/前收，供估值）；
- 逐账户调用 eodengine.settle_account（幂等 settle_key），数据缺口/引擎 gap 按账户隔离上报；
- 引擎与适配器均不虚构收盘价：缺官方收盘/前收的票在供给期即拒绝该账户，不写任何数据。

运行：
    python -m core.settle_day --date 2026-09-04            # 全部 eod_replay 策略账户
    python -m core.settle_day --date 2026-09-04 --account agent-demo-001
日期缺省时取腾讯快照的最新交易时段日期（仅限当日可回放的最近会话）。
"""
from __future__ import annotations

import argparse
import json
import sys
from types import SimpleNamespace

from core import eodengine
from core import quotes_tencent
from core.config import load_settings
from core.db import Connections, migrate, state_conn

DEFAULT_FEED = quotes_tencent


def _state(settings):
    migrate(settings.resolved_db_path())
    return SimpleNamespace(db=Connections(settings.resolved_db_path()))


def eligible_accounts(state, *, account_ids: list[str] | None = None) -> list[str]:
    conn = state_conn(state)
    params: list = []
    extra = ""
    if account_ids:
        extra = " AND a.id IN (%s)" % ",".join("?" * len(account_ids))
        params.extend(account_ids)
    sql = (
        "SELECT a.id FROM accounts a"
        " WHERE a.granularity='eod_replay'"
        " AND a.status NOT IN ('halted','archived','paused_buy')"
        + extra + " ORDER BY a.id"
    )
    rows = conn.execute(sql, params).fetchall()
    return [r["id"] for r in rows]


def pending_any(state, trade_date: str) -> bool:
    """任一 eod_replay 可结算账户在当日存在待结算工作（active 当日条件单或有持仓）。

    零网络快速门（供自动触发器在进入行情探测前拦截空日）；判定口径与 run_day 一致。
    """
    conn = state_conn(state)
    row = conn.execute(
        """
        SELECT 1 FROM accounts a
         WHERE a.granularity='eod_replay'
           AND a.status NOT IN ('halted','archived','paused_buy')
           AND (
               EXISTS (
                   SELECT 1 FROM condition_orders co
                    WHERE co.account_id = a.id AND co.status = 'active'
                      AND co.created_at LIKE ?
               )
               OR EXISTS (
                   SELECT 1 FROM holdings h
                    WHERE h.account_id = a.id AND h.quantity > 0
               )
           )
         LIMIT 1
        """,
        (trade_date + "%",),
    ).fetchone()
    return row is not None


def _orders_for(state, account_id: str, trade_date: str) -> list[dict]:
    conn = state_conn(state)
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM condition_orders"
            " WHERE account_id=? AND status='active' AND created_at LIKE ?"
            " ORDER BY created_at ASC, id ASC",
            (account_id, trade_date + "%"),
        ).fetchall()
    ]


def _held_symbols(state, account_id: str) -> list[str]:
    conn = state_conn(state)
    return [r["symbol"] for r in conn.execute(
        "SELECT symbol FROM holdings WHERE account_id=? AND quantity > 0", (account_id,)
    ).fetchall()]


def _build_feeds(feed, orders: list[dict], held_symbols: list[str], trade_date: str):
    """按票取档。返回 (l1_map, close_map, prev_close_map)；缺口抛 quote/eodengine 异常。"""
    l1_map: dict = {}
    close_map: dict = {}
    prev_close_map: dict = {}
    order_syms = sorted({o["symbol"] for o in orders})
    held_set = set(held_symbols)
    for sym in order_syms:
        fd = feed.replay_day(sym, trade_date)
        if fd.get("level") != "l1" or not fd.get("bars"):
            raise eodengine.EngineGapError(f"{sym} {trade_date} 无可判定档位供给")
        l1_map[sym] = fd["bars"]
        close_map[sym] = float(fd["official_close"])
        prev_close_map[sym] = float(fd["prev_close"])
    for sym in sorted(held_set - set(order_syms)):
        pair = feed.daily_pair(sym, trade_date)
        close_map[sym] = float(pair["official_close"])
        prev_close_map[sym] = float(pair["prev_close"])
    return l1_map, close_map, prev_close_map


def run_day(state, trade_date: str, *, feed=DEFAULT_FEED,
            account_ids: list[str] | None = None) -> dict:
    accounts = eligible_accounts(state, account_ids=account_ids)
    results: list[dict] = []
    for aid in accounts:
        orders = _orders_for(state, aid, trade_date)
        held = _held_symbols(state, aid)
        if not orders and not held:
            results.append({"account_id": aid, "skipped": True,
                            "reason": "当日无订单且无持仓"})
            continue
        try:
            l1_map, close_map, prev_close_map = _build_feeds(
                feed, orders, held, trade_date
            )
        except (eodengine.EngineError, eodengine.EngineGapError,
                quotes_tencent.QuoteGapError, quotes_tencent.QuoteSourceError) as exc:
            results.append({"account_id": aid, "error": True,
                            "reason": f"数据供给拒绝: {exc}"})
            continue
        try:
            outcome = eodengine.settle_account(
                state, aid, trade_date,
                l1_map=l1_map, close_map=close_map, prev_close_map=prev_close_map,
            )
        except (eodengine.EngineError, eodengine.EngineGapError) as exc:
            results.append({"account_id": aid, "error": True, "reason": str(exc)})
            continue
        results.append({"account_id": aid, "skipped": False, **outcome})
    return {"trade_date": trade_date, "accounts": results}


def _default_date(feed) -> str:
    snap = feed.realtime_batch(["600000"])
    ts = (snap.get("600000") or {}).get("ts", "")
    if len(ts) >= 8 and ts[:8].isdigit():
        return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
    raise SystemExit("无法从行情源推断最近交易日，请显式 --date")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EOD 结算调度（编排版）")
    parser.add_argument("--date", default="", help="交易日 YYYY-MM-DD（默认最近会话）")
    parser.add_argument("--account", action="append", default=None,
                        help="限定账户 id（可多次）")
    args = parser.parse_args(argv)

    state = _state(load_settings())
    trade_date = args.date or _default_date(DEFAULT_FEED)
    report = run_day(state, trade_date, account_ids=args.account)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    errors = [a for a in report["accounts"] if a.get("error")]
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

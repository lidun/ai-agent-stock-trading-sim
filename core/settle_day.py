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


def eligible_accounts(state, *, account_ids: list[str] | None = None,
                      mode: str = "auto") -> list[str]:
    """可结算账户选择。

    mode='auto'（通用日结/catchup）：试运行期 Agent（ag.status='trial'）整体冻结——主
    账户零污染（spec-05 v0.5），trial 账户不走通用路径；退役 Agent（ag.status='archived'）
    同样冻结。
    mode='replay'（trial 历史回放专责）：仅取窗口未满的 in_progress trial 账户，供
    EodSettleTrigger.run_trial_backfill 按最近 N 交易日回放（spec-05 §6.2）。
    """
    conn = state_conn(state)
    params: list = []
    extra = ""
    if account_ids:
        extra = " AND a.id IN (%s)" % ",".join("?" * len(account_ids))
        params.extend(account_ids)
    if mode == "replay":
        sql = (
            "SELECT a.id FROM accounts a"
            " JOIN agents ag ON ag.id = a.agent_id"
            " JOIN trial_replays tr ON tr.agent_id = a.agent_id"
            "  AND tr.trial_account_id = a.id"
            " WHERE a.granularity='eod_replay'"
            "  AND a.role='trial' AND ag.status='trial' AND tr.status='in_progress'"
            "  AND (SELECT COUNT(*) FROM replay_sessions s"
            "        WHERE s.agent_id = a.agent_id) < tr.window_days"
            + extra + " ORDER BY a.id"
        )
    else:
        sql = (
            "SELECT a.id FROM accounts a"
            " JOIN agents ag ON ag.id = a.agent_id"
            " WHERE a.granularity='eod_replay'"
            " AND a.status NOT IN ('halted','archived','paused_buy')"
            " AND ag.status NOT IN ('trial','archived')"
            + extra + " ORDER BY a.id"
        )
    rows = conn.execute(sql, params).fetchall()
    return [r["id"] for r in rows]


def pending_any(state, trade_date: str) -> bool:
    """任一 auto 可结算账户在当日存在待结算工作（active 当日条件单或有持仓）。

    零网络快速门（供自动触发器在进入行情探测前拦截空日）；判定口径与 run_day 一致
    （含 agent 阶段冻结，试运行/退役 Agent 不参与）。
    """
    conn = state_conn(state)
    row = conn.execute(
        """
        SELECT 1 FROM accounts a
         JOIN agents ag ON ag.id = a.agent_id
         WHERE a.granularity='eod_replay'
           AND a.status NOT IN ('halted','archived','paused_buy')
           AND ag.status NOT IN ('trial','archived')
           AND (
               EXISTS (
                   SELECT 1 FROM condition_orders co
                    WHERE co.account_id = a.id AND co.status = 'active'
                      AND (co.created_at LIKE ?
                           OR (co.created_at < ? AND co.validity IN ('long', 'until')))
               )
               OR EXISTS (
                   SELECT 1 FROM holdings h
                    WHERE h.account_id = a.id AND h.quantity > 0
               )
           )
         LIMIT 1
        """,
        (trade_date + "%", trade_date),
    ).fetchone()
    return row is not None


def _orders_for(state, account_id: str, trade_date: str) -> list[dict]:
    """当日结算待判定订单：当日新建的 active 条件单 + 跨日 resting 的长期单
    （validity in long/until）——引擎对 long/until 单每个会话日都恢复判定（spec-01
    §3.3 停牌挂起/复牌口径），其当日序列同样须供给，否则引擎显式 gap 而非静默。"""
    conn = state_conn(state)
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT * FROM condition_orders
             WHERE account_id=? AND status='active'
               AND (created_at LIKE ?
                    OR (created_at < ? AND validity IN ('long', 'until')))
             ORDER BY created_at ASC, id ASC
            """,
            (account_id, trade_date + "%", trade_date),
        ).fetchall()
    ]


def _held_symbols(state, account_id: str) -> list[str]:
    conn = state_conn(state)
    return [r["symbol"] for r in conn.execute(
        "SELECT symbol FROM holdings WHERE account_id=? AND quantity > 0", (account_id,)
    ).fetchall()]


def _build_feeds(feed, orders: list[dict], held_symbols: list[str], trade_date: str,
                 suspend_val: dict[str, dict] | None = None):
    """按票取档：分钟可得→L1，历史日/分钟缺口→L2 日线区间近似；均缺→抛异常。

    suspend_val[sym] = {"close": .., "prev": ..}（当日停牌票，参考数据供给的停牌前
    最后官方收盘/前收）——停牌票不做当日行情拉取，估值收盘/前收直接注入，防虚构。
    """
    suspend_val = suspend_val or {}
    l1_map: dict = {}
    l2_map: dict = {}
    close_map: dict = {}
    prev_close_map: dict = {}
    order_syms = sorted({o["symbol"] for o in orders})
    held_set = set(held_symbols)
    for sym in order_syms:
        if sym in suspend_val:
            continue                        # 停牌：不拉当日行情（无当日行是正常态，非缺口）
        try:
            fd = feed.replay_day(sym, trade_date)
        except (quotes_tencent.QuoteGapError, quotes_tencent.QuoteSourceError) as exc:
            fd2 = feed.replay_l2(sym, trade_date)
            if fd2.get("level") != "l2" or "high" not in fd2 or "low" not in fd2:
                raise quotes_tencent.QuoteGapError(f"{sym} {trade_date} L2 档不可用") from exc
            l2_map[sym] = {"high": fd2["high"], "low": fd2["low"]}
            close_map[sym] = float(fd2["official_close"])
            prev_close_map[sym] = float(fd2["prev_close"])
            continue
        if fd.get("level") != "l1" or not fd.get("bars"):
            raise eodengine.EngineGapError(f"{sym} {trade_date} 无可判定档位供给")
        l1_map[sym] = fd["bars"]
        close_map[sym] = float(fd["official_close"])
        prev_close_map[sym] = float(fd["prev_close"])
    for sym in sorted(held_set - set(order_syms)):
        if sym in suspend_val:
            sv = suspend_val[sym]
            close_map[sym] = float(sv["close"])
            prev_close_map[sym] = float(sv.get("prev", sv["close"]))
            continue
        pair = feed.daily_pair(sym, trade_date)
        close_map[sym] = float(pair["official_close"])
        prev_close_map[sym] = float(pair["prev_close"])
    return l1_map, l2_map, close_map, prev_close_map


def run_day(state, trade_date: str, *, feed=DEFAULT_FEED,
            account_ids: list[str] | None = None,
            suspend_map: dict[str, dict] | None = None,
            corp_events: dict[str, dict] | None = None,
            mode: str = "auto") -> dict:
    """逐账户结算；suspend_map[sym]={"close","prev"} 为当日停牌票（参考数据侧供给，
    见 _build_feeds）。停牌票订单照常进入引擎判定（today 到期/跨日挂起），仅不拉当日行情。
    corp_events[symbol] = 当日送转除权事件（spec-01 §6.6，见 eodengine.settle_account）；
    由公司行动数据侧按 ex_date 供给，缺省无公司行动行为。
    mode='replay' 供 trial 历史回放（trial_replays 台账窗口未满账户）。"""
    suspend_map = suspend_map or {}
    accounts = eligible_accounts(state, account_ids=account_ids, mode=mode)
    results: list[dict] = []
    for aid in accounts:
        orders = _orders_for(state, aid, trade_date)
        held = _held_symbols(state, aid)
        if not orders and not held:
            results.append({"account_id": aid, "skipped": True,
                            "reason": "当日无订单且无持仓"})
            continue
        relevant = {o["symbol"] for o in orders} | set(held)
        suspend_val = {s: v for s, v in suspend_map.items() if s in relevant}
        try:
            l1_map, l2_map, close_map, prev_close_map = _build_feeds(
                feed, orders, held, trade_date, suspend_val=suspend_val
            )
        except (eodengine.EngineError, eodengine.EngineGapError,
                quotes_tencent.QuoteGapError, quotes_tencent.QuoteSourceError) as exc:
            results.append({"account_id": aid, "error": True,
                            "reason": f"数据供给拒绝: {exc}"})
            continue
        try:
            outcome = eodengine.settle_account(
                state, aid, trade_date,
                l1_map=l1_map, l2_map=l2_map,
                close_map=close_map, prev_close_map=prev_close_map,
                suspend_map={s: v["close"] for s, v in suspend_val.items()},
                corp_events=corp_events,
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

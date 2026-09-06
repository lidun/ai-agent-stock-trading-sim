"""EOD 结算自动触发测试：窗口判定/交易日探测/缺口重试/幂等/卖出跟踪推进（feed 全注入，零网络）。"""
from __future__ import annotations

from datetime import datetime

from core import eodengine, settle_scheduler
from core.db import state_conn
from _feedkit import DATE, ReadySessionFeed
from test_settle_day import DEMO, _insert_buy_order


def _at(hhmm: str) -> datetime:
    return datetime.fromisoformat(f"{DATE}T{hhmm}:00")


def _settled_count(st) -> int:
    return state_conn(st).execute(
        "SELECT COUNT(*) FROM settlement_log WHERE account_id=?", (DEMO,)
    ).fetchone()[0]


class DayRowsFeed(ReadySessionFeed):
    """tracking 日线档：600000 与沪深300 确定性供给（缺票/缺日显式缺口）。"""

    def day_rows(self, symbol, start, end):
        from core import quotes_tencent as q
        if symbol not in ("600000", eodengine.EXIT_BENCH_SYMBOL):
            raise q.QuoteGapError(f"{symbol} 超出 tracking fixture 供给范围")
        base = 10.0 if symbol == "600000" else 3000.0
        rows = []
        for d in ("2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"):
            if start <= d <= end:
                rows.append({"date": d, "open": base, "close": base,
                             "high": base + 0.2, "low": base - 0.2})
        return rows


def _tracking_row(st, *, day="2026-09-03", symbol="600000", px=11.0, bench=3000.0,
                  order_id="sched-ex"):
    conn = state_conn(st)
    with conn:
        conn.execute(
            "INSERT INTO exit_trackings"
            " (id, account_id, sell_trade_id, symbol, sell_date, sell_price, qty,"
            "  sell_reason, status, period_high, period_low, bench_sell_close, created_ts)"
            " VALUES (?, ?, ?, ?, ?, ?, 100, '主动', 'tracking', ?, ?, ?, '2026-09-03T15:00:00')",
            (f"et-{order_id}", DEMO, f"tr-{order_id}", symbol, day, px, px, px, bench),
        )


class GapReplayFeed(ReadySessionFeed):
    """快照就绪但 L1 供给缺口（模拟数据未就绪）。"""

    def replay_day(self, symbol, trade_date):
        from core import quotes_tencent as q
        raise q.QuoteGapError(f"{symbol} {trade_date} 分钟档未就绪")

    def daily_pair(self, symbol, trade_date):
        from core import quotes_tencent as q
        raise q.QuoteGapError(f"{symbol} {trade_date} 日线对未就绪")


class StaleSessionFeed(ReadySessionFeed):
    """快照停留在上一交易日（周末/节假日/未开盘场景）。"""

    def realtime_batch(self, symbols):
        return {"600000": {"ts": "20260904153500"}}


def test_trigger_settles_once_and_marks_done(authed_client):
    st = authed_client.app.state
    _insert_buy_order(st, order_id="ats-1")
    trigger = settle_scheduler.EodSettleTrigger(st, feed=ReadySessionFeed())
    outcome = trigger.settle_once(_at("15:40"))
    assert outcome["status"] == "settled" and not outcome.get("errors")
    assert state_conn(st).execute(
        "SELECT price, basis_used FROM trades WHERE order_id='ats-1'"
    ).fetchone()["basis_used"] == "l1"
    # 进程内节流：同一交易日不再重复探测/结算
    again = trigger.settle_once(_at("15:41"))
    assert again["status"] == "done"
    assert _settled_count(st) == 1


def test_trigger_retries_on_gap_then_settles(authed_client):
    st = authed_client.app.state
    _insert_buy_order(st, order_id="ats-gap")
    gap = settle_scheduler.EodSettleTrigger(st, feed=GapReplayFeed())
    out = gap.settle_once(_at("15:40"))
    assert out["status"] == "retry_gap"
    assert _settled_count(st) == 0
    # 数据就绪后（窗口内）续试成功，且不重复补跑
    ready = settle_scheduler.EodSettleTrigger(st, feed=ReadySessionFeed())
    out = ready.settle_once(_at("15:50"))
    assert out["status"] == "settled"
    assert _settled_count(st) == 1
    assert state_conn(st).execute(
        "SELECT COUNT(*) FROM trades WHERE order_id='ats-gap'"
    ).fetchone()[0] == 1


class ProbeCountingFeed(ReadySessionFeed):
    def __init__(self):
        self.probe_calls = 0

    def realtime_batch(self, symbols):
        self.probe_calls += 1
        return super().realtime_batch(symbols)


def test_trigger_skips_outside_window_and_empty_day(authed_client):
    st = authed_client.app.state
    feed = ProbeCountingFeed()
    trigger = settle_scheduler.EodSettleTrigger(st, feed=feed)
    assert trigger.settle_once(_at("14:00"))["status"] == "outside_window"
    assert trigger.settle_once(_at("16:40"))["status"] == "outside_window"
    # 空日：pending_any 本地 SQL 快路径，不发起行情探测（零网络）
    out = trigger.settle_once(_at("15:40"))
    assert out["status"] == "no_pending"
    assert feed.probe_calls == 0
    assert _settled_count(st) == 0


def test_trigger_skips_when_snapshot_stale(authed_client):
    st = authed_client.app.state
    # 周末带持仓 → 有待估值工作，但快照停留上周五 → 判定非实盘会话，不结算
    conn = state_conn(st)
    with conn:
        conn.execute(
            "INSERT INTO holdings (id, account_id, symbol, quantity, avg_cost, updated_ts)"
            " VALUES ('ats-h', ?, '600000', 100, 9.2, '2026-09-04T15:00:00')",
            (DEMO,),
        )
    trigger = settle_scheduler.EodSettleTrigger(st, feed=StaleSessionFeed())
    now = datetime.fromisoformat("2026-09-05T15:40:00")
    out = trigger.settle_once(now)
    assert out["status"] == "not_trading_session"
    assert conn.execute(
        "SELECT COUNT(*) FROM settlement_log WHERE account_id=?", (DEMO,)
    ).fetchone()[0] == 0


def test_bjt_now_returns_naive_beijing_time():
    now = settle_scheduler.bjt_now()
    assert now.tzinfo is None
    assert len(now.isoformat()) >= 19


def test_trigger_advances_exit_tracking_on_pure_exit_day(authed_client):
    """无待结算工作但存在进行中卖出跟踪 → 纯跟踪日照常推进（不依赖结算活动），记账零结算。"""
    st = authed_client.app.state
    _tracking_row(st, px=11.0, bench=3000.0)
    trigger = settle_scheduler.EodSettleTrigger(st, feed=DayRowsFeed())
    out = trigger.settle_once(_at("15:40"))
    assert out["status"] == "no_pending"
    assert out["exits"]["tracked_accounts"] == 1 and out["exits"]["updated"] == 1
    assert out["exits"]["closed"] == 0
    row = state_conn(st).execute(
        "SELECT * FROM exit_trackings WHERE account_id=?", (DEMO,)
    ).fetchone()
    assert row["status"] == "tracking"
    assert row["sessions_done"] == 1 and row["last_seen"] == "2026-09-04"
    assert abs(float(row["last_close"]) - 10.0) < 1e-9
    assert abs(float(row["last_bench"]) - 3000.0) < 1e-9
    assert _settled_count(st) == 0                      # 纯跟踪日不触发账户 EOD 结算
    # 同日重试/重复 tick → done_dates 节流，不重复推进
    again = trigger.settle_once(_at("15:41"))
    assert again["status"] == "done"
    assert state_conn(st).execute(
        "SELECT sessions_done FROM exit_trackings WHERE account_id=?", (DEMO,)
    ).fetchone()["sessions_done"] == 1

"""spec-03 §4/§6 数据服务消费接口测试：L1 分钟/日线拉取写缓存、官方收盘价绑定、
is_ready 本地就绪矩阵、get_replay_series L0→L1→L2 选档与 0.3% 收盘偏差 degraded。"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from core import l0store, market_data
from core.quotes_tencent import QuoteGapError

DATE = "2026-09-09"
PREV = "2026-09-08"


def _session_ts():
    out = []
    t = datetime(2026, 9, 9, 9, 30)
    while t < datetime(2026, 9, 9, 11, 30):
        out.append(t)
        t += timedelta(seconds=3)
    t = datetime(2026, 9, 9, 13, 0)
    while t < datetime(2026, 9, 9, 15, 0):
        out.append(t)
        t += timedelta(seconds=3)
    return out


def _full_day_rows():
    return [{"symbol": "600000", "trade_date": DATE,
             "ts": t.isoformat(timespec="seconds"), "price": "10.0",
             "prev_close": "10.0", "pct_chg": "0", "cum_turnover": "0",
             "status": "normal", "is_extended": 0, "source": "fake"}
            for t in _session_ts()]


def _minute_bars(n: int = 240):
    t = datetime(2026, 9, 9, 9, 31)
    bars = []
    for _ in range(n):
        bars.append((t.isoformat(timespec="seconds"), Decimal("10.0")))
        t += timedelta(minutes=1)
    return bars


class Feed:
    SOURCE = "fake"

    def day_rows(self, symbol, start, end):
        return [
            {"date": PREV, "open": Decimal("9.8"), "close": Decimal("9.9"),
             "high": Decimal("10.1"), "low": Decimal("9.7"),
             "volume": Decimal("1")},
            {"date": DATE, "open": Decimal("9.9"), "close": Decimal("10.0"),
             "high": Decimal("10.2"), "low": Decimal("9.8"),
             "volume": Decimal("1")},
        ]

    def replay_day(self, symbol, trade_date):
        if trade_date != DATE:
            raise QuoteGapError(f"{symbol} 历史分钟不可得")
        return {"level": "l1", "official_close": Decimal("10.0"),
                "prev_close": Decimal("9.9"),
                "bars": _minute_bars(), "session_date": DATE,
                "source": self.SOURCE}


class NoMinuteFeed(Feed):
    def replay_day(self, symbol, trade_date):
        raise QuoteGapError(f"{symbol} 分时不可得")


def test_pull_minute_fetches_then_serves_from_cache(authed_client):
    st = authed_client.app.state
    pm = market_data.pull_minute(st, "600000", DATE, feed=Feed())
    assert pm["cached"] is False and pm["minutes"] == 240
    assert pm["official_close"] == Decimal("10.0")
    cached = market_data.pull_minute(st, "600000", DATE, feed=Feed())
    assert cached["cached"] is True and cached["minutes"] == 240
    rows = l0store.minute_cache_get(st, "600000", DATE)
    assert len(rows) == 240 and rows[0]["source"] == "fake"


def test_official_close_binds_source_and_caches(authed_client):
    st = authed_client.app.state
    oc = market_data.official_close(st, "600000", DATE, feed=Feed())
    assert oc["official_close"] == Decimal("10.0")
    assert l0store.get_official_close_source(st, DATE) == "fake"
    assert l0store.daily_cache_get(st, "600000", DATE)["close"] == "10.0"
    oc2 = market_data.official_close(st, "600000", DATE, feed=Feed())
    assert oc2["cached"] is True


def test_is_ready_matrix(authed_client):
    st = authed_client.app.state
    r0 = market_data.is_ready(st, "600000", DATE)
    assert r0["l0_ready"] is False and r0["l1_ready"] is None
    assert r0["official_close"] is None
    l0store.upsert_l0_ticks(st, _full_day_rows())
    l0store.update_coverage(st, "600000", DATE)
    r1 = market_data.is_ready(st, "600000", DATE)
    assert r1["l0_ready"] is True and abs(float(r1["l0_coverage_rate"]) - 1) < 1e-9
    l0store.minute_cache_put(st, "600000", DATE, _minute_bars(200), "fake")
    r2 = market_data.is_ready(st, "600000", DATE)
    assert r2["l1_ready"] is False                                # 200 < 228
    l0store.minute_cache_put(st, "600000", DATE, _minute_bars(230), "fake")
    r3 = market_data.is_ready(st, "600000", DATE)
    assert r3["l1_ready"] is True                                 # 230 ≥ 228


def test_get_replay_series_l0_selection(authed_client):
    st = authed_client.app.state
    l0store.upsert_l0_ticks(st, _full_day_rows())
    l0store.update_coverage(st, "600000", DATE)
    s = market_data.get_replay_series(st, "600000", DATE, feed=Feed())
    assert s["level"] == "l0" and len(s["series"]) == 4800
    assert s["official_close"] == Decimal("10.0")
    assert s["official_close_source"] == "fake" and s["quality"] == "ok"


def test_l0_close_deviation_marks_degraded(authed_client):
    st = authed_client.app.state
    l0store.upsert_l0_ticks(st, _full_day_rows())
    l0store.upsert_l0_ticks(st, [{
        "symbol": "600000", "trade_date": DATE, "ts": "2026-09-09T15:00:01",
        "price": "10.05", "prev_close": "10.0", "pct_chg": "0.5",
        "cum_turnover": "0", "status": "normal", "is_extended": 1,
        "source": "fake"}])
    l0store.update_coverage(st, "600000", DATE)
    s = market_data.get_replay_series(st, "600000", DATE, feed=Feed())
    assert s["level"] == "l0" and s["quality"] == "degraded"
    assert s["close_candidates"][-1][1] == Decimal("10.05")
    cov = l0store.get_coverage(st, "600000", DATE)
    assert cov["degraded_reason"] == "close_deviation"            # 票级不回填语义


def test_l0_within_tolerance_stays_ok(authed_client):
    st = authed_client.app.state
    l0store.upsert_l0_ticks(st, _full_day_rows())
    l0store.upsert_l0_ticks(st, [{
        "symbol": "600000", "trade_date": DATE, "ts": "2026-09-09T15:00:01",
        "price": "10.002", "prev_close": "10.0", "pct_chg": "0.02",
        "cum_turnover": "0", "status": "normal", "is_extended": 1,
        "source": "fake"}])
    l0store.update_coverage(st, "600000", DATE)
    s = market_data.get_replay_series(st, "600000", DATE, feed=Feed())
    assert s["quality"] == "ok" and s["notes"] == "L0"


def test_get_replay_series_l1_fallback(authed_client):
    st = authed_client.app.state
    s = market_data.get_replay_series(st, "600000", DATE, feed=Feed())
    assert s["level"] == "l1" and len(s["series"]) == 240
    assert s["official_close"] == Decimal("10.0")
    assert s["prev_close"] == Decimal("9.9")
    assert l0store.get_official_close_source(st, DATE) == "fake"


def test_get_replay_series_l2_day_range_fallback(authed_client):
    st = authed_client.app.state
    s = market_data.get_replay_series(st, "600000", DATE,
                                      feed=NoMinuteFeed())
    assert s["level"] == "l2"
    assert s["official_close"] == Decimal("10.0")
    assert s["prev_close"] == Decimal("9.9")
    assert s["high"] == Decimal("10.2") and s["low"] == Decimal("9.8")
    assert l0store.get_official_close_source(st, DATE) == "fake"
    day = l0store.daily_cache_get(st, "600000", DATE)
    assert day is not None and day["close"] == "10.0"


def test_l1_root_gate_short_cache_drops_to_l2(authed_client):
    """本地 L1 根数 < 应有×(1−5%)（缺失>5%）→ L1 不就绪降 L2（spec-03 §4.1 A4）。"""
    st = authed_client.app.state
    l0store.minute_cache_put(st, "600000", DATE, _minute_bars(200), "fake")
    s = market_data.get_replay_series(st, "600000", DATE, feed=Feed())
    assert s["level"] == "l2" and s["official_close"] == Decimal("10.0")


def test_l1_root_gate_within_tolerance_marks_degraded(authed_client):
    """本地 L1 缺失 ≤5%（容差带内）→ 仍走 L1，当日标 degraded。"""
    st = authed_client.app.state
    l0store.minute_cache_put(st, "600000", DATE, _minute_bars(235), "fake")
    s = market_data.get_replay_series(st, "600000", DATE, feed=Feed())
    assert s["level"] == "l1" and len(s["series"]) == 235
    assert s["quality"] == "degraded" and "缺 5 根" in s["notes"]


def test_l1_cached_mismatch_official_drops_to_l2(authed_client):
    """L1 缓存会话末价与官方收盘不一致 → 不抛错，兜 L2 日线区间。"""
    st = authed_client.app.state
    bars = _minute_bars()
    bars[-1] = (bars[-1][0], Decimal("10.05"))
    l0store.minute_cache_put(st, "600000", DATE, bars, "fake")
    s = market_data.get_replay_series(st, "600000", DATE, feed=Feed())
    assert s["level"] == "l2"
    assert s["official_close"] == Decimal("10.0") and s["high"] == Decimal("10.2")


class NoDayFeed(NoMinuteFeed):
    """当日与历史日线整体缺口（非交易日/数据未就绪 → 应显式 QuoteGap，不虚构）。"""

    def day_rows(self, symbol, start, end):
        raise QuoteGapError(f"{symbol} {start}..{end} 日线整体未就绪")


class PrevOnlyFeed(NoMinuteFeed):
    """有前收、当日无日线 → daily_pair=None；settle_input 应拒供（prev_close 不虚构）。"""

    def day_rows(self, symbol, start, end):
        return [
            {"date": PREV, "open": Decimal("9.8"), "close": Decimal("9.9"),
             "high": Decimal("10.1"), "low": Decimal("9.7"),
             "volume": Decimal("1")},
        ]


def test_daily_pair_cached_and_missing_day(authed_client):
    st = authed_client.app.state
    p = market_data.daily_pair(st, "600000", DATE, feed=Feed())
    assert p["official_close"] == Decimal("10.0")
    assert p["prev_close"] == Decimal("9.9")
    cached = market_data.daily_pair(st, "600000", DATE, feed=Feed())
    assert cached["cached"] is True and cached["source"] == "fake"
    assert market_data.daily_pair(st, "600000", PREV, feed=PrevOnlyFeed()) is None
    with pytest.raises(QuoteGapError):                # 未缓存日整体缺口 → 显式异常
        market_data.daily_pair(st, "600000", "2026-09-10", feed=NoDayFeed())


def test_settle_input_l0_enriches_prev_close(authed_client):
    """settle_day 选档入口：L0 档补 prev_close（settle_input，spec-03 §4.1 接线）。"""
    st = authed_client.app.state
    l0store.upsert_l0_ticks(st, _full_day_rows())
    l0store.update_coverage(st, "600000", DATE)
    inp = market_data.settle_input(st, "600000", DATE, feed=Feed())
    assert inp["level"] == "l0" and len(inp["series"]) == 4800
    assert inp["official_close"] == Decimal("10.0")
    assert inp["prev_close"] == Decimal("9.9")       # L0 档前收来自日线前一日
    assert inp["source"] == "fake" and inp["quality"] == "ok"


def test_settle_input_l2_fallback_and_gap(authed_client):
    st = authed_client.app.state
    inp = market_data.settle_input(st, "600000", DATE, feed=NoMinuteFeed())
    assert inp["level"] == "l2" and inp["prev_close"] == Decimal("9.9")
    with pytest.raises(QuoteGapError):
        market_data.settle_input(st, "600000", DATE, feed=NoDayFeed())
    with pytest.raises(QuoteGapError):                # 有前收无当日 → 拒供，不虚构
        market_data.settle_input(st, "600000", DATE, feed=PrevOnlyFeed())

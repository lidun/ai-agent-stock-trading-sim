"""spec-03 §5/§6 本地行情库测试：L0 序列幂等/计数/覆盖率分母口径、绑定、观察集合与
清单、L1 分钟与日线缓存。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from core import l0store


def _tick(symbol, trade_date, ts, price="10.0", status="normal",
          is_extended=0):
    return {"symbol": symbol, "trade_date": trade_date, "ts": ts,
            "price": price, "prev_close": "10.0", "pct_chg": "0",
            "cum_turnover": "0", "status": status,
            "is_extended": is_extended, "source": "tencent"}


def test_upsert_idempotent_and_counts(authed_client):
    st = authed_client.app.state
    rows = [_tick("600000", "2026-09-09", "2026-09-09T09:30:00"),
            _tick("600000", "2026-09-09", "2026-09-09T09:30:03"),
            _tick("600000", "2026-09-09", "2026-09-09T09:30:06"),
            _tick("600000", "2026-09-09", "2026-09-09T09:30:09",
                  status="suspended"),
            _tick("600000", "2026-09-09", "2026-09-09T15:00:01",
                  is_extended=1)]
    assert l0store.upsert_l0_ticks(st, rows) == len(rows)
    assert l0store.upsert_l0_ticks(st, rows) == len(rows)       # 同 ts 幂等
    assert len(l0store.get_l0_ticks(st, "600000", "2026-09-09")) == 3
    assert len(l0store.get_l0_ticks(
        st, "600000", "2026-09-09", extended=True)) == 4         # +延续样本
    c = l0store.l0_counts(st, "600000", "2026-09-09")
    assert c == {"normal": 3, "suspended": 1, "extended": 1}


def test_coverage_denominator_minus_suspended(authed_client):
    st = authed_client.app.state
    assert l0store.expected_ticks(0) == 4800
    assert l0store.expected_ticks(120) == (14400 - 120) // 3
    assert l0store.expected_ticks(14400) == 0
    l0store.upsert_l0_ticks(
        st, [_tick("600000", "2026-09-09", f"2026-09-09T{ts}")
             for ts in ("09:30:00", "09:30:03")])
    l0store.upsert_l0_ticks(
        st, [_tick("600000", "2026-09-09", "2026-09-09T09:31:00",
                   status="suspended")])
    stats = l0store.refresh_coverage(st, "600000", "2026-09-09")
    assert stats["suspended_secs"] == 3                          # 剔除 1 个周期
    assert stats["expected"] == (14400 - 3) // 3
    assert stats["actual"] == 2
    assert abs(stats["coverage_rate"] - Decimal(2) / Decimal((14400 - 3) // 3)) < 1e-12
    cov = l0store.update_coverage(st, "600000", "2026-09-09",
                                  quality="degraded",
                                  degraded_reason="coverage_gap",
                                  notes="测试")
    assert cov["actual_ticks"] == 2 and cov["suspended_secs"] == 3
    assert cov["degraded_reason"] == "coverage_gap"
    got = l0store.get_coverage(st, "600000", "2026-09-09")
    assert got["coverage_rate"] == cov["coverage_rate"]
    assert len(l0store.coverage_for_day(st, "2026-09-09")) == 1


def test_binding_first_wins_and_official_close_source(authed_client):
    st = authed_client.app.state
    l0store.set_source_binding(st, "600000", "2026-09-09", "tencent")
    l0store.set_source_binding(st, "600000", "2026-09-09", "sina")  # 次日前的原样守卫
    assert l0store.get_source_binding(st, "600000", "2026-09-09") == "tencent"
    assert l0store.day_binding_sources(st, "2026-09-09") == ["tencent"]
    l0store.set_official_close_source(st, "2026-09-09", "tencent")
    assert l0store.get_official_close_source(st, "2026-09-09") == "tencent"
    l0store.set_official_close_source(st, "2026-09-09", "eastmoney")
    assert l0store.get_official_close_source(st, "2026-09-09") == "eastmoney"


def test_observation_cap_reactivate_and_remove(authed_client):
    st = authed_client.app.state
    for i in range(20):
        l0store.observation_add(st, f"6000{i:02d}")
    with pytest.raises(ValueError):
        l0store.observation_add(st, "600100")
    assert len(l0store.observation_list(st)) == 20
    l0store.observation_add(st, "600001")                         # 幂等：保留原行
    assert len(l0store.observation_list(st)) == 20
    assert l0store.observation_remove(st, "600001")
    assert len(l0store.observation_list(st)) == 19


def test_watchlist_replace_and_read(authed_client):
    st = authed_client.app.state
    n = l0store.replace_watchlist(
        st, "2026-09-09", [{"symbol": "600000", "reason": "order"},
                           {"symbol": "000001", "reason": "observation"}])
    assert n == 2
    assert [w["symbol"] for w in l0store.watchlist_for(st, "2026-09-09")] == [
        "000001", "600000"]
    l0store.replace_watchlist(st, "2026-09-09",
                              [{"symbol": "600000", "reason": "holding"}])
    got = l0store.watchlist_for(st, "2026-09-09")
    assert len(got) == 1 and got[0]["reason"] == "holding"        # 按日重建幂等


def test_minute_and_daily_cache(authed_client):
    st = authed_client.app.state
    bars = [("09:31", "10.0"), ("09:32", "10.1")]
    assert l0store.minute_cache_put(st, "600000", "2026-09-09", bars, "tencent") == 2
    got = l0store.minute_cache_get(st, "600000", "2026-09-09")
    assert [(r["minute"], r["close"]) for r in got] == bars
    assert got[0]["source"] == "tencent" and got[0]["fetched_ts"]
    day = {"open": "9.9", "close": "10.0", "high": "10.2", "low": "9.8",
           "volume": "123"}
    l0store.daily_cache_put(st, "600000", "2026-09-09", day, "tencent")
    l0store.daily_cache_put(st, "600000", "2026-09-08", day, "tencent")
    assert l0store.daily_cache_get(st, "600000", "2026-09-09")["close"] == "10.0"
    assert len(l0store.daily_cache_range(st, "600000", "2026-09-01",
                                         "2026-09-09")) == 2

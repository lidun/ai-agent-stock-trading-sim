"""spec-03 §3/§12 采集器测试：会话档位/清单计算与采集落库/停牌剔除/退避/当日绑定不切源/
延续采样 is_extended 隔离/收盘覆盖率收敛（含 90% 边界）。"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from core import collector, l0store, source_health
from core.collector import MarketCollector


def _dt(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 9, 9, h, m, s)


def _session_ts():
    out = []
    t = _dt(9, 30)
    while t < _dt(11, 30):
        out.append(t)
        t += timedelta(seconds=3)
    t = _dt(13, 0)
    while t < _dt(15, 0):
        out.append(t)
        t += timedelta(seconds=3)
    return out


def _full_day_rows(symbol: str = "600000", trade_date: str = "2026-09-09"):
    return [{"symbol": symbol, "trade_date": trade_date,
             "ts": t.isoformat(timespec="seconds"), "price": "10.0",
             "prev_close": "10.0", "pct_chg": "0", "cum_turnover": "0",
             "status": "normal", "is_extended": 0, "source": "tencent"}
            for t in _session_ts()]


class Snap:
    SOURCE = "tencent"

    def __init__(self, suspended: bool = False, boom: bool = False):
        self.suspended = suspended
        self.boom = boom

    def realtime_batch(self, symbols):
        if self.boom:
            raise RuntimeError("源不可达")
        out = {}
        for s in symbols:
            if self.suspended:
                out[s] = {"price": Decimal("0"), "prev_close": Decimal("10.0"),
                          "name": s + "停牌", "ts": ""}
            else:
                out[s] = {"price": Decimal("10.0"),
                          "prev_close": Decimal("10.0"), "name": s, "ts": ""}
        return out


def test_session_of():
    assert collector.session_of(_dt(9, 20)) == "preopen"
    assert collector.session_of(_dt(10, 0)) == "morning"
    assert collector.session_of(_dt(12, 0)) == "lunch"
    assert collector.session_of(_dt(14, 0)) == "afternoon"
    assert collector.session_of(_dt(15, 0, 30)) == "extended"
    assert collector.session_of(_dt(15, 3)) == "closed"


def test_cycle_collects_and_binds(authed_client):
    st = authed_client.app.state
    l0store.observation_add(st, "600000")
    c = MarketCollector(st, providers={"tencent": Snap()})
    out = c.cycle(_dt(10, 0))
    assert out["status"] == "collected" and out["source"] == "tencent"
    assert out["symbols"] == ["600000"] and out["samples"] == 1
    assert l0store.get_source_binding(st, "600000", "2026-09-09") == "tencent"
    ticks = l0store.get_l0_ticks(st, "600000", "2026-09-09")
    assert len(ticks) == 1 and ticks[0]["ts"] == "2026-09-09T10:00:00"
    watch = l0store.watchlist_for(st, "2026-09-09")
    assert [w["symbol"] for w in watch] == ["600000"]
    assert watch[0]["reason"] == "observation"


def test_cycle_records_source_health(authed_client):
    st = authed_client.app.state
    l0store.observation_add(st, "600001")
    MarketCollector(st, providers={"tencent": Snap()}).cycle(_dt(10, 0))
    h = source_health.get_health(st, "tencent", "2026-09-09")
    assert h["calls"] == 1 and h["success"] == 1 and h["kind"] == "collect"
    MarketCollector(st, providers={"tencent": Snap(boom=True)}).cycle(_dt(10, 0, 3))
    h2 = source_health.get_health(st, "tencent", "2026-09-09")
    assert h2["fail"] == 1 and h2["consecutive_failures"] == 1


def test_cycle_empty_watchlist_no_network(authed_client):
    st = authed_client.app.state
    c = MarketCollector(st, providers={"tencent": Snap(boom=True)})
    out = c.cycle(_dt(10, 0))
    assert out["status"] == "empty_watchlist"
    assert c.status()["consecutive_fails"] == 0


def test_suspended_sample_excluded_from_judgement(authed_client):
    st = authed_client.app.state
    l0store.observation_add(st, "600000")
    c = MarketCollector(st, providers={"tencent": Snap(suspended=True)})
    out = c.cycle(_dt(10, 0))
    assert out["status"] == "collected" and out["samples"] == 1
    counts = l0store.l0_counts(st, "600000", "2026-09-09")
    assert counts["suspended"] == 1 and counts["normal"] == 0
    assert l0store.get_l0_ticks(st, "600000", "2026-09-09") == []  # 不进判定序列


def test_backoff_after_three_consecutive_failures(authed_client):
    st = authed_client.app.state
    l0store.observation_add(st, "600000")
    c = MarketCollector(st, providers={"tencent": Snap(boom=True)})
    for m in (10, 0), (10, 0, 3), (10, 0, 6):
        out = c.cycle(_dt(*m))
        assert out["status"] == "source_error"
    assert c.status()["consecutive_fails"] == 3
    assert c.status()["backoff"] is True
    out = c.cycle(_dt(10, 0, 9))
    assert out["status"] == "backoff"                             # 指数退避内不发请求
    assert l0store.get_l0_ticks(st, "600000", "2026-09-09") == []


def test_day_binding_beats_candidate_order(authed_client):
    st = authed_client.app.state
    l0store.observation_add(st, "600000")
    l0store.set_source_binding(st, "600000", "2026-09-09", "tencent")
    c = MarketCollector(st, configured=["sina", "tencent"],
                        providers={"sina": Snap(), "tencent": Snap()})
    out = c.cycle(_dt(10, 0))
    assert out["source"] == "tencent"                             # 绑定优先：当日不切源
    assert l0store.get_source_binding(st, "600000", "2026-09-09") == "tencent"


def test_extended_sample_isolated_and_finalize_degraded(authed_client):
    st = authed_client.app.state
    l0store.observation_add(st, "600000")
    c = MarketCollector(st, providers={"tencent": Snap()})
    assert c.cycle(_dt(10, 0))["status"] == "collected"
    out = c.cycle(_dt(15, 0, 30))
    assert out["status"] == "collected"
    counts = l0store.l0_counts(st, "600000", "2026-09-09")
    assert counts["normal"] == 1 and counts["extended"] == 1
    assert len(l0store.get_l0_ticks(st, "600000", "2026-09-09")) == 1  # 延续不进判定
    out = c.cycle(_dt(15, 3))
    assert out["status"] == "idle"
    cov = l0store.get_coverage(st, "600000", "2026-09-09")
    assert cov["quality"] == "degraded"
    assert cov["degraded_reason"] == "coverage_gap"
    assert cov["actual_ticks"] == 1 and cov["expected_ticks"] == 4800
    assert c.status()["finalized"] is True


def test_full_day_coverage_ok_boundary(authed_client):
    st = authed_client.app.state
    l0store.observation_add(st, "600000")
    assert len(_session_ts()) == 4800
    l0store.upsert_l0_ticks(st, _full_day_rows())
    c = MarketCollector(st, providers={"tencent": Snap()})
    c.cycle(_dt(15, 3))
    cov = l0store.get_coverage(st, "600000", "2026-09-09")
    assert cov["quality"] == "ok" and cov["degraded_reason"] == ""
    assert abs(float(cov["coverage_rate"]) - 1.0) < 1e-9


def test_89p9_coverage_degraded_boundary(authed_client):
    st = authed_client.app.state
    l0store.observation_add(st, "600000")
    rows = _full_day_rows()[:4319]                                # 4319/4800 < 90%
    l0store.upsert_l0_ticks(st, rows)
    c = MarketCollector(st, providers={"tencent": Snap()})
    c.cycle(_dt(15, 3))
    cov = l0store.get_coverage(st, "600000", "2026-09-09")
    assert cov["quality"] == "degraded"
    assert cov["degraded_reason"] == "coverage_gap"

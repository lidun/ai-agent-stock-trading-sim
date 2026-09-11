"""数据源健康度与故障切换测试（spec-03 §9 / §2.2）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core import source_health
from core.db import state_conn

_BJT = timezone(timedelta(hours=8))
_TD = "2026-09-11"
_T0 = datetime(2026, 9, 11, 10, 0, tzinfo=_BJT)


def _burst(st, source: str, *, total: int, ok: int, kind: str = "pull",
           now: datetime = _T0) -> None:
    for i in range(total):
        source_health.record_call(
            st, source=source, ok=i < ok, latency_ms=100.0, kind=kind,
            source_family="akshare", trade_date=_TD, now=now)


def test_grade_thresholds(authed_client):
    st = authed_client.app.state
    _burst(st, "src-a", total=10, ok=10)
    assert source_health.get_health(st, "src-a", _TD)["score"] == "A"
    _burst(st, "src-b", total=10, ok=9)
    assert source_health.get_health(st, "src-b", _TD)["score"] == "B"
    _burst(st, "src-c", total=10, ok=8)
    h = source_health.get_health(st, "src-c", _TD)
    assert h["score"] == "C" and h["c_since_ts"]


def test_metrics_p50_p95_and_degraded(authed_client):
    st = authed_client.app.state
    for ms in (100, 200, 300, 400, 5000):
        source_health.record_call(st, source="src-m", ok=True, latency_ms=ms,
                                  trade_date=_TD, now=_T0)
    source_health.record_call(st, source="src-m", ok=True, latency_ms=50,
                              degraded=True, trade_date=_TD, now=_T0)
    h = source_health.get_health(st, "src-m", _TD)
    assert h["calls"] == 6 and h["degraded_count"] == 1
    assert h["p50_ms"] == 200 and h["p95_ms"] == 5000


def test_pull_switch_same_day(authed_client):
    st = authed_client.app.state
    _burst(st, "src-p", total=2, ok=0)
    early = source_health.evaluate_switch(st, source="src-p", trade_date=_TD,
                                          now=_T0)
    assert early["action"] == "retreat"
    late = source_health.evaluate_switch(
        st, source="src-p", trade_date=_TD, now=_T0 + timedelta(minutes=11))
    assert late["action"] == "switch" and late["effective"] == "today"
    assert source_health.get_health(st, "src-p", _TD)["switched"] == 1
    again = source_health.evaluate_switch(
        st, source="src-p", trade_date=_TD, now=_T0 + timedelta(minutes=12))
    assert again["action"] == "already_switched"
    n = state_conn(st).execute(
        "SELECT COUNT(*) n FROM audit_logs WHERE action='source.switch'").fetchone()["n"]
    assert n == 1


def test_collect_switch_next_day(authed_client):
    st = authed_client.app.state
    _burst(st, "src-col", total=2, ok=0, kind="collect")
    r = source_health.evaluate_switch(
        st, source="src-col", trade_date=_TD, now=_T0 + timedelta(minutes=11))
    assert r["action"] == "switch_next_day" and r["effective"] == "next_day"


def test_recover_after_cooldown(authed_client):
    st = authed_client.app.state
    _burst(st, "src-r", total=2, ok=0)
    source_health.evaluate_switch(st, source="src-r", trade_date=_TD,
                                  now=_T0 + timedelta(minutes=11))
    wait = source_health.probe_recover(st, source="src-r", ok=True,
                                       trade_date=_TD, now=_T0 + timedelta(minutes=12))
    assert wait["recovered"] is False and wait["waiting_cooldown"] is True
    first = source_health.probe_recover(st, source="src-r", ok=True,
                                        trade_date=_TD, now=_T0 + timedelta(minutes=45))
    assert first["recovered"] is False and first["recover_successes"] == 1
    second = source_health.probe_recover(st, source="src-r", ok=True,
                                         trade_date=_TD, now=_T0 + timedelta(minutes=46))
    assert second["recovered"] is True
    h = source_health.get_health(st, "src-r", _TD)
    assert h["switched"] == 0 and h["c_since_ts"] == ""


def test_snapshot_route(authed_client):
    st = authed_client.app.state
    _burst(st, "src-route", total=10, ok=10)
    r = authed_client.get("/api/market/source-health",
                          params={"trade_date": _TD})
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert [x["source"] for x in rows] == ["src-route"]
    assert rows[0]["score"] == "A"

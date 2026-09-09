"""spec-03 数据服务路由测试：观察集合 CRUD/清单计算与刷新/采集状态/覆盖率/回放供给。"""
from __future__ import annotations

from decimal import Decimal

from core import l0store, market_data
from conftest import csrf_headers


def test_observation_routes(authed_client):
    h = csrf_headers(authed_client)
    r = authed_client.put("/api/market/observation",
                          json={"symbol": "600000", "reason": "盘中观察"},
                          headers=h)
    assert r.status_code == 200 and r.json()["observation"]["active"] == 1
    bad = authed_client.put("/api/market/observation",
                            json={"symbol": "abc"}, headers=h)
    assert bad.status_code == 400
    got = authed_client.get("/api/market/observation")
    assert got.status_code == 200 and len(got.json()["rows"]) == 1
    d = authed_client.delete("/api/market/observation/600000", headers=h)
    assert d.status_code == 200 and d.json()["removed"] is True
    assert authed_client.get("/api/market/observation").json()["rows"] == []


def test_watchlist_derive_refresh_and_read(authed_client):
    st = authed_client.app.state
    h = csrf_headers(authed_client)
    assert authed_client.put(
        "/api/market/observation", json={"symbol": "600000"}, headers=h).status_code == 200
    pre = authed_client.get("/api/market/watchlist/derive",
                            params={"trade_date": "2026-09-09"})
    assert pre.status_code == 200 and pre.json()["count"] == 1
    assert pre.json()["rows"][0]["reason"] == "observation"
    ref = authed_client.put("/api/market/watchlist/refresh",
                            params={"trade_date": "2026-09-09"}, headers=h)
    assert ref.json()["ok"] and ref.json()["count"] == 1
    got = authed_client.get("/api/market/watchlist",
                            params={"trade_date": "2026-09-09"})
    assert got.json()["rows"][0]["symbol"] == "600000"
    assert l0store.watchlist_for(st, "2026-09-09")[0]["reason"] == "observation"


def test_collector_status_disabled_by_default(authed_client):
    r = authed_client.get("/api/market/collector/status")
    assert r.status_code == 200 and r.json()["enabled"] is False


def test_coverage_and_ready_route(authed_client):
    st = authed_client.app.state
    l0store.upsert_l0_ticks(st, [{
        "symbol": "600000", "trade_date": "2026-09-09",
        "ts": "2026-09-09T10:00:00", "price": "10.0", "prev_close": "10.0",
        "pct_chg": "0", "cum_turnover": "0", "status": "normal",
        "is_extended": 0, "source": "fake"}])
    l0store.update_coverage(st, "600000", "2026-09-09",
                            quality="degraded", degraded_reason="coverage_gap")
    r = authed_client.get("/api/market/coverage",
                          params={"symbol": "600000",
                                  "trade_date": "2026-09-09"})
    assert r.status_code == 200
    body = r.json()
    assert body["coverage"]["degraded_reason"] == "coverage_gap"
    assert body["ready"]["l0_ready"] is False
    day = authed_client.get("/api/market/coverage",
                            params={"trade_date": "2026-09-09"})
    assert len(day.json()["coverage"]) == 1


def test_replay_route_level_selection(authed_client, monkeypatch):
    def fake(st, symbol, trade_date):
        return {"level": "l0", "series": [("t", Decimal("10.0"))],
                "close_candidates": [], "official_close": Decimal("10.0"),
                "official_close_source": "fake", "source": "fake",
                "quality": "ok", "notes": "L0"}
    monkeypatch.setattr(market_data, "get_replay_series", fake)
    r = authed_client.get("/api/market/replay/600000",
                          params={"trade_date": "2026-09-09"})
    assert r.status_code == 200 and r.json()["level"] == "l0"
    assert r.json()["samples"] == 1 and r.json()["official_close"] == "10.0"


def test_pull_minute_route(authed_client, monkeypatch):
    h = csrf_headers(authed_client)
    def fake(st, symbol, trade_date):
        return {"cached": False, "minutes": 240, "source": "fake",
                "official_close": Decimal("10.0"),
                "prev_close": None, "bars": [], "symbol": symbol,
                "trade_date": trade_date}
    monkeypatch.setattr(market_data, "pull_minute", fake)
    r = authed_client.post("/api/market/pull-minute",
                           json={"symbol": "600000",
                                 "trade_date": "2026-09-09"},
                           headers=h)
    assert r.status_code == 200 and r.json()["minutes"] == 240

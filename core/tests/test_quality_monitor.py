"""数据质量监控聚合（spec-02 §7 / spec-03）：settlement/卖出跟踪/成交标签 纯读。"""
from __future__ import annotations

import json

from core import quality_monitor, seed_demo
from core.db import state_conn, write_txn

_DATE1, _DATE2 = "2026-09-07", "2026-09-08"


def _main_acc_id(state) -> str:
    row = state_conn(state).execute(
        "SELECT a.id FROM accounts a JOIN agents ag ON ag.id=a.agent_id"
        " WHERE a.role='main' AND ag.id='agent-demo-001' LIMIT 1").fetchone()
    assert row is not None
    return row["id"]


def _fixture_rows(state):
    """写入真实引擎形态数据：两结算日 + 一在跟踪 + 一已结清卖出跟踪。"""
    seed_demo.seed(state)
    acc = _main_acc_id(state)
    c = state_conn(state)
    with write_txn(c) as cw:
        for i, (date, gmap) in enumerate([
            (_DATE1, {"600519": "eod_replay", "000858": "eod_replay"}),
            (_DATE2, {"600519": "eod_replay"}),
        ], start=1):
            cw.execute(
                "INSERT INTO settlement_log"
                " (id, settle_key, trade_date, account_id, granularity_used,"
                "  status, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (f"sl{i}", f"2026-09-08:{acc}:{i}", date, acc,
                 json.dumps(gmap, ensure_ascii=False),
                 "done", f"{date}T15:30:00"),
            )
        cw.execute(
            "INSERT INTO exit_trackings (id, account_id, sell_trade_id, symbol,"
            " sell_date, sell_price, qty, period_high, period_low,"
            " status, track_end_date, conclusion, quality, created_ts, done_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("et-track", acc, "t-1", "600519", _DATE1, 1700.0, 100,
             1720.0, 1680.0, "tracking", "", "", "", f"{_DATE1}T15:30:00", ""),
        )
        cw.execute(
            "INSERT INTO exit_trackings (id, account_id, sell_trade_id, symbol,"
            " sell_date, sell_price, qty, period_high, period_low,"
            " status, track_end_date, conclusion, quality, created_ts, done_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("et-done", acc, "t-2", "000858", _DATE1, 120.0, 200,
             121.0, 119.0, "done", _DATE2, "卖对", "official",
             f"{_DATE1}T15:30:00", f"{_DATE2}T15:30:00"),
        )


def test_monitor_aggregates_settle_days_and_exit_stream(authed_client):
    state = authed_client.app.state
    _fixture_rows(state)
    out = quality_monitor.monitor(state)

    assert [d["trade_date"] for d in out["days"]] == [_DATE2, _DATE1]
    day1 = out["days"][1]
    assert day1["runs"] == 1 and day1["symbols_total"] == 2
    assert day1["granularity"] == {"eod_replay": 2}
    assert out["settle_symbols_total"] == 3

    assert out["exits"]["total"] == 2
    assert out["exits"]["tracking"] == 1 and out["exits"]["done"] == 1
    assert out["exits"]["conclusions"] == {"卖对": 1}
    assert out["exits"]["by_quality"] == {"official": 1, "none": 1}

    assert out["feed"]["cross_family_check"] == "skipped_recorded"
    assert out["feed"]["note"]
    assert out["window_days"] == 60


def test_monitor_empty_db_returns_zero_aggregation(authed_client):
    state = authed_client.app.state
    seed_demo.seed(state)  # 仅目录/账户数据，无结算痕迹
    out = quality_monitor.monitor(state)
    assert out["days"] == []
    assert out["trades"] == {"total": 0, "by_quality": {}}
    assert out["exits"]["total"] == 0 and out["accounts"]["total"] >= 0


def test_quality_monitor_route_returns_payload(authed_client):
    seed_demo.seed(authed_client.app.state)
    r = authed_client.get("/api/quality/monitor?days=10")
    assert r.status_code == 200
    body = r.json()
    assert body["days"] == [] and body["feed"]["source_family"] == "tencent"
    assert body["eod_auto_settle"] is False


def test_quality_monitor_route_requires_session(client):
    seed_demo.seed(client.app.state)
    r = client.get("/api/quality/monitor")
    assert r.status_code == 401

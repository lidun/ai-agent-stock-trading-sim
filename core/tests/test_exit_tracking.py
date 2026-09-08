"""卖出跟踪只读测试（spec-06 §6.4 P3 列表，spec-01 §8.1 数据）。"""
from __future__ import annotations

from core import exit_tracking
from core.db import state_conn, write_txn

DEMO = "agent-demo-001"


def _seed(st, n: int = 3) -> None:
    conn = state_conn(st)
    rows = [
        ("et-t1", "600000", "2026-09-02", 10.0, 100, "tracking",
         "主动", "", 0.0, 0.0, 0.0, "", 0, 0, 0, 0),
        ("et-t2", "000001", "2026-09-01", 9.0, 200, "done",
         "止损", "2026-09-05", -3.2, -2.1, -1.1, "卖对", 1, 0, 0, 0),
        ("et-t3", "600519", "2026-08-28", 1500.0, 10, "done",
         "止盈", "2026-09-04", 2.5, 1.0, 1.5, "卖早", 0, 0, 0, 0),
    ]
    c = conn
    for rid, sym, date, px, qty, status, reason, end, fwd, bench, ex, concl, loss, sess, lc, lb in rows:
        with write_txn(c) as cw:
            cw.execute(
                "INSERT INTO exit_trackings (id, account_id, sell_trade_id, symbol,"
                " sell_date, sell_price, qty, sell_reason, status, track_end_date,"
                " fwd_return_pct, bench_return_pct, excess_pct, period_high, period_low,"
                " conclusion, is_loss_case, bench_sell_close, last_close, last_bench,"
                " quality, created_ts, done_ts, sessions_done)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, DEMO, f"t-{rid}", sym, date, px, qty, reason, status, end,
                 fwd, bench, ex, px + 1.0, px - 1.0, concl, loss, 0, lc, lb,
                 "", date + "T15:00:00Z", "2026-09-06T15:00:00Z", sess),
            )


def test_list_all_and_filter(authed_client):
    st = authed_client.app.state
    _seed(st)
    r = exit_tracking.list_trackings(st, DEMO)
    assert r["total"] == 3 and r["tracking"] == 1 and r["done"] == 2
    assert r["items"][0]["status"] == "tracking"          # sell_date desc 优先
    done = exit_tracking.list_trackings(st, DEMO, status="done")
    assert done["total"] == 2 and all(i["status"] == "done" for i in done["items"])
    # 角色标注（main 账户）
    assert r["items"][0]["role"] == "main"
    assert r["items"][0]["sell_reason"] == "主动"
    assert r["items"][0]["conclusion"] == ""


def test_http_roundtrip(authed_client):
    st = authed_client.app.state
    _seed(st, n=1)
    resp = authed_client.get(f"/api/agents/{DEMO}/exit-trackings")
    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == 3
    resp2 = authed_client.get(f"/api/agents/{DEMO}/exit-trackings?status=bad")
    assert resp2.status_code == 400
    assert authed_client.get("/api/agents/nope/exit-trackings").status_code == 404

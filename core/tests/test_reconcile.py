"""补跑与时效分级测试（spec-04 §2.4/§2.6）。"""
from __future__ import annotations

from datetime import datetime

from core import reconcile, tasks
from core.db import state_conn, write_txn

DEMO = "agent-demo-001"


def _add_holding(st, account_id: str = DEMO, symbol: str = "600000",
                 qty: float = 100.0) -> None:
    with write_txn(state_conn(st)) as c:
        c.execute(
            "INSERT OR IGNORE INTO holdings (id, account_id, symbol, quantity,"
            " avg_cost, updated_ts) VALUES (?,?,?,?,10.0,'2026-09-04T00:00:00+00:00')",
            (f"h-{account_id}-{symbol}", account_id, symbol, qty))


def test_expire_overdue_selection_skipped(authed_client):
    st = authed_client.app.state
    t = tasks.enqueue(st, task_type="选股", agent_id=DEMO, trade_date="2026-09-04",
                      dedup_key="sel1", is_deferrable=False)
    r = reconcile.expire_decision_tasks(
        st, today="2026-09-07", now_bj=datetime(2026, 9, 7, 10, 0))
    assert len(r["skipped"]) == 1
    assert tasks.get_task(st, t["id"])["status"] == "skipped"


def test_same_day_slot_overdue(authed_client):
    st = authed_client.app.state
    t = tasks.enqueue(st, task_type="挂单", agent_id=DEMO, trade_date="2026-09-07",
                      task_slot="09:30", dedup_key="ord1", is_deferrable=False)
    reconcile.expire_decision_tasks(
        st, today="2026-09-07", now_bj=datetime(2026, 9, 7, 9, 30))
    assert tasks.get_task(st, t["id"])["status"] == "pending"
    reconcile.expire_decision_tasks(
        st, today="2026-09-07", now_bj=datetime(2026, 9, 7, 10, 30))
    assert tasks.get_task(st, t["id"])["status"] == "skipped"


def test_overdue_report_becomes_absent(authed_client):
    st = authed_client.app.state
    t = tasks.enqueue(st, task_type="日报", agent_id=DEMO, trade_date="2026-09-04",
                      dedup_key="rep1", is_deferrable=False)
    r = reconcile.expire_decision_tasks(
        st, today="2026-09-07", now_bj=datetime(2026, 9, 7, 10, 0))
    assert tasks.get_task(st, t["id"])["status"] == "skipped"
    assert r["absent_reports"] == [{"agent_id": DEMO, "trade_date": "2026-09-04"}]
    row = state_conn(st).execute(
        "SELECT status FROM daily_reports WHERE agent_id=? AND trade_date='2026-09-04'",
        (DEMO,)).fetchone()
    assert row is not None and row["status"] == "absent"


def test_remedy_startup_enqueues_and_runs(authed_client):
    st = authed_client.app.state
    _add_holding(st)
    out = reconcile.remedy_startup(
        st, now_bj=datetime(2026, 9, 7, 10, 0), trading=True)
    assert len(out) == 1 and out[0]["account_id"] == DEMO
    rows = tasks.list_tasks(st, task_type="补救启动")
    assert len(rows) == 1 and rows[0]["status"] == "pending"
    run = tasks.run_pending(st, task_type="补救启动")
    assert run["done"] == 1
    assert tasks.get_task(st, rows[0]["id"])["status"] == "done"


def test_remedy_skips_when_active_order(authed_client):
    st = authed_client.app.state
    _add_holding(st)
    with write_txn(state_conn(st)) as c:
        c.execute(
            "INSERT INTO condition_orders (id, account_id, order_type, direction,"
            " symbol, status, created_at, creator)"
            " VALUES ('co-1', ?, 'sell_stop', 'sell', '600000', 'active',"
            " '2026-09-07T10:00:00+08:00', ?)",
            (DEMO, DEMO))
    assert reconcile.remedy_startup(
        st, now_bj=datetime(2026, 9, 7, 10, 0), trading=True) == []


def test_remedy_skips_non_trading(authed_client):
    st = authed_client.app.state
    _add_holding(st)
    assert reconcile.remedy_startup(
        st, now_bj=datetime(2026, 9, 5, 10, 0), trading=False) == []

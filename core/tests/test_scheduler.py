"""调度器 tick 与空闲窗口/资源闸门测试（spec-04 §2.2/§2.5/§7.2）。"""
from __future__ import annotations

from datetime import datetime, timezone

from core import scheduler, tasks
from core.db import state_conn, write_txn
from core.tests.conftest import csrf_headers

DEMO = "agent-demo-001"
# 2026-09-05 周六 20:00（北京）——非交易时段
OFF_HOURS = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
# 2026-09-07 周一 10:00（北京）——交易时段
TRADING = datetime(2026, 9, 7, 2, 0, tzinfo=timezone.utc)


def _cap(cpu: float, mem: float) -> dict:
    return {"cpu_avail_ratio": cpu, "mem_avail_ratio": mem, "known": True}


def _pending_approval(state, *, expires: str) -> str:
    with write_txn(state_conn(state)) as c:
        c.execute(
            "INSERT INTO approval_requests (id, type, agent_id, payload, status,"
            " expires_ts, created_ts) VALUES ('ap-1','exemption',?, '{}','pending',?,"
            " '2026-09-01T00:00:00+00:00')", (DEMO, expires))
    return "ap-1"


def test_is_trading_hours():
    assert scheduler._is_trading_hours(TRADING) is True
    assert scheduler._is_trading_hours(OFF_HOURS) is False


def test_idle_window_conditions(authed_client):
    st = authed_client.app.state
    w = scheduler.idle_window(st, now=OFF_HOURS, capacity=_cap(0.9, 0.9))
    assert w["idle"] is True and w["reason"] == "空闲窗口"

    low = scheduler.idle_window(st, now=OFF_HOURS, capacity=_cap(0.05, 0.9))
    assert low["idle"] is False and "30%" in low["reason"]

    trading = scheduler.idle_window(st, now=TRADING, capacity=_cap(0.9, 0.9))
    assert trading["idle"] is False and "交易时段" in trading["reason"]

    tasks.enqueue(st, task_type="备份", agent_id=DEMO, dedup_key="r1",
                  is_deferrable=False)
    tasks.claim_due(st)
    busy = scheduler.idle_window(st, now=OFF_HOURS, capacity=_cap(0.9, 0.9))
    assert busy["idle"] is False and busy["running"] == 1


def test_tick_expires_and_runs_deferrable(authed_client):
    st = authed_client.app.state
    _pending_approval(st, expires="2026-09-01T00:00:00+00:00")
    t = tasks.enqueue(st, task_type="kb_stats刷新", agent_id=DEMO, dedup_key="k1")

    out = scheduler.tick(st, now=OFF_HOURS, capacity=_cap(0.9, 0.9),
                         deferrable_limit=5)
    assert out["expired_approvals"] == 1
    assert out["idle"] is True and out["deferrable"]["done"] == 1
    assert tasks.get_task(st, t["id"])["status"] == "done"
    row = state_conn(st).execute(
        "SELECT status FROM approval_requests WHERE id='ap-1'").fetchone()
    assert row["status"] == "expired"


def test_tick_skips_when_not_idle(authed_client):
    st = authed_client.app.state
    t = tasks.enqueue(st, task_type="kb_stats刷新", agent_id=DEMO, dedup_key="k2")
    out = scheduler.tick(st, now=OFF_HOURS, capacity=_cap(0.05, 0.05))
    assert out["idle"] is False and out["deferrable"]["claimed"] == 0
    assert tasks.get_task(st, t["id"])["status"] == "pending"


def test_scheduler_http_roundtrip(authed_client):
    r = authed_client.get("/api/scheduler/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "capacity" in body and "idle" in body and "pending_tasks" in body

    t = authed_client.post("/api/scheduler/tick", json={"deferrable_limit": 5},
                           headers=csrf_headers(authed_client))
    assert t.status_code == 200, t.text
    assert "expired_approvals" in t.json()

"""任务崩溃恢复测试（spec-04 §2.3/§2.6）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core import scheduler, tasks

DEMO = "agent-demo-001"
BASE = datetime(2030, 1, 1, 0, 0, tzinfo=timezone.utc)


def _running(authed_client, key: str) -> str:
    st = authed_client.app.state
    t = tasks.enqueue(st, task_type="备份", agent_id=DEMO, dedup_key=key,
                      is_deferrable=False)
    tasks.claim_due(st, now=BASE.isoformat(timespec="seconds"))
    assert tasks.get_task(st, t["id"])["status"] == "running"
    return t["id"]


def test_recover_stale_after_timeout(authed_client):
    st = authed_client.app.state
    tid = _running(authed_client, "rec1")
    recovered = tasks.recover_crashed(st, now=BASE + timedelta(minutes=31))
    assert len(recovered) == 1 and recovered[0]["id"] == tid
    assert tasks.get_task(st, tid)["status"] == "failed"


def test_recover_within_threshold_keeps_running(authed_client):
    st = authed_client.app.state
    tid = _running(authed_client, "rec2")
    assert tasks.recover_crashed(st, now=BASE + timedelta(minutes=10)) == []
    assert tasks.get_task(st, tid)["status"] == "running"


def test_heartbeat_prevents_recovery(authed_client):
    st = authed_client.app.state
    tid = _running(authed_client, "rec3")
    tasks.heartbeat(st, tid, now=(BASE + timedelta(minutes=20)).isoformat(timespec="seconds"))
    assert tasks.recover_crashed(st, now=BASE + timedelta(minutes=31)) == []
    assert tasks.get_task(st, tid)["status"] == "running"


def test_recover_on_startup(authed_client):
    st = authed_client.app.state
    tid = _running(authed_client, "rec4")
    out = tasks.recover_on_startup(st)
    assert len(out) == 1
    row = tasks.get_task(st, tid)
    assert row["status"] == "failed" and "进程重启" in row["last_error"]


def test_tick_recovers_then_idle(authed_client):
    st = authed_client.app.state
    tid = _running(authed_client, "rec5")
    out = scheduler.tick(st, now=BASE + timedelta(minutes=31),
                         capacity={"cpu_avail_ratio": 0.9,
                                   "mem_avail_ratio": 0.9, "known": True})
    assert out["recovered_tasks"] == 1
    assert tasks.get_task(st, tid)["status"] == "failed"

"""管理 Agent 安全自治模式测试（spec-04 §9.1）。"""
from __future__ import annotations

from datetime import datetime, timezone

from core import manager, scheduler, tasks

DEMO = "agent-demo-001"
OFF_HOURS = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


def _cap(cpu: float, mem: float) -> dict:
    return {"cpu_avail_ratio": cpu, "mem_avail_ratio": mem, "known": True}


def test_health_failures_degrade_then_recover(authed_client):
    st = authed_client.app.state
    assert manager.status(st)["mode"] == "active"

    for i in range(2):
        manager.record_health(st, False, detail=f"ping 失败 {i}")
    assert manager.is_degraded(st) is False
    st3 = manager.record_health(st, False, detail="ping 失败 3")
    assert st3["mode"] == "autonomous" and st3["degraded"] is True
    assert st3["degraded_since"]

    manager.record_health(st, True, detail="ping ok 1")
    assert manager.is_degraded(st) is True
    st2 = manager.record_health(st, True, detail="ping ok 2")
    assert st2["mode"] == "active" and st2["recovered_ts"]


def test_resume_failures_degrade(authed_client):
    st = authed_client.app.state
    for i in range(3):
        manager.record_resume_call(st, False, detail=f"恢复失败 {i}")
    assert manager.is_degraded(st) is True


def test_degraded_defers_llm_heavy_deferrable(authed_client):
    st = authed_client.app.state
    for _ in range(3):
        manager.record_health(st, False, detail="fail")
    assert manager.is_degraded(st) is True

    llm = tasks.enqueue(st, task_type="经验提取", agent_id=DEMO,
                        dedup_key="intr-llm", resource_class="llm-heavy",
                        is_deferrable=True, payload={"report_id": "r-x"})
    light = tasks.enqueue(st, task_type="kb_stats刷新", agent_id=DEMO,
                          dedup_key="intr-light", resource_class="light",
                          is_deferrable=True)

    out = tasks.run_deferrable(st, limit=10)
    assert out["deferred"] == 1
    assert tasks.get_task(st, llm["id"])["status"] == "pending"
    assert tasks.get_task(st, light["id"])["status"] == "done"
    assert out["failed"] == 0


def test_tick_enqueues_health_and_reports_mode(authed_client):
    st = authed_client.app.state
    out = scheduler.tick(st, now=OFF_HOURS, capacity=_cap(0.9, 0.9))
    assert out["manager_mode"] in ("active", "autonomous")
    rows = tasks.list_tasks(st, task_type="健康检查")
    assert len(rows) == 1 and rows[0]["status"] == "pending"


def test_scheduler_status_exposes_manager_mode(authed_client):
    r = authed_client.get("/api/scheduler/status")
    assert r.status_code == 200, r.text
    assert r.json()["manager_mode"] in ("active", "autonomous")

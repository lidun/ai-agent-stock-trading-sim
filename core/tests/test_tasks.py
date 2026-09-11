"""调度任务表与可延迟任务组测试（spec-04 §2.1/§2.5 最小实现）。"""
from __future__ import annotations

from core import retrospective, tasks
from core.db import state_conn, write_txn
from core.tests.conftest import csrf_headers

DEMO = "agent-demo-001"


def _settle(state, sid: str, fwd, end: str = "2026-09-03") -> None:
    with write_txn(state_conn(state)) as c:
        c.execute(
            "UPDATE signal_registry SET fwd_return_pct=?, fwd_end_date=? WHERE id=?",
            (fwd, end, sid))


def test_enqueue_idempotent_and_fields(authed_client):
    st = authed_client.app.state
    first = tasks.enqueue(st, task_type="摘要补跑", agent_id=DEMO,
                          trade_date="2026-09-04", dedup_key="d1",
                          payload={"period_key": "2026-09-04"}, priority=3)
    assert first["created"] is True
    second = tasks.enqueue(st, task_type="摘要补跑", agent_id=DEMO,
                           trade_date="2026-09-04", dedup_key="d1")
    assert second["created"] is False and second["id"] == first["id"]
    row = tasks.get_task(st, first["id"])
    assert row["status"] == "pending" and row["is_deferrable"] is True
    assert row["priority"] == 3 and row["payload"]["period_key"] == "2026-09-04"


def test_enqueue_rejects_bad_input(authed_client):
    st = authed_client.app.state
    for kwargs in ({"task_type": ""}, {"task_type": "x", "resource_class": "ghost"}):
        try:
            tasks.enqueue(st, **kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"应拒绝 {kwargs}")


def test_claim_due_and_finish(authed_client):
    st = authed_client.app.state
    t = tasks.enqueue(st, task_type="备份", agent_id=DEMO, dedup_key="b1",
                      is_deferrable=False, priority=1)
    nondef = tasks.enqueue(st, task_type="备份", agent_id=DEMO, dedup_key="b2",
                           is_deferrable=False)
    claimed = tasks.claim_due(st, deferrable_only=True)
    assert claimed == []                                   # 非可延迟任务不被认领
    claimed = tasks.claim_due(st)
    ids = {c["id"] for c in claimed}
    assert ids == {t["id"], nondef["id"]}
    assert all(c["status"] == "running" and c["attempt_count"] == 1 for c in claimed)
    done = tasks.finish(st, t["id"], ok=True, detail="ok")
    assert done["status"] == "done" and done["ended_ts"]


def test_run_deferrable_dispatches_retro_attribution(authed_client):
    st = authed_client.app.state
    from core import signalstore
    sid = signalstore.register(st, DEMO, "buy", "600000", "2026-08-20",
                               concept_tag="概念T", env_bucket="cn_a_main",
                               ref_price=10.0)
    _settle(st, sid, 2.0)
    rpt = retrospective.build_report(st, DEMO, use_llm=False, actor="admin")
    assert rpt["attribution_status"] == "deferred"
    t = tasks.enqueue(st, task_type="经验提取", agent_id=DEMO, dedup_key=rpt["id"],
                      payload={"report_id": rpt["id"], "agent_id": DEMO},
                      resource_class="llm-heavy", priority=8)

    out = tasks.run_deferrable(st, task_type="经验提取", limit=5)
    assert out["claimed"] == 1 and out["done"] == 1
    assert tasks.get_task(st, t["id"])["status"] == "done"
    after = retrospective.get_report(st, rpt["id"])
    assert after["attribution_status"] == "not_configured"  # 测试环境未配置模型


def test_run_deferrable_skips_unknown_handler(authed_client):
    st = authed_client.app.state
    t = tasks.enqueue(st, task_type="L0清理", agent_id=DEMO, dedup_key="l0")
    out = tasks.run_deferrable(st)
    assert out["skipped"] == 1
    assert tasks.get_task(st, t["id"])["status"] == "skipped"


def test_kb_stats_refresh_handler(authed_client):
    st = authed_client.app.state
    t = tasks.enqueue(st, task_type="kb_stats刷新", agent_id=DEMO, dedup_key="k1")
    out = tasks.run_deferrable(st, task_type="kb_stats刷新")
    assert out["claimed"] == 1 and out["done"] == 1
    assert tasks.get_task(st, t["id"])["status"] == "done"


def test_recompute_failure_enqueues_fallback(authed_client, monkeypatch):
    st = authed_client.app.state
    from core import kb
    from core.settle_scheduler import EodSettleTrigger

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(kb, "recompute_stats", boom)
    trig = EodSettleTrigger(st, feed=None)
    r = trig._recompute_kb("2026-09-04")
    assert r.get("error") is True
    rows = tasks.list_tasks(st, task_type="kb_stats刷新")
    assert len(rows) == 1
    assert rows[0]["status"] == "pending" and rows[0]["is_deferrable"] is True
    assert rows[0]["trade_date"] == "2026-09-04"


def test_tasks_http_roundtrip(authed_client):
    h = csrf_headers(authed_client)
    r = authed_client.post("/api/tasks", json={
        "task_type": "kb_stats刷新", "agent_id": DEMO, "dedup_key": "k1",
        "is_deferrable": True}, headers=h)
    assert r.status_code == 200, r.text
    tid = r.json()["id"]

    lst = authed_client.get(f"/api/tasks?agent_id={DEMO}")
    assert lst.status_code == 200
    assert any(t["id"] == tid for t in lst.json()["tasks"])

    run = authed_client.post("/api/tasks/run", json={"limit": 5}, headers=h)
    assert run.status_code == 200, run.text
    assert run.json()["claimed"] >= 1

    bad = authed_client.post("/api/tasks", json={"task_type": ""}, headers=h)
    assert bad.status_code == 400

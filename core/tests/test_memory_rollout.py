"""市场观察滚动删除与抽检测试（spec-02 §4.3 / §12）。"""
from __future__ import annotations

from core import memory_cards, memory_rollout, summaries
from core.db import state_conn, write_txn
from core.tests.conftest import csrf_headers

DEMO = "agent-demo-001"


def _add_entry(st, key: str, *, quality: str = "normal",
               ts: str = "2026-08-10T02:00:00+00:00") -> str:
    rid = f"me-{key}"
    with write_txn(state_conn(st)) as c:
        c.execute(
            "INSERT INTO memory_entries (id, agent_id, mem_type, version_no, ts,"
            " body, ref_ids, revision, source, dedup_key, quality)"
            " VALUES (?,?,'market_note','',?,?,'[]',0,'test',?,?)",
            (rid, DEMO, ts, f"观察 {key}", key, quality))
    return rid


def _prepared(st, key: str, *, card: bool = True) -> str:
    mid = _add_entry(st, key)
    if card:
        memory_cards.generate_card(st, mid, use_llm=False)
    return mid


def test_inspect_passes_with_cards(authed_client):
    st = authed_client.app.state
    a = _prepared(st, "i1")
    b = _prepared(st, "i2")
    summaries.generate(st, DEMO, "day", "2026-08-10", use_llm=False)
    r = memory_rollout.inspect_period(st, DEMO, "day", "2026-08-10",
                                      sample_ratio=1.0)
    assert r["passed"] is True
    assert r["checked"] == 2 and r["total"] == 2
    assert set(summaries.get_summary(st, DEMO, "day", "2026-08-10")["record_ids"]) == {a, b}
    assert memory_rollout.list_inspections(st, DEMO)[0]["id"] == r["id"]


def test_inspect_flags_missing_card(authed_client):
    st = authed_client.app.state
    _prepared(st, "m1", card=False)
    summaries.generate(st, DEMO, "day", "2026-08-10", use_llm=False)
    r = memory_rollout.inspect_period(st, DEMO, "day", "2026-08-10",
                                      sample_ratio=1.0)
    assert r["passed"] is False and r["missing_cards"] == 1


def test_roll_requires_summary_and_inspection(authed_client):
    st = authed_client.app.state
    _prepared(st, "s1")
    # 有卡片但无日摘要、无抽检凭证 → 不动
    r = memory_rollout.roll_market_notes(st, agent_id=DEMO, before="2026-09-01")
    assert r["deleted"] == 0 and r["skipped"] == 1


def test_roll_cascades_cards_and_entries(authed_client):
    st = authed_client.app.state
    a = _prepared(st, "r1")
    b = _prepared(st, "r2")
    summaries.generate(st, DEMO, "day", "2026-08-10", use_llm=False)
    memory_rollout.inspect_period(st, DEMO, "day", "2026-08-10", sample_ratio=1.0)
    calls: list[list[str]] = []
    r = memory_rollout.roll_market_notes(
        st, agent_id=DEMO, before="2026-09-01",
        vector_delete=lambda _st, ids: calls.append(list(ids)))
    assert r["deleted"] == 2 and set(r["memory_ids"]) == {a, b}
    assert calls and set(calls[0]) == {a, b}
    c = state_conn(st)
    assert c.execute("SELECT COUNT(*) n FROM memory_entries WHERE id IN (?,?)",
                     (a, b)).fetchone()["n"] == 0
    assert c.execute("SELECT COUNT(*) n FROM memory_cards WHERE memory_id IN (?,?)",
                     (a, b)).fetchone()["n"] == 0
    assert c.execute("SELECT COUNT(*) n FROM audit_logs WHERE action='memory.rollout'"
                     " AND result='ok'").fetchone()["n"] == 1


def test_flagged_blocks_roll(authed_client):
    st = authed_client.app.state
    mid = _prepared(st, "f1")
    with write_txn(state_conn(st)) as c:
        c.execute("UPDATE memory_entries SET quality='flagged' WHERE id=?", (mid,))
    summaries.generate(st, DEMO, "day", "2026-08-10", use_llm=False)
    insp = memory_rollout.inspect_period(st, DEMO, "day", "2026-08-10",
                                         sample_ratio=1.0)
    assert insp["passed"] is False and insp["flagged"] == 1
    r = memory_rollout.roll_market_notes(st, agent_id=DEMO, before="2026-09-01")
    assert r["deleted"] == 0
    assert state_conn(st).execute("SELECT COUNT(*) n FROM memory_entries WHERE id=?",
                                  (mid,)).fetchone()["n"] == 1


def test_roll_routes(authed_client):
    st = authed_client.app.state
    _prepared(st, "rt1")
    summaries.generate(st, DEMO, "day", "2026-08-10", use_llm=False)
    h = csrf_headers(authed_client)
    ri = authed_client.post("/api/memory/inspect", json={
        "agent_id": DEMO, "period": "day", "period_key": "2026-08-10",
        "sample_ratio": 1.0}, headers=h)
    assert ri.status_code == 200 and ri.json()["passed"] is True
    rr = authed_client.post("/api/memory/rollout", json={
        "agent_id": DEMO, "before": "2026-09-01"}, headers=h)
    assert rr.status_code == 200 and rr.json()["deleted"] == 1
    lst = authed_client.get("/api/memory/inspections", params={"agent_id": DEMO})
    assert lst.status_code == 200 and len(lst.json()["items"]) == 1

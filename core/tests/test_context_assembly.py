"""上下文装配测试（spec-02 §7）。"""
from __future__ import annotations

from core import context_assembly, memory_cards
from core.db import state_conn, write_txn
from core.tests.conftest import csrf_headers

DEMO = "agent-demo-001"


def _add_entry(st, key: str, body: str) -> str:
    rid = f"me-{key}"
    with write_txn(state_conn(st)) as c:
        c.execute(
            "INSERT INTO memory_entries (id, agent_id, mem_type, version_no, ts,"
            " body, ref_ids, revision, source, dedup_key, quality)"
            " VALUES (?,?,'market_note','','2026-09-11T02:00:00+00:00',?, '[]',0,"
            "'test',?, 'normal')",
            (rid, DEMO, body, key))
    return rid


def test_create_template_versions(authed_client):
    st = authed_client.app.state
    v1 = context_assembly.create_template(st, DEMO)
    assert v1["template_version"] == 1 and v1["active"] is True
    v2 = context_assembly.create_template(
        st, DEMO, route_table_={"诊断": ["近期卡片"], "fallback": ["月摘要"]})
    assert v2["template_version"] == 2
    assert context_assembly.active_template(st, DEMO)["template_version"] == 2
    old = state_conn(st).execute(
        "SELECT active, superseded_ts FROM context_templates WHERE id=?",
        (v1["id"],)).fetchone()
    assert old["active"] == 0 and old["superseded_ts"] != ""


def test_default_route_and_fallback(authed_client):
    st = authed_client.app.state
    rt = context_assembly.route_table(st, DEMO)
    assert "选股" in rt and "fallback" in rt
    assert context_assembly.effective_intent(rt, "不存在") == "fallback"
    assert context_assembly.effective_intent(rt, "选股") == "选股"


def test_assemble_blocks_and_budget(authed_client):
    st = authed_client.app.state
    mid = _add_entry(st, "a1", "券商板块放量，关注龙头")
    memory_cards.generate_card(st, mid, use_llm=False)
    out = context_assembly.assemble(st, DEMO, "选股", {"query": "券商"})
    assert out["intent"] == "选股"
    assert out["prefix_static"]
    assert out["memory_block"]["items"]
    assert out["budget"]["estimated_tokens"] > 0
    assert out["budget"]["high_cost"] is False


def test_assemble_unknown_intent_fallback(authed_client):
    st = authed_client.app.state
    out = context_assembly.assemble(st, DEMO, "外星意图")
    assert out["intent"] == "fallback"
    assert out["route"] == context_assembly.DEFAULT_ROUTE_TABLE["fallback"]


def test_assemble_high_cost_flagged(authed_client):
    st = authed_client.app.state
    st.settings.context_budget_tokens = 1
    out = context_assembly.assemble(st, DEMO, "选股",
                                    {"task_id": "t-high", "query": "任意"})
    assert out["budget"]["high_cost"] is True
    row = state_conn(st).execute(
        "SELECT high_cost_flag, result FROM performance_records WHERE task_id='t-high'"
    ).fetchone()
    assert row is not None and row["high_cost_flag"] == 1 and row["result"] == "high_cost"


def test_context_routes(authed_client):
    st = authed_client.app.state
    r = authed_client.post("/api/context/template",
                           json={"agent_id": DEMO,
                                 "route_table": {"选股": ["近期卡片"],
                                                 "fallback": ["月摘要"]}},
                           headers=csrf_headers(authed_client))
    assert r.status_code == 200 and r.json()["template_version"] == 1
    got = authed_client.get(f"/api/context/template?agent_id={DEMO}")
    assert got.status_code == 200 and got.json()["template"] is not None
    asm = authed_client.post(
        "/api/context/assemble",
        json={"agent_id": DEMO, "task_intent": "选股", "task_payload": {}},
        headers=csrf_headers(authed_client))
    assert asm.status_code == 200 and asm.json()["intent"] == "选股"

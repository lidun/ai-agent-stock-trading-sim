"""归档经验提取测试（spec-05 §5：终局统计 + LLM 归因降级 + 评审回填知识库）。"""
from __future__ import annotations

from core import kb, retrospective, signalstore
from core.db import state_conn, write_txn
from core.tests.conftest import csrf_headers

DEMO = "agent-demo-001"


def _settle(state, sid: str, fwd, end: str = "2026-09-03") -> None:
    with write_txn(state_conn(state)) as c:
        c.execute(
            "UPDATE signal_registry SET fwd_return_pct=?, fwd_end_date=? WHERE id=?",
            (fwd, end, sid))


def _sig(state, concept, fwd=None, *, bucket="cn_a_main", sig_type="buy"):
    sid = signalstore.register(state, DEMO, sig_type, "600000", "2026-08-20",
                               concept_tag=concept, env_bucket=bucket,
                               ref_price=10.0)
    if fwd is not None:
        _settle(state, sid, fwd)
    return sid


def test_final_stats_buckets_and_verdicts(authed_client):
    st = authed_client.app.state
    _sig(st, "有效概念", 4.0)
    _sig(st, "有效概念", 2.0)
    _sig(st, "有效概念", -1.0)
    _sig(st, "无效概念", -3.0)
    _sig(st, "在途概念")                                   # 未结清不计
    _sig(st, "试运行概念", 9.0)                            # trial 排除
    with write_txn(state_conn(st)) as c:
        c.execute("UPDATE signal_registry SET trial_flag=1 WHERE concept_tag='试运行概念'")

    out = retrospective.final_stats(st, DEMO)
    buckets = {b["concept_tag"]: b for b in out["buckets"]}
    assert out["signal_total"] == 5 and out["settled_n"] == 4  # trial 已在查询层排除
    assert buckets["有效概念"]["sample_n"] == 3
    assert buckets["有效概念"]["verdict"] == "effective"
    assert buckets["无效概念"]["verdict"] == "ineffective"
    assert buckets["在途概念"]["sample_n"] == 0
    assert "试运行概念" not in buckets


def test_final_stats_merges_tag_aliases(authed_client):
    st = authed_client.app.state
    ent = kb.create_entry(st, type_="positive", name="规范概念", description="x")
    kb.merge_tag(st, alias="自由标签", canonical_kb_id=ent["id"],
                 actor="admin", reason="归并")
    _sig(st, "自由标签", 5.0)

    out = retrospective.final_stats(st, DEMO)
    b = next(b for b in out["buckets"] if b["concept_tag"] == "规范概念")
    assert b["sample_n"] == 1 and b["raw_tags"] == ["自由标签"]


def test_build_report_llm_not_configured_falls_back(authed_client):
    st = authed_client.app.state
    _sig(st, "概念A", 1.0)
    rpt = retrospective.build_report(st, DEMO, actor="admin")
    assert rpt["status"] == "pending_review"
    assert rpt["attribution_status"] == "not_configured"
    assert rpt["attribution"] == ""
    assert rpt["stats"]["settled_n"] == 1
    row = state_conn(st).execute(
        "SELECT COUNT(*) AS n FROM audit_logs WHERE action='kb.retro.extract'"
    ).fetchone()
    assert row["n"] == 1


def test_build_report_unknown_agent(authed_client):
    st = authed_client.app.state
    try:
        retrospective.build_report(st, "ghost", actor="admin")
    except LookupError:
        pass
    else:
        raise AssertionError("应抛 LookupError")


def test_confirm_report_applies_merge_and_invalidate(authed_client):
    st = authed_client.app.state
    ent = kb.create_entry(st, type_="positive", name="规范概念", description="x")
    bad = kb.create_entry(st, type_="positive", name="坏概念", description="x")
    _sig(st, "自由标签", 5.0)
    rpt = retrospective.build_report(st, DEMO, actor="admin")

    done = retrospective.confirm_report(
        st, rpt["id"], actor="admin", note="归档回填",
        decisions=[
            {"concept_tag": "自由标签", "action": "merge", "kb_id": ent["id"]},
            {"concept_tag": "坏概念", "action": "invalidate", "kb_id": bad["id"]},
        ])
    assert done["status"] == "confirmed"
    assert done["review_ref"]["decided_by"] == "admin"
    applied = done["review_ref"]["applied"]
    assert any(a["action"] == "merge" and a["created"] for a in applied)
    assert any(a["action"] == "invalidate" for a in applied)
    aliases = {a["alias"]: a for a in kb.list_tag_aliases(st)}
    assert aliases["自由标签"]["canonical_kb_id"] == ent["id"]
    assert kb.get_entry(st, bad["id"])["status"] == "invalid"
    try:
        retrospective.confirm_report(st, rpt["id"], actor="admin")
    except ValueError:
        pass
    else:
        raise AssertionError("重复确认应被拒绝")


def test_retro_http_roundtrip(authed_client):
    st = authed_client.app.state
    ent = kb.create_entry(st, type_="positive", name="规范概念", description="x")
    _sig(st, "自由标签", 5.0)

    r = authed_client.post("/api/kb/retro-reports", json={"agent_id": DEMO},
                           headers=csrf_headers(authed_client))
    assert r.status_code == 200, r.text
    rid = r.json()["report"]["id"]

    lst = authed_client.get(f"/api/kb/retro-reports?agent_id={DEMO}")
    assert lst.status_code == 200 and lst.json()["reports"][0]["id"] == rid

    det = authed_client.get(f"/api/kb/retro-reports/{rid}")
    assert det.status_code == 200 and det.json()["report"]["agent_id"] == DEMO

    conf = authed_client.post(
        f"/api/kb/retro-reports/{rid}/confirm",
        json={"note": "ok", "decisions": [
            {"concept_tag": "自由标签", "action": "create", "type": "positive"}]},
        headers=csrf_headers(authed_client))
    assert conf.status_code == 200, conf.text
    assert conf.json()["report"]["status"] == "confirmed"
    assert any(a["action"] == "create" for a in conf.json()["report"]["review_ref"]["applied"])

    missing = authed_client.get("/api/kb/retro-reports/ghost")
    assert missing.status_code == 404

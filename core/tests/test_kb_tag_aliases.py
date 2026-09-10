"""概念标签治理测试（spec-05 §3.11：月度归并映射 append-only、统计按 canonical 归并、
历史信号行不改写、自由标签探索口径、归并候选提议、路由）。"""
from __future__ import annotations

import pytest

from core import kb, signalstore
from core.db import state_conn, write_txn
from core.tests.conftest import csrf_headers

DEMO = "agent-demo-001"
SPEC = {
    "trigger_rule": "收盘前 5 分钟涨幅 < 0 且成交额 < 近 20 日均值 50%",
    "computation": "按日内 5m 序列末段相邻分钟判定触达",
    "data_sources": ["l1_minute", "eod_daily"],
}


def _settle(state, sid: str, fwd, end: str = "2026-09-03", quality: str = "") -> None:
    c = state_conn(state)
    with write_txn(c) as cc:
        cc.execute(
            "UPDATE signal_registry SET fwd_return_pct=?, fwd_end_date=?, quality=?"
            " WHERE id=?", (fwd, end, quality, sid))


def _positive(state, name: str) -> dict:
    return kb.create_entry(state, type_="positive", name=name, description="x")


def test_merge_tag_append_only_idempotent_and_audit(authed_client):
    st = authed_client.app.state
    a = _positive(st, "回调低吸")
    b = _positive(st, "行业轮动加速")
    out = kb.merge_tag(st, alias="低位买入", canonical_kb_id=a["id"],
                       actor="mgr", reason="同义合并")
    assert out["created"] is True and out["canonical_kb_id"] == a["id"]
    again = kb.merge_tag(st, alias="低位买入", canonical_kb_id=a["id"])
    assert again["created"] is False                 # 同映射幂等
    with pytest.raises(ValueError):
        kb.merge_tag(st, alias="低位买入", canonical_kb_id=b["id"])  # append-only 拒绝改绑
    with pytest.raises(ValueError):
        kb.merge_tag(st, alias="回调低吸", canonical_kb_id=a["id"])  # 与规范名相同
    pit = kb.create_entry(st, name="缩量陷阱", type_="pitfall", description="z",
                          computable_spec=SPEC, severity="mid")
    with pytest.raises(ValueError):
        kb.merge_tag(st, alias="尾盘走弱", canonical_kb_id=pit["id"])  # 不归并到 pitfall
    assert [x["alias"] for x in kb.list_tag_aliases(st, canonical_kb_id=a["id"])] \
        == ["低位买入"]
    row = state_conn(st).execute(
        "SELECT COUNT(*) AS c FROM audit_logs WHERE action='kb.tag.merge'").fetchone()
    assert row["c"] >= 1


def test_stats_merge_canonical_without_rewriting_history(authed_client):
    st = authed_client.app.state
    e = _positive(st, "回调低吸")
    canon = signalstore.register(st, DEMO, "buy", "600000", "2026-08-20",
                                 concept_tag="回调低吸", env_bucket="cn_a_main",
                                 ref_price=10.0)
    free = signalstore.register(st, DEMO, "buy", "600519", "2026-08-20",
                                concept_tag="低位买入", env_bucket="cn_a_main",
                                ref_price=10.0)
    _settle(st, canon, 3.0)
    _settle(st, free, -1.0)
    kb.recompute_stats(st, kb_id=e["id"])
    assert kb.list_stats(st, kb_id=e["id"])[0]["sample_n"] == 1  # 归并前自由标签独立
    kb.merge_tag(st, alias="低位买入", canonical_kb_id=e["id"], actor="mgr")
    kb.recompute_stats(st, kb_id=e["id"])
    s = kb.list_stats(st, kb_id=e["id"])[0]
    assert s["sample_n"] == 2 and s["win_rate"] == 0.5           # 归并后按 canonical
    tags = {r["concept_tag"] for r in state_conn(st).execute(
        "SELECT concept_tag FROM signal_registry WHERE id IN (?,?)",
        (canon, free)).fetchall()}
    assert tags == {"回调低吸", "低位买入"}                        # 历史行不改写


def test_unmerged_free_tags_and_proposals(authed_client):
    st = authed_client.app.state
    _positive(st, "回调低吸")
    signalstore.register(st, DEMO, "buy", "600000", "2026-08-20",
                         concept_tag="回调低吸低吸", env_bucket="cn_a_main",
                         ref_price=10.0)
    signalstore.register(st, DEMO, "buy", "600519", "2026-08-20",
                         concept_tag="另类主题", env_bucket="cn_a_main",
                         ref_price=10.0)
    free = {f["concept_tag"]: f["signals"] for f in kb.unmerged_free_tags(st)}
    assert set(free) == {"回调低吸低吸", "另类主题"}
    props = kb.propose_tag_merges(st)
    assert props and props[0]["alias"] == "回调低吸低吸"
    assert props[0]["suggested_name"] == "回调低吸"
    kb.merge_tag(st, alias="回调低吸低吸", canonical_kb_id=props[0]["suggested_kb_id"])
    assert {f["concept_tag"] for f in kb.unmerged_free_tags(st)} == {"另类主题"}


def test_tag_aliases_http_roundtrip(authed_client):
    st = authed_client.app.state
    e = _positive(st, "回调低吸")
    h = csrf_headers(authed_client)
    r = authed_client.post("/api/kb/tag-aliases",
                           json={"alias": "低位买入", "canonical_kb_id": e["id"],
                                 "reason": "同义合并"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["created"] is True
    got = authed_client.get("/api/kb/tag-aliases").json()["aliases"]
    assert got and got[0]["alias"] == "低位买入"      # 路由未被 /kb/{id} 吞并
    bad = authed_client.post("/api/kb/tag-aliases",
                             json={"alias": "x", "canonical_kb_id": "KB-9999"},
                             headers=h)
    assert bad.status_code == 400
    ft = authed_client.get("/api/kb/free-tags").json()
    assert set(ft) == {"free_tags", "proposals"}

"""知识库测试（spec-05 §3：双闸入库/状态机/统计快照/软删，spec-06 §6.7 数据源）。"""
from __future__ import annotations

import pytest

from core import kb
from core.db import state_conn
from core.tests.conftest import csrf_headers

DEMO = "agent-demo-001"
SPEC = {
    "trigger_rule": "收盘前 5 分钟涨幅 < 0 且成交额 < 近 20 日均值 50%",
    "computation": "按日内 5m 序列末段相邻分钟判定触达",
    "data_sources": ["l1_minute", "eod_daily"],
}


def _pitfall():
    return dict(
        name="尾盘缩量走弱陷阱", type_="pitfall", description="尾盘无量阴跌多为资金退潮信号，不宜抄底",
        computable_spec=SPEC, severity="mid", env_scope="all",
        source="retrospective", origin_agent=DEMO, created_by="user",
    )


def test_create_requires_gate1_for_pitfall(authed_client):
    st = authed_client.app.state
    with pytest.raises(ValueError, match="闸1 未通过"):
        kb.create_entry(st, name="尾盘缩量", type_="pitfall",
                        computable_spec={"trigger_rule": "x"}, severity="mid")
    kw = _pitfall()
    kw["severity"] = "nope"
    with pytest.raises(ValueError, match="severity"):
        kb.create_entry(st, **kw)


def test_full_lifecycle_transitions(authed_client):
    st = authed_client.app.state
    ent = kb.create_entry(st, **_pitfall())
    assert ent["id"] == "KB-0001" and ent["status"] == "observing"
    assert ent["review_gate1_ref"]["passed"] is True
    assert ent["type_label"] == "反向避坑"
    assert ent["computable_spec"] == SPEC

    with pytest.raises(ValueError, match="闸2|评审"):
        kb.transition_kb(st, ent["id"], action="start_validation", note="")
    ent = kb.transition_kb(st, ent["id"], action="start_validation",
                           note="评审通过：逻辑一致且可证伪", actor="user")
    assert ent["status"] == "validating"
    assert ent["review_gate2_ref"]["note"] == "评审通过：逻辑一致且可证伪"

    with pytest.raises(ValueError, match="原因"):
        kb.transition_kb(st, ent["id"], action="seal", note="")
    ent = kb.transition_kb(st, ent["id"], action="seal",
                           note="样本不足 30，验证预算到期封存")
    assert ent["status"] == "sealed" and ent["sealed_reason"]

    ent = kb.transition_kb(st, ent["id"], action="start_validation",
                           note="新证据复核重新进入验证")
    assert ent["status"] == "validating"

    ent = kb.transition_kb(st, ent["id"], action="approve_valid",
                           note="单桶 n≥30 期望值为正（statistics-driven）")
    assert ent["status"] == "valid"

    ent = kb.transition_kb(st, ent["id"], action="invalidate",
                           note="滚动 60 交易日期望值 <0，失效候选确认")
    assert ent["status"] == "invalid" and ent["invalid_reason"]

    with pytest.raises(ValueError, match="不允许"):
        kb.transition_kb(st, ent["id"], action="approve_valid", note="x")


def test_soft_delete_restore_keeps_stats(authed_client):
    st = authed_client.app.state
    ent = kb.create_entry(st, type_="positive", name="高股息防守轮动",
                          description="红利风格防御型轮动方法论")
    kb.upsert_stats(st, ent["id"], env_bucket="cn_a_main",
                    sample_n=32, win_rate=0.56, avg_win=2.1, avg_loss=-1.4,
                    expectancy=0.56, dispatch_n=12, note="历史样本")
    got = kb.list_stats(st, kb_id=ent["id"])
    assert len(got) == 1 and got[0]["sample_n"] == 32

    kb.update_entry(st, ent["id"], actor="user", deleted=True, note="口径过时")
    assert kb.get_entry(st, ent["id"])["deleted_ts"]
    # 默认列表不含已软删；统计保留供审计
    assert all(e["id"] != ent["id"] for e in kb.list_entries(st))
    assert len(kb.list_stats(st, kb_id=ent["id"])) == 1

    kb.update_entry(st, ent["id"], actor="user", restore=True, note="复核重新激活")
    ent2 = kb.get_entry(st, ent["id"])
    assert not ent2["deleted_ts"]
    assert ent2["status"] == "observing"
    with pytest.raises(ValueError, match="不能为负"):
        kb.upsert_stats(st, ent["id"], env_bucket="cn_a_main", sample_n=-1)


def test_http_api_roundtrip(authed_client):
    h = csrf_headers(authed_client)
    r = authed_client.post("/api/kb", json={
        "name": "破位跳空陷阱", "type": "pitfall",
        "description": "向下跳空破位当日不宜追涨补仓",
        "computable_spec": SPEC, "severity": "high", "source": "user",
    }, headers=h)
    assert r.status_code == 200, r.text
    eid = r.json()["entry"]["id"]
    assert r.json()["entry"]["status"] == "observing"

    # 闸1 拦截：避坑缺 computation → 400
    bad = authed_client.post("/api/kb", json={
        "name": "x", "type": "pitfall", "computable_spec": {"trigger_rule": "a"},
        "severity": "low"}, headers=h)
    assert bad.status_code == 400

    r2 = authed_client.post(f"/api/kb/{eid}/transition", json={
        "action": "start_validation", "note": "逻辑一致可证伪，查重通过"}, headers=h)
    assert r2.status_code == 200 and r2.json()["entry"]["status"] == "validating"

    r3 = authed_client.get("/api/kb")
    assert r3.status_code == 200
    assert any(e["id"] == eid for e in r3.json()["entries"])

    r4 = authed_client.get(f"/api/kb/{eid}")
    assert r4.status_code == 200 and r4.json()["entry"]["stats"] == []

    r5 = authed_client.post(f"/api/kb/{eid}/stats", json={
        "env_bucket": "cn_a_main", "sample_n": 30, "win_rate": 0.5,
        "expectancy": 0.3, "note": "接口写入"}, headers=h)
    assert r5.status_code == 200 and r5.json()["stats"]["sample_n"] == 30

    # 已软删后迁移 → 400；恢复后可再次启动验证
    assert authed_client.post(f"/api/kb/{eid}/delete", json={}, headers=h).status_code == 200
    r6 = authed_client.post(f"/api/kb/{eid}/transition", json={
        "action": "seal", "note": "x"}, headers=h)
    assert r6.status_code == 400
    assert authed_client.post(f"/api/kb/{eid}/restore", json={}, headers=h).status_code == 200

    r7 = authed_client.patch(f"/api/kb/{eid}", json={
        "name": "破位跳空陷阱·修订", "note": "名称口径修订"}, headers=h)
    assert r7.status_code == 200 and r7.json()["entry"]["name"] == "破位跳空陷阱·修订"

    audit = state_conn(authed_client.app.state).execute(
        "SELECT action FROM audit_logs WHERE object_id=? ORDER BY ts", (eid,)).fetchall()
    assert any("kb.create" == a["action"] for a in audit)
    assert any(a["action"] == "kb.restore" for a in audit)

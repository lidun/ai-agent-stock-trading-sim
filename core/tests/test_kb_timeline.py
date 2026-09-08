"""知识库状态机时间线测试（spec-06 §6.7；spec-05 §3.2 证据变化可读）。

时间线数据源=audit_logs（kb.create/transition/delete/restore/stats.upsert），
按条目时序正序只读返回，不新增业务状态。
"""
from __future__ import annotations

import pytest

from core import kb

DEMO = "agent-demo-001"
SPEC = {
    "trigger_rule": "连板次日在 10:00 前炸板",
    "computation": "当日 5m 序列首小时最高价后回落 >= 3%",
    "data_sources": ["l1_minute"],
}


def _pitfall(**kw):
    base = dict(name="连板炸板回落陷阱", type_="pitfall",
                description="连板炸板回落多为情绪退潮信号",
                computable_spec=SPEC, severity="high", env_scope="all",
                source="retrospective", origin_agent=DEMO, created_by="user")
    base.update(kw)
    return base


def test_timeline_lifecycle_events(authed_client):
    st = authed_client.app.state
    ent = kb.create_entry(st, **_pitfall())
    kb.transition_kb(st, ent["id"], action="start_validation", note="闸2 通过：逻辑可证伪",
                     actor="user")
    kb.transition_kb(st, ent["id"], action="approve_valid",
                     note="统计驱动结论：n=32 E=0.35", actor="user")

    events = kb.timeline(st, ent["id"])
    actions = [e["action"] for e in events]
    assert actions == ["kb.create", "kb.transition.start_validation",
                       "kb.transition.approve_valid"]
    assert events[1]["detail"].startswith("观察中 → 验证中")
    assert events[2]["detail"].startswith("验证中 → 有效")
    assert events[0]["actor"] == "user"


def test_timeline_soft_delete_restore_and_stats(authed_client):
    st = authed_client.app.state
    ent = kb.create_entry(st, **_pitfall(name="封存留痕样例"))
    kb.upsert_stats(st, ent["id"], env_bucket="all", sample_n=18,
                    note="验证预算到期样本不足")
    kb.transition_kb(st, ent["id"], action="start_validation",
                     note="闸2 复核进入：新证据周期", actor="user")
    kb.transition_kb(st, ent["id"], action="seal", note="样本不足 30，封存留痕",
                     actor="user")
    kb.update_entry(st, ent["id"], deleted=True, note="管理软删", actor="user")
    kb.update_entry(st, ent["id"], restore=True, note="复核重新激活", actor="user")

    events = kb.timeline(st, ent["id"])
    assert [e["action"] for e in events] == [
        "kb.create", "kb.stats.upsert", "kb.transition.start_validation",
        "kb.transition.seal", "kb.delete", "kb.restore",
    ]
    assert events[-1]["detail"].startswith("复核重新激活")


def test_timeline_http_roundtrip_and_404(authed_client):
    st = authed_client.app.state
    ent = kb.create_entry(st, **_pitfall(name="HTTP 时间线样例"))
    r = authed_client.get(f"/api/kb/{ent['id']}/timeline")
    assert r.status_code == 200
    body = r.json()["events"]
    assert len(body) == 1 and body[0]["action"] == "kb.create"

    assert authed_client.get("/api/kb/KB-9999/timeline").status_code == 404


def test_timeline_unknown_kb(authed_client):
    st = authed_client.app.state
    with pytest.raises(LookupError):
        kb.timeline(st, "KB-0000")

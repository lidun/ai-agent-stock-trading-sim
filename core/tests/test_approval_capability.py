"""能力申请-下发闭环（spec-05 §2.1 / spec-04 §4.1）：审批通过才绑定，驳回不下发。"""
from __future__ import annotations

from conftest import csrf_headers

from core import seed_demo


def _skill_cap(state):
    seed_demo.seed(state)
    from core import capability_center
    items = capability_center.catalog(state)["items"]
    skill = next(c for c in items if c["type"] == "skill")
    return skill["id"], skill["name"]


def _submit(client, cap_id: str, reason: str):
    return client.post("/api/approvals", json={
        "type": "capability", "agent_id": "agent-demo-001",
        "payload": {"capability_id": cap_id}, "reason": reason,
    }, headers=csrf_headers(client)).json()


def _decide(client, aid: str, decision: str, reason: str):
    return client.patch(
        f"/api/approvals/{aid}/decision", json={
            "decision": decision, "reason": reason,
        }, headers=csrf_headers(client)).json()


def test_capability_approval_binds_on_approve(authed_client):
    cap_id, name = _skill_cap(authed_client.app.state)
    out = _submit(authed_client, cap_id, "演示：需月度复盘模板能力做归因复盘")
    assert out["ok"] is True
    assert _decide(authed_client, out["approval"]["id"],
                   "approved", "评审通过")["ok"] is True
    binds = authed_client.get(
        "/api/agents/agent-demo-001/capability-bindings").json()["items"]
    assert any(b["name"] == name and b["unbound_ts"] == "" for b in binds)


def test_capability_approval_reject_leaves_unbound(authed_client):
    cap_id, name = _skill_cap(authed_client.app.state)
    out = _submit(authed_client, cap_id, "演示：申请后驳回不下发")
    assert out["ok"] is True
    assert _decide(authed_client, out["approval"]["id"],
                   "rejected", "暂无预算")["ok"] is True
    binds = authed_client.get(
        "/api/agents/agent-demo-001/capability-bindings").json()["items"]
    assert all(b["name"] != name for b in binds)


def test_capability_approval_reapply_short_circuits_after_approved(authed_client):
    cap_id, _ = _skill_cap(authed_client.app.state)
    first = _submit(authed_client, cap_id, "演示：重复申请被确定性短路")
    aid = first["approval"]["id"]
    assert _decide(authed_client, aid, "approved", "通过")["ok"] is True
    again = _submit(authed_client, cap_id, "演示：重复申请被确定性短路")
    assert again["ok"] is False
    assert again["reason"] == "hash_hit_approved"


def test_capability_approval_unknown_capability_effect_failed(authed_client):
    out = _submit(authed_client, "cap-no-such", "演示：无效能力应在决定时 effect_failed")
    assert out["ok"] is True
    r = _decide(authed_client, out["approval"]["id"], "approved", "通过")
    assert r["ok"] is False and r["reason"] == "effect_failed"

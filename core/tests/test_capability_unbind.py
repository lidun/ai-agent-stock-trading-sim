"""管理侧能力解绑（spec-05 §2.4 / spec-06 §6.8）：unbound_ts 留痕 + 审计。"""
from __future__ import annotations

from conftest import csrf_headers

from core import seed_demo
from core.db import state_conn


def _tool_cap(state):
    seed_demo.seed(state)
    from core import capability_center
    items = capability_center.catalog(state)["items"]
    return next(c for c in items if c["type"] == "tool")["id"]


def test_unbind_flags_history_and_shrinks_active_list(authed_client):
    cap_id = _tool_cap(authed_client.app.state)
    r = authed_client.post(
        f"/api/capabilities/{cap_id}/unbind", json={"agent_id": "agent-demo-001"},
        headers=csrf_headers(authed_client))
    assert r.status_code == 200 and r.json()["ok"] is True

    binds = authed_client.get(
        "/api/agents/agent-demo-001/capability-bindings").json()["items"]
    assert len(binds) == 1  # tool 解绑后默认在绑清单仅剩 datasource

    detail = authed_client.get(f"/api/capabilities/{cap_id}").json()
    tool_row = next(b for b in detail["bindings"]
                    if b["agent_id"] == "agent-demo-001")
    assert tool_row["active"] is False and tool_row["unbound_ts"]


def test_unbind_writes_audit_log(authed_client):
    cap_id = _tool_cap(authed_client.app.state)
    authed_client.post(
        f"/api/capabilities/{cap_id}/unbind", json={"agent_id": "agent-demo-001"},
        headers=csrf_headers(authed_client))
    n = state_conn(authed_client.app.state).execute(
        "SELECT COUNT(*) n FROM audit_logs WHERE action='capability.unbind'"
    ).fetchone()["n"]
    assert n == 1


def test_unbind_unknown_capability_404(authed_client):
    seed_demo.seed(authed_client.app.state)
    r = authed_client.post(
        "/api/capabilities/cap-no-such/unbind", json={"agent_id": "agent-demo-001"},
        headers=csrf_headers(authed_client))
    assert r.status_code == 404


def test_unbind_missing_agent_400(authed_client):
    cap_id = _tool_cap(authed_client.app.state)
    r = authed_client.post(
        f"/api/capabilities/{cap_id}/unbind", json={},
        headers=csrf_headers(authed_client))
    assert r.status_code == 400

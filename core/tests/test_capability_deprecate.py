"""管理侧能力停用（spec-05 §2.2 → deprecated）：状态机 + 审计 + 新绑定闸门。"""
from __future__ import annotations

from conftest import csrf_headers

from core import capability_center, seed_demo
from core.db import state_conn


def _demo_cap(state, cap_type: str = "tool"):
    seed_demo.seed(state)
    items = capability_center.catalog(state)["items"]
    return next(c for c in items if c["type"] == cap_type)["id"]


def test_deprecate_flips_status_and_shrinks_default_catalog(authed_client):
    cap_id = _demo_cap(authed_client.app.state)
    r = authed_client.post(
        f"/api/capabilities/{cap_id}/deprecate",
        json={"reason": "沙箱环境过期"},
        headers=csrf_headers(authed_client))
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["status"] == "deprecated"

    detail = authed_client.get(f"/api/capabilities/{cap_id}").json()["capability"]
    assert detail["status"] == "deprecated"

    cats = capability_center.catalog(authed_client.app.state,
                                     status="active")["items"]
    assert all(c["id"] != cap_id for c in cats)
    only_dep = capability_center.catalog(authed_client.app.state,
                                         status="deprecated")["items"]
    assert any(c["id"] == cap_id for c in only_dep)


def test_deprecate_is_idempotent(authed_client):
    cap_id = _demo_cap(authed_client.app.state)
    state = authed_client.app.state
    r1 = capability_center.deprecate(state, capability_id=cap_id,
                                     reason="a", audit_actor="tester")
    r2 = capability_center.deprecate(state, capability_id=cap_id,
                                     reason="a", audit_actor="tester")
    assert r1["already"] is False and r2["already"] is True
    n = state_conn(state).execute(
        "SELECT COUNT(*) n FROM audit_logs WHERE action='capability.deprecate'"
    ).fetchone()["n"]
    assert n == 1  # 幂等不重复审计


def test_deprecate_writes_audit_log_with_reason(authed_client):
    cap_id = _demo_cap(authed_client.app.state)
    authed_client.post(
        f"/api/capabilities/{cap_id}/deprecate",
        json={"reason": "上游源停更"},
        headers=csrf_headers(authed_client))
    row = state_conn(authed_client.app.state).execute(
        "SELECT actor, action, result, detail FROM audit_logs"
        " WHERE action='capability.deprecate'"
    ).fetchone()
    assert row["result"] == "deprecated" and "上游源停更" in row["detail"]


def test_deprecate_blocks_new_bind_but_keeps_active_bindings(authed_client):
    state = authed_client.app.state
    cap_id = _demo_cap(state)
    seed_demo.seed(state)
    binds_before = capability_center.detail(state, cap_id)["bindings"]
    active_before = sum(1 for b in binds_before if b["active"])
    assert active_before >= 1

    capability_center.deprecate(state, capability_id=cap_id,
                                reason="maint", audit_actor="tester")
    try:
        capability_center.bind(state, capability_id=cap_id,
                               agent_id="agent-demo-001", bound_by="agent-manager")
        raise AssertionError("deprecated 能力不应允许新下发")
    except ValueError:
        pass

    binds_after = capability_center.detail(state, cap_id)["bindings"]
    active_after = sum(1 for b in binds_after if b["active"])
    assert active_after == active_before  # 存量在绑保留


def test_deprecate_unknown_capability_404(authed_client):
    seed_demo.seed(authed_client.app.state)
    r = authed_client.post(
        "/api/capabilities/cap-no-such/deprecate",
        json={"reason": "x"},
        headers=csrf_headers(authed_client))
    assert r.status_code == 404

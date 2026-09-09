"""seed_demo 幂等性：重复运行不得产生重复演示行。"""
from __future__ import annotations

from core import seed_demo


def test_seed_demo_idempotent(authed_client):
    state = authed_client.app.state
    first = seed_demo.seed(state)
    second = seed_demo.seed(state)
    assert first == second
    assert first["charter_versions"] == 2
    assert first["strategy_versions"] == 2
    assert first["memory"] == 5  # 2 条人工演进 + EVOQUANT v1 checkpoint/activate + v2 checkpoint
    assert first["capabilities"] == 3
    assert first["bindings"] == 2
    assert first["kb"] == 2


def test_seed_demo_surfaces_on_pages(authed_client):
    seed_demo.seed(authed_client.app.state)
    agent = "agent-demo-001"
    assert authed_client.get(
        f"/api/agents/{agent}/strategy-profile").status_code == 200
    assert authed_client.get(
        f"/api/agents/{agent}/strategy-memory").status_code == 200
    assert authed_client.get(
        f"/api/agents/{agent}/capability-bindings").status_code == 200
    assert authed_client.get("/api/capabilities").json()["total"] >= 3
    names = {e["name"] for e in authed_client.get("/api/kb").json()["entries"]}
    assert {"尾盘缩量走弱陷阱", "红利因子股息率筛选"} <= names

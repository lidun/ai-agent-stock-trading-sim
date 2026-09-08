"""演进记忆测试（spec-02 §3.1 type=strategy 子集：只读 + 幂等 append 壳）。"""
from __future__ import annotations

from core import strategy_memory

DEMO = "agent-demo-001"


def test_list_and_version_filter(authed_client):
    st = authed_client.app.state
    strategy_memory._append(
        st, DEMO, version_no="v0.1.0",
        body="诊断：卖出信号胜率低于基线，止损宽度偏窄。",
        source="opt:001", dedup_key="opt:001", ts="2026-09-01T10:00:00Z")
    strategy_memory._append(
        st, DEMO, version_no="v0.2.0",
        body="受控修改：止损宽度 2%→3%；依据=诊断证据；预期=降低卖早占比。",
        source="opt:002", dedup_key="opt:002", ts="2026-09-03T10:00:00Z")
    # 幂等：同 dedup_key 重复写入不重复
    strategy_memory._append(
        st, DEMO, version_no="v0.1.0", body="重复投递测试。",
        source="opt:001", dedup_key="opt:001", ts="2026-09-04T10:00:00Z")

    r = strategy_memory.list_strategy_memory(st, DEMO)
    assert r["total"] == 2
    assert r["versions"] == ["v0.1.0", "v0.2.0"]
    assert r["items"][0]["version_no"] == "v0.2.0"          # ts 降序
    assert r["items"][0]["mem_type"] == "strategy"

    only_v1 = strategy_memory.list_strategy_memory(st, DEMO, version_no="v0.1.0")
    assert only_v1["total"] == 1 and only_v1["items"][0]["dedup_key"] == "opt:001"

    capped = strategy_memory.list_strategy_memory(st, DEMO, limit=1)
    assert capped["total"] == 1


def test_http_roundtrip(authed_client):
    strategy_memory._append(
        authed_client.app.state, DEMO, version_no="v0.1.0",
        body="HTTP 读取演进记忆。", source="opt:http", dedup_key="opt:http")
    resp = authed_client.get(f"/api/agents/{DEMO}/strategy-memory")
    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["body"] == "HTTP 读取演进记忆。"

    assert authed_client.get(
        "/api/agents/nope/strategy-memory").status_code == 404
    assert authed_client.get(
        f"/api/agents/{DEMO}/strategy-memory?limit=9999").status_code == 422

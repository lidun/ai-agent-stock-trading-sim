"""策略章程只读测试（spec-06 §6.4 理念区块，方案 A：只读 + 空态）。"""
from __future__ import annotations

from core import strategy_profile

DEMO = "agent-demo-001"


def test_profile_empty_when_no_charter(authed_client):
    st = authed_client.app.state
    p = strategy_profile.profile(st, DEMO)
    assert p["agent_id"] == DEMO
    assert p["active"] is None
    assert p["versions"] == []
    assert p["has_capability_packs"] is False


def test_profile_active_and_versions(authed_client):
    st = authed_client.app.state
    strategy_profile.write_seed(
        st, DEMO, version_no="v0.1.0",
        core_belief="低波红利演示：理念由作者方落库。",
        layers={"execution": {"field_whitelist": ["rebalance_interval"]}},
        note="演示 seed",
    )
    strategy_profile.write_seed(
        st, DEMO, version_no="v0.2.0",
        core_belief="低波红利演示：理念变更需用户授权（第二版）。",
        layers={"execution": {"field_whitelist": ["rebalance_interval", "stop_loss"]}},
        locked=True, note="受控修改",
    )

    p = strategy_profile.profile(st, DEMO)
    assert p["active"]["version_no"] == "v0.2.0"          # 新版本晋升 active
    assert p["active"]["locked"] is True
    assert p["active"]["layers"]["execution"]["field_whitelist"] == \
        ["rebalance_interval", "stop_loss"]
    assert [v["version_no"] for v in p["versions"]] == ["v0.1.0", "v0.2.0"]
    # 摘要不带正文，仅 active 全量返回
    assert "core_belief" not in p["versions"][0]


def test_version_detail_roundtrip(authed_client):
    st = authed_client.app.state
    strategy_profile.write_seed(
        st, DEMO, version_no="v0.1.0",
        core_belief="第一版理念。",
        layers={"scope": {"sectors": ["红利"]}},
    )
    d = strategy_profile.version_detail(st, DEMO, "v0.1.0")
    assert d["agent_id"] == DEMO
    assert d["version"]["core_belief"] == "第一版理念。"
    assert d["version"]["layers"]["scope"]["sectors"] == ["红利"]

    try:
        strategy_profile.version_detail(st, DEMO, "v9.9.9")
        raise AssertionError("应抛 LookupError")
    except LookupError:
        pass


def test_http_roundtrip(authed_client):
    strategy_profile.write_seed(
        authed_client.app.state, DEMO, version_no="v0.1.0",
        core_belief="HTTP 读取理念。",
    )
    r = authed_client.get(f"/api/agents/{DEMO}/strategy-profile")
    assert r.status_code == 200, r.text
    assert r.json()["active"]["core_belief"] == "HTTP 读取理念。"

    r2 = authed_client.get(f"/api/agents/{DEMO}/strategy-profile/versions/v0.1.0")
    assert r2.status_code == 200 and r2.json()["version"]["version_no"] == "v0.1.0"

    assert authed_client.get("/api/agents/nope/strategy-profile").status_code == 404
    assert authed_client.get(
        f"/api/agents/{DEMO}/strategy-profile/versions/v9.9.9").status_code == 404

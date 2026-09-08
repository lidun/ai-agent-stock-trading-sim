"""能力配置中心统一域测试（spec-05 §2.1/§2.4：注册四门槛 / 绑定解绑留痕 / 目录与路由）。"""
from __future__ import annotations

import pytest

from core import capability_center, strategy_profile

MANAGER = "agent-manager"
DEMO = "agent-demo-001"


def _cap(state, **kw):
    base = dict(name="移动均线过滤器", capability_type="tool", version="v1.0.0",
                description="MA 趋势过滤（只读计算）。",
                source_type="selfmade", source_ref="作者:agent-manager",
                maintainer=MANAGER)
    base.update(kw)
    return capability_center.register(state, **base)


def test_register_gate_and_uniqueness(authed_client):
    st = authed_client.app.state
    cap = _cap(st)
    assert cap["id"] and cap["status"] == "active"
    assert cap["sandbox_status"] == "passed"
    assert cap["type"] == "tool" and cap["source_ref"]

    with pytest.raises(ValueError):  # 门槛① 描述缺失
        _cap(st, name="无描述能力", description="")
    with pytest.raises(ValueError):  # 门槛② 沙箱未过
        _cap(st, name="未沙箱能力", sandbox_status="pending")
    with pytest.raises(ValueError):  # 门槛③ 来源未留痕
        _cap(st, name="无来源能力", source_ref="")
    with pytest.raises(ValueError):  # 类型非法
        _cap(st, name="坏类型", capability_type="shell")
    with pytest.raises(ValueError):  # (name, version) 幂等冲突
        _cap(st)


def test_bind_unbind_history_and_gates(authed_client):
    st = authed_client.app.state
    cap = _cap(st, name="网格补仓工具")
    cap2 = _cap(st, name="信号邮件通知", capability_type="skill")

    capability_center.bind(st, capability_id=cap["id"], agent_id=DEMO)
    capability_center.bind(st, capability_id=cap2["id"], agent_id=DEMO)

    d = capability_center.detail(st, cap["id"])
    assert d["capability"]["active_bindings"] == 1
    assert d["bindings"][0]["active"] is True
    assert d["bindings"][0]["bound_by"] == MANAGER

    # 幂等：重复绑同版本不新增
    capability_center.bind(st, capability_id=cap["id"], agent_id=DEMO)
    assert capability_center.agent_bindings(st, DEMO)["total"] == 2

    # 解绑留痕：默认仅 active，include_unbound 可见历史
    capability_center.unbind(st, capability_id=cap["id"], agent_id=DEMO)
    assert capability_center.agent_bindings(st, DEMO)["total"] == 1
    hist = capability_center.agent_bindings(st, DEMO, include_unbound=True)
    assert hist["total"] == 2
    unbound_item = next(i for i in hist["items"] if i["name"] == "网格补仓工具")
    assert unbound_item["unbound_ts"] != ""

    # 废弃能力不可再下发（存量绑定保留、不阻断读取）
    dep_row = capability_center.register(
        st, name="量化研报拉取", version="v0.9.0", description="旧版，待迁移。",
        capability_type="datasource", source_type="opensource", source_ref="repo:x@mit",
        status="deprecated", maintainer=MANAGER)
    with pytest.raises(ValueError):
        capability_center.bind(st, capability_id=dep_row["id"], agent_id=DEMO)


def test_catalog_filters(authed_client):
    st = authed_client.app.state
    _cap(st, name="布林带收窄探测", capability_type="tool", version="v1.0.0")
    _cap(st, name="行业新闻异动源", capability_type="datasource", version="v1.0.0")
    _cap(st, name="研报结构化解析", capability_type="skill", version="v2.0.0",
         status="deprecated")

    assert capability_center.catalog(st)["total"] == 3
    only_tool = capability_center.catalog(st, capability_type="tool")
    assert only_tool["total"] == 1 and only_tool["items"][0]["name"] == "布林带收窄探测"
    active = capability_center.catalog(st, status="active")
    assert active["total"] == 2
    kw = capability_center.catalog(st, keyword="新闻")
    assert kw["total"] == 1 and kw["items"][0]["type"] == "datasource"


def test_agent_require(authed_client):
    st = authed_client.app.state
    with pytest.raises(LookupError):
        capability_center.agent_bindings(st, "ghost-agent")
    with pytest.raises(LookupError):
        capability_center.detail(st, "cap-does-not-exist")


def test_http_readonly_roundtrip(authed_client):
    st = authed_client.app.state
    cap = _cap(st, name="T+0 试算助手")
    capability_center.bind(st, capability_id=cap["id"], agent_id=DEMO)

    r = authed_client.get("/api/capabilities")
    assert r.status_code == 200
    assert r.json()["total"] == 1 and r.json()["items"][0]["active_bindings"] == 1

    r = authed_client.get(f"/api/capabilities/{cap['id']}")
    assert r.status_code == 200 and r.json()["bindings"][0]["agent_id"] == DEMO

    r = authed_client.get(f"/api/agents/{DEMO}/capability-bindings")
    assert r.status_code == 200 and r.json()["total"] == 1

    assert authed_client.get("/api/capabilities?type=shell").status_code == 400
    assert authed_client.get("/api/capabilities/nope").status_code == 404
    assert authed_client.get("/api/agents/nope/capability-bindings").status_code == 404


def test_profile_exposes_binding_after_bind(authed_client):
    st = authed_client.app.state
    cap = _cap(st, name="回撤风控哨兵", capability_type="tool")
    p = strategy_profile.profile(st, DEMO)
    assert p["has_capability_packs"] is False and p["capability_packs"] == []

    capability_center.bind(st, capability_id=cap["id"], agent_id=DEMO)
    p = strategy_profile.profile(st, DEMO)
    assert p["has_capability_packs"] is True
    assert p["capability_packs"][0]["name"] == "回撤风控哨兵"
    assert p["capability_packs"][0]["version"] == "v1.0.0"

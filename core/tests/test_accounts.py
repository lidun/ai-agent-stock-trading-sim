"""账户域测试（spec-01 §2.1/§6.1：accounts 种子与只读视图契约）。"""
from __future__ import annotations

DEMO = "agent-demo-001"
MANAGER = "agent-manager"


def test_accounts_seeded_for_strategy_only(authed_client):
    r = authed_client.get("/api/accounts")
    assert r.status_code == 200, r.text
    accs = {a["id"]: a for a in r.json()["accounts"]}
    # 策略子 Agent 拥有 1:1 模拟账户
    assert DEMO in accs
    a = accs[DEMO]
    assert a["agent_name"] == "低波红利 · 演示子 Agent"
    assert a["agent_role"] == "strategy"
    assert a["agent_status"] == "running"
    # running → 账户 normal；金额与份额法种子（初始 10 万、nav=1）
    assert a["status"] == "normal"
    assert a["initial_capital"] == "100000.00"
    assert a["cash"] == "100000.00"
    assert a["shares"] == "100000.00"
    assert a["nav"] == "1.0000"
    assert a["total_pnl"] == "0.00"
    assert a["today_pnl"] == "0.00"
    assert a["granularity"] == "eod_replay"
    assert a["settle_key"] == ""
    # 管理 Agent 非交易账户，不进入 accounts
    assert MANAGER not in accs


def test_account_list_requires_auth(client):
    r = client.get("/api/accounts")
    assert r.status_code == 401


def test_account_detail(authed_client):
    r = authed_client.get(f"/api/accounts/{DEMO}")
    assert r.status_code == 200, r.text
    assert r.json()["id"] == DEMO

    r404 = authed_client.get(f"/api/accounts/{MANAGER}")   # manager 无账户
    assert r404.status_code == 404
    r404b = authed_client.get("/api/accounts/nope")
    assert r404b.status_code == 404


def test_account_list_matches_agents(authed_client):
    """账户仅覆盖 strategy 角色 Agent（账户归属不变式）。"""
    r = authed_client.get("/api/agents")
    strategy_ids = {a["id"] for a in r.json()["agents"] if a["role"] == "strategy"}
    ra = authed_client.get("/api/accounts")
    acct_ids = {a["id"] for a in ra.json()["accounts"]}
    assert strategy_ids == acct_ids


def test_trade_storage_empty_views(authed_client):
    """引擎存储层基座：持仓/条件单表就位但引擎尚未写入，返回空态。"""
    h = authed_client.get(f"/api/accounts/{DEMO}/holdings")
    assert h.status_code == 200, h.text
    assert h.json()["account_id"] == DEMO
    assert h.json()["holdings"] == []

    co = authed_client.get(f"/api/accounts/{DEMO}/condition-orders")
    assert co.status_code == 200, co.text
    assert co.json()["account_id"] == DEMO
    assert co.json()["condition_orders"] == []


def test_trade_storage_requires_auth(client):
    """持仓/条件单视图未登录一律 401。"""
    for ep in ("holdings", "condition-orders"):
        assert client.get(f"/api/accounts/{DEMO}/{ep}").status_code == 401


def test_trade_storage_scopes_and_auth(client, authed_client):
    """视图鉴权 + 归属不变式：非策略 Agent/不存在账户一律 404。"""
    assert authed_client.get(f"/api/accounts/{DEMO}/holdings").status_code == 200
    for ep in ("holdings", "condition-orders"):
        assert authed_client.get(f"/api/accounts/{MANAGER}/{ep}").status_code == 404
        assert authed_client.get(f"/api/accounts/nope/{ep}").status_code == 404

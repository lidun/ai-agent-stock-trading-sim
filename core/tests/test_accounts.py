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

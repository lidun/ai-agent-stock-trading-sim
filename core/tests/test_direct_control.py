"""用户直控测试（spec-06 §6.3 → spec-01 直控接口）：冻结买入保留卖出/熔断全停/紧急清仓。"""
from __future__ import annotations

import pytest

from core import orderstore
from core.db import state_conn
from conftest import csrf_headers

DEMO = "agent-demo-001"
MANAGER = "agent-manager"


def _main_row(st, agent_id=DEMO):
    return state_conn(st).execute(
        "SELECT id, status FROM accounts WHERE agent_id=? AND role='main'",
        (agent_id,)).fetchone()


def test_control_pause_resume_and_gate(authed_client):
    st = authed_client.app.state
    # 冻结买入：Agent 保持 running、主账户 paused_buy；卖出仍放行
    r = authed_client.patch(f"/api/agents/{DEMO}/control",
                            json={"op": "pause_buy"}, headers=csrf_headers(authed_client))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["from"] == "normal" and body["to"] == "paused_buy"
    assert body["account"]["agent_status"] == "running"
    with pytest.raises(orderstore.OrderError):
        orderstore.place_order(st, account_id=DEMO, creator=DEMO, symbol="600519",
                               qty=100, price_type="market", direction="buy",
                               reason="冻结期买入应被拒")
    sell = orderstore.place_order(st, account_id=DEMO, creator=DEMO, symbol="600519",
                                  qty=100, price_type="market", direction="sell",
                                  reason="冻结期卖出保留")
    assert sell["direction"] == "sell" and sell["status"] == "active"
    # 恢复 → 全方向可下单
    resume = authed_client.patch(f"/api/agents/{DEMO}/control",
                                 json={"op": "resume"}, headers=csrf_headers(authed_client))
    assert resume.status_code == 200 and resume.json()["to"] == "normal"
    ok = orderstore.place_order(st, account_id=DEMO, creator=DEMO, symbol="600519",
                                qty=100, price_type="market", direction="buy",
                                reason="恢复后买入")
    assert ok["status"] == "active"
    # 熔断冻结：买卖全停（卖出亦拦截）
    hal = authed_client.patch(f"/api/agents/{DEMO}/control", json={"op": "halt"}, headers=csrf_headers(authed_client))
    assert hal.status_code == 200 and hal.json()["to"] == "halted"
    with pytest.raises(orderstore.OrderError):
        orderstore.place_order(st, account_id=DEMO, creator=DEMO, symbol="600519",
                               qty=100, price_type="market", direction="sell",
                               reason="熔断期卖出应被拒")
    # 重复直控幂等拒绝
    dup = authed_client.patch(f"/api/agents/{DEMO}/control", json={"op": "halt"}, headers=csrf_headers(authed_client))
    assert dup.status_code == 409 and "无变更" in dup.json()["detail"]
    # 收尾恢复
    back = authed_client.patch(f"/api/agents/{DEMO}/control", json={"op": "resume"}, headers=csrf_headers(authed_client))
    assert back.status_code == 200 and back.json()["to"] == "normal"


def test_emergency_sell_all_graceful_and_blocked(authed_client):
    st = authed_client.app.state
    ok = authed_client.post(f"/api/agents/{DEMO}/control/sell-all", headers=csrf_headers(authed_client))
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["account_id"] == DEMO and body["holdings"] == 0
    assert body["orders"] == [] and body["blocked_halted"] is False
    # 熔断冻结期间清仓：卖出同样被闸门拦截（blocked_halted=True）
    authed_client.patch(f"/api/agents/{DEMO}/control", json={"op": "halt"}, headers=csrf_headers(authed_client))
    blocked = authed_client.post(f"/api/agents/{DEMO}/control/sell-all", headers=csrf_headers(authed_client)).json()
    assert blocked["blocked_halted"] is True
    authed_client.patch(f"/api/agents/{DEMO}/control", json={"op": "resume"}, headers=csrf_headers(authed_client))


def test_control_guards(authed_client):
    # 管理 Agent 无交易账户
    mgr = authed_client.patch(f"/api/agents/{MANAGER}/control",
                              json={"op": "pause_buy"}, headers=csrf_headers(authed_client))
    assert mgr.status_code == 409
    # 未知 Agent / 非法 op
    miss = authed_client.patch("/api/agents/agent-nope/control",
                               json={"op": "pause_buy"}, headers=csrf_headers(authed_client))
    assert miss.status_code == 409
    bad = authed_client.patch(f"/api/agents/{DEMO}/control", json={"op": "wipe"}, headers=csrf_headers(authed_client))
    assert bad.status_code == 422
    # 紧急清仓守卫
    assert authed_client.post("/api/agents/agent-nope/control/sell-all", headers=csrf_headers(authed_client)).status_code == 409
    assert authed_client.post(f"/api/agents/{MANAGER}/control/sell-all", headers=csrf_headers(authed_client)).status_code == 409

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


def test_freeze_security_gate_and_unfreeze(authed_client):
    """冻结证券：取消该票 active 买入单、新买入拦截、卖出保留；解除恢复可买。"""
    st = authed_client.app.state
    h = csrf_headers(authed_client)
    # 冻结前挂一笔该票买入单
    pre = orderstore.place_order(st, account_id=DEMO, creator=DEMO, symbol="600519",
                                 qty=100, price_type="market", direction="buy",
                                 reason="冻结前挂单")
    assert pre["status"] == "active"
    # 冻结：即时生效并取消挂单
    fr = authed_client.post(f"/api/agents/{DEMO}/frozen",
                            json={"symbol": "600519", "reason": "停牌风险冻结"},
                            headers=h)
    assert fr.status_code == 200, fr.text
    assert fr.json()["cancelled_buy_orders"] == 1
    conn = state_conn(st)
    assert conn.execute(
        "SELECT status FROM condition_orders WHERE id=?", (pre["id"],)
    ).fetchone()["status"] == "cancelled"
    # 新买入被拦截；卖出保留
    with pytest.raises(orderstore.OrderError):
        orderstore.place_order(st, account_id=DEMO, creator=DEMO, symbol="600519",
                               qty=100, price_type="market", direction="buy",
                               reason="冻结期买入应被拒")
    sell = orderstore.place_order(st, account_id=DEMO, creator=DEMO, symbol="600519",
                                  qty=100, price_type="market", direction="sell",
                                  reason="冻结期卖出保留")
    assert sell["direction"] == "sell"
    # 清单常驻可见；重复冻结幂等拒绝
    listed = authed_client.get(f"/api/frozen?agent_id={DEMO}").json()["frozen"]
    assert [f["symbol"] for f in listed] == ["600519"]
    dup = authed_client.post(f"/api/agents/{DEMO}/frozen",
                             json={"symbol": "600519"}, headers=h)
    assert dup.status_code == 409 and "已处于冻结" in dup.json()["detail"]
    # 解除 → 恢复可买
    un = authed_client.delete(f"/api/agents/{DEMO}/frozen/600519", headers=h)
    assert un.status_code == 200 and un.json()["removed"] == 1
    ok = orderstore.place_order(st, account_id=DEMO, creator=DEMO, symbol="600519",
                                qty=100, price_type="market", direction="buy",
                                reason="解除后恢复买入")
    assert ok["status"] == "active"
    # 非策略 Agent 冻结拒绝
    mgr = authed_client.post(f"/api/agents/{MANAGER}/frozen",
                             json={"symbol": "600519"}, headers=h)
    assert mgr.status_code == 409


def test_control_notify_in_conversation(authed_client):
    """直控干预自动通知子 Agent：消息出现在对话面板（未读角标联动）。"""
    h = csrf_headers(authed_client)
    fr = authed_client.post(f"/api/agents/{DEMO}/frozen",
                            json={"symbol": "600519", "reason": "通知验证"},
                            headers=h)
    assert fr.status_code == 200
    un = authed_client.delete(f"/api/agents/{DEMO}/frozen/600519", headers=h)
    assert un.status_code == 200
    convs = authed_client.get("/api/conversations").json()["conversations"]
    conv = next(c for c in convs if c["agent_id"] == DEMO and c["conv_type"] == "user_chat")
    assert conv["unread"] == 2
    assert conv["last_message"]["msg_type"] == "control"
    assert conv["last_message"]["body"].startswith("## 直控干预")
    # 读消息面板可拉取两条 control 回执
    msgs = authed_client.get(
        f"/api/conversations/{conv['id']}/messages").json()["messages"]
    control_msgs = [m for m in msgs if m["msg_type"] == "control"]
    assert len(control_msgs) == 2
    bodies = {m["body"] for m in control_msgs}
    assert any(b.startswith("## 直控干预 · 冻结证券 600519") for b in bodies)
    assert any("已恢复买入" in b for b in bodies)


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

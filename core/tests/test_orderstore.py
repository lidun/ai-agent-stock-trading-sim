"""条件单创建域测试（spec-01 §2.3：orderstore 前置校验 + 桩 Agent 演示下单链路）。"""
from __future__ import annotations

import pytest

from core import orderstore

MANAGER = "agent-manager"
DEMO = "agent-demo-001"


def test_place_order_valid_and_listable(authed_client):
    st = authed_client.app.state
    order = orderstore.place_order(
        st, account_id=DEMO, creator=DEMO, symbol="600519", qty=100,
        price_type="market", reason="单测登记",
    )
    assert order["id"].startswith("co") and order["status"] == "active"
    r = authed_client.get(f"/api/accounts/{DEMO}/condition-orders")
    assert r.status_code == 200, r.text
    rows = [o for o in r.json()["condition_orders"] if o["id"] == order["id"]]
    assert len(rows) == 1
    assert rows[0]["symbol"] == "600519"
    assert rows[0]["creator"] == DEMO
    assert rows[0]["basis"] == "replay_l0" and rows[0]["validity"] == "today"


def test_place_order_qty_rule(authed_client):
    st = authed_client.app.state
    with pytest.raises(orderstore.OrderError):
        orderstore.place_order(st, account_id=DEMO, creator=DEMO, symbol="600519",
                               qty=150, price_type="market")
    # 科创板规则：≥200 任意整数
    ok = orderstore.place_order(st, account_id=DEMO, creator=DEMO, symbol="688001",
                                qty=201, price_type="market")
    assert ok["symbol"] == "688001"


def test_place_order_rejects_manager(authed_client):
    st = authed_client.app.state
    with pytest.raises(orderstore.OrderError) as exc:
        orderstore.place_order(st, account_id=MANAGER, creator=MANAGER,
                               symbol="600519", qty=100, price_type="market")
    assert "管理 Agent" in str(exc.value) or "仅策略 Agent" in str(exc.value)


def test_place_order_limit_requires_trigger(authed_client):
    st = authed_client.app.state
    with pytest.raises(orderstore.OrderError):
        orderstore.place_order(st, account_id=DEMO, creator=DEMO, symbol="600519",
                               qty=100, price_type="limit")
    order = orderstore.place_order(st, account_id=DEMO, creator=DEMO, symbol="600519",
                                   qty=100, trigger={"op": "le", "price": 1500.0},
                                   price_type="limit")
    assert order["status"] == "active"


def test_demo_order_via_chat_e2e(authed_client):
    """策略子 Agent 会话消息含「演示下单」→ 登记市价买入单并在回复中回执。"""
    from test_conv import _send, _wait_reply  # noqa: PLC0415
    from conftest import csrf_headers  # noqa: PLC0415
    conv_id = authed_client.post("/api/conversations", json={"agent_id": DEMO},
                                 headers=csrf_headers(authed_client)).json()["id"]
    msg = _send(authed_client, conv_id, "演示下单 帮我挂一张明日市价单")
    reply = _wait_reply(authed_client, conv_id, msg["id"])
    assert "P1 桩 · 演示下单已登记" in reply["body"]
    r = authed_client.get(f"/api/accounts/{DEMO}/condition-orders")
    orders = [o for o in r.json()["condition_orders"] if o["creator"] == DEMO]
    assert len(orders) == 1
    assert orders[0]["symbol"] == "600519"
    assert int(float(orders[0]["qty"])) == 100 and orders[0]["status"] == "active"
    assert orders[0]["id"] in reply["body"]


def test_trigger_kind_canonical_persisted(authed_client):
    """spec-01 §2.4 canonical kind 接受且落库统一 kind；legacy op 兼容并规范化存储。"""
    import json as _json

    from core.db import state_conn  # noqa: PLC0415
    st = authed_client.app.state
    o1 = orderstore.place_order(st, account_id=DEMO, creator=DEMO, symbol="600519",
                                qty=100, trigger={"kind": "price_le", "price": 1500.0},
                                price_type="limit")
    o2 = orderstore.place_order(st, account_id=DEMO, creator=DEMO, symbol="600519",
                                qty=100, trigger={"op": "ge", "price": 1600.0},
                                price_type="limit")
    conn = state_conn(st)
    rows = {r["id"]: r["trigger"] for r in conn.execute(
        "SELECT id, trigger FROM condition_orders WHERE id IN (?, ?)", (o1["id"], o2["id"]))}
    assert _json.loads(rows[o1["id"]]) == {"kind": "price_le", "price": 1500.0}
    assert _json.loads(rows[o2["id"]]) == {"kind": "price_ge", "price": 1600.0}


def test_trigger_unimplemented_kind_rejected(authed_client):
    """未实现 kind（trail/pct_chg 等）与非法 op 均在 orderstore 前置显式拒单。"""
    st = authed_client.app.state
    for bad in ({"kind": "trail", "drop_pct": 3},
                {"kind": "pct_chg", "pct": 2},
                {"op": "gt", "price": 10.0}):
        with pytest.raises(orderstore.OrderError):
            orderstore.place_order(st, account_id=DEMO, creator=DEMO, symbol="600519",
                                   qty=100, trigger=bad, price_type="limit")

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


def test_trail_order_place_and_canonical(authed_client):
    """sell_trail（kind=trail）可下单并规范落库；越界组合在 orderstore 前置拒单。"""
    import json as _json

    from core.db import state_conn  # noqa: PLC0415
    st = authed_client.app.state
    o = orderstore.place_order(st, account_id=DEMO, creator=DEMO, order_type="sell_trail",
                               direction="sell", symbol="600519", qty=100,
                               trigger={"kind": "trail", "drop_pct": 3}, price_type="market")
    assert o["status"] == "active" and o["order_type"] == "sell_trail"
    trig = _json.loads(state_conn(st).execute(
        "SELECT trigger FROM condition_orders WHERE id=?", (o["id"],)).fetchone()["trigger"])
    assert trig == {"kind": "trail", "drop_pct": 3.0}
    # 组合越界：trail 仅限 sell_trail + market；drop_pct 须 > 0
    for kwargs in ({"order_type": "buy", "direction": "buy",
                    "trigger": {"kind": "trail", "drop_pct": 3}},
                   {"order_type": "sell_trail", "direction": "sell", "price_type": "limit",
                    "trigger": {"kind": "trail", "drop_pct": 3}},
                   {"order_type": "sell_trail", "direction": "sell",
                    "trigger": {"kind": "trail"}},
                   {"order_type": "sell_trail", "direction": "sell",
                    "trigger": {"kind": "trail", "drop_pct": 0}}):
        with pytest.raises(orderstore.OrderError):
            orderstore.place_order(st, account_id=DEMO, creator=DEMO, symbol="600519",
                                   qty=100, **kwargs)


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
    """未实现 kind 与非法 trigger 均在 orderstore 前置显式拒单。"""
    st = authed_client.app.state
    for bad in ({"kind": "trail", "drop_pct": 3},
                {"kind": "pct_chg", "pct": 2},            # 缺 op
                {"kind": "open_board"},
                {"op": "gt", "price": 10.0}):
        with pytest.raises(orderstore.OrderError):
            orderstore.place_order(st, account_id=DEMO, creator=DEMO, symbol="600519",
                                   qty=100, trigger=bad, price_type="limit")


def test_pct_vscost_trigger_canonical_and_rejects(authed_client):
    """pct_chg/vs_cost 下单规范化落库；op/pct/not_limit 越界在 orderstore 前置拒单。"""
    import json as _json

    from core.db import state_conn  # noqa: PLC0415
    st = authed_client.app.state
    o1 = orderstore.place_order(st, account_id=DEMO, creator=DEMO, order_type="sell_stop",
                                direction="sell", symbol="600519", qty=100,
                                trigger={"kind": "pct_chg", "op": "le", "pct": -5,
                                         "not_limit": True},
                                price_type="market")
    o2 = orderstore.place_order(st, account_id=DEMO, creator=DEMO, order_type="sell_take_profit",
                                direction="sell", symbol="600519", qty=100,
                                trigger={"kind": "vs_cost", "op": "ge", "pct": 8},
                                price_type="market")
    conn = state_conn(st)
    rows = {r["id"]: r["trigger"] for r in conn.execute(
        "SELECT id, trigger FROM condition_orders WHERE id IN (?, ?)", (o1["id"], o2["id"]))}
    assert _json.loads(rows[o1["id"]]) == {"kind": "pct_chg", "op": "le", "pct": -5.0,
                                           "not_limit": True}
    assert _json.loads(rows[o2["id"]]) == {"kind": "vs_cost", "op": "ge", "pct": 8.0}
    for bad in ({"kind": "pct_chg", "op": "gt", "pct": -5},
                {"kind": "pct_chg", "op": "le"},
                {"kind": "pct_chg", "op": "le", "pct": 0},
                {"kind": "pct_chg", "op": "le", "pct": -5, "not_limit": "yes"},
                {"kind": "vs_cost", "op": "le"},
                {"kind": "vs_cost", "op": "le", "pct": 0}):
        with pytest.raises(orderstore.OrderError):
            orderstore.place_order(st, account_id=DEMO, creator=DEMO, symbol="600519",
                                   qty=100, trigger=bad, price_type="market")


def test_time_order_canonical_and_rejects(authed_client):
    """time 定时单规范化与非法 at/price_type 拒单。"""
    st = authed_client.app.state
    from core.orderstore import OrderError, place_order
    r = place_order(st, account_id=DEMO, creator="agent-demo-001",
                    order_type="time", direction="buy", symbol="600000", qty=100,
                    trigger={"kind": "time", "at": "14:50"}, price_type="market")
    assert r["order_type"] == "time" and r["status"] == "active"
    with pytest.raises(OrderError, match="HH:MM"):
        place_order(st, account_id=DEMO, creator="agent-demo-001",
                    order_type="time", direction="buy", symbol="600000", qty=100,
                    trigger={"kind": "time", "at": "4:50pm"})
    with pytest.raises(OrderError, match="price_type=market"):
        place_order(st, account_id=DEMO, creator="agent-demo-001",
                    order_type="time", direction="buy", symbol="600000", qty=100,
                    trigger={"kind": "time", "at": "14:50"}, price_type="limit")

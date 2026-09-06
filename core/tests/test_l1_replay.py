"""L1 档引擎判定测试（spec-01 §3.3 相邻分钟确认子集，close/OHLC 与收盘分钟口径）。"""
from __future__ import annotations

import json

import pytest

from core import eodengine
from core.db import state_conn, write_txn

DEMO = "agent-demo-001"


def _insert_order(state, *, order_id, order_type, qty, trigger, price_type="limit",
                  created="2026-09-07T09:00:00"):
    conn = state_conn(state)
    with write_txn(conn) as c:
        c.execute(
            """
            INSERT INTO condition_orders(id, account_id, order_type, direction, scope, symbol,
                trigger, basis, price_ref, qty, price_type, validity, priority, status,
                created_at, creator, reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                order_id, DEMO, order_type, "buy" if order_type == "buy" else "sell",
                "single", "600000", json.dumps(trigger), "replay_l0", "absolute", qty,
                price_type, "today", 0, "active", created, "agent-demo-001", "",
            ),
        )


def _fetch(state, sql, args=()):
    conn = state_conn(state)
    with conn:
        return conn.execute(sql, args).fetchall()


def _cash(state) -> float:
    return _fetch(state, "SELECT cash FROM accounts WHERE id=?", (DEMO,))[0]["cash"]


def test_l1_buy_fills_at_trigger_price_not_sample(authed_client):
    st = authed_client.app.state
    _insert_order(st, order_id="l1-buy", order_type="buy", qty=100,
                  trigger={"op": "le", "price": 10.05})
    r = eodengine.settle_account(
        st, DEMO, "2026-09-07",
        l1_map={"600000": [
            ("2026-09-07T09:59:00", 10.30),     # 未触达
            ("2026-09-07T10:00:00", 9.90),      # 相邻分钟确认触达（跨 10.05）
        ]},
        close_map={"600000": 9.90},
    )
    assert r["already_settled"] is False
    tr = _fetch(st, "SELECT * FROM trades WHERE order_id='l1-buy'")[0]
    assert tr["price"] == 10.05                    # L1 按条件价成交（非采样 9.90）
    assert tr["basis_used"] == "l1" and tr["basis_requested"] == "replay_l0"
    assert tr["quality"] == ""                     # 序列末价 9.90 == 官方收盘 9.90
    assert tr["amount"] == 1005.0
    assert abs(_cash(st) - 98989.99) < 0.02
    lot = _fetch(st, "SELECT remaining FROM lots WHERE account_id=?", (DEMO,))[0]
    assert lot["remaining"] == 100
    sl = _fetch(st, "SELECT granularity_used FROM settlement_log WHERE account_id=?", (DEMO,))[0]
    assert json.loads(sl["granularity_used"]) == {"600000": "l1"}


def test_l1_close_minute_conservative(authed_client):
    st = authed_client.app.state
    _insert_order(st, order_id="l1-cm", order_type="buy", qty=100,
                  trigger={"op": "le", "price": 9.90})
    r = eodengine.settle_account(
        st, DEMO, "2026-09-07",
        l1_map={"600000": [
            ("2026-09-07T14:59:00", 10.00),     # 未触达
            ("2026-09-07T15:00:00", 9.50),      # 触达落在收盘分钟
        ]},
        close_map={"600000": 9.60},
    )
    tr = _fetch(st, "SELECT * FROM trades WHERE order_id='l1-cm'")[0]
    assert tr["price"] == 9.90                     # max(X=9.90, C=9.60)=9.90
    assert tr["quality"] == "close_minute_fill"
    assert not _fetch(st, "SELECT * FROM trades WHERE quality='degraded'")
    assert tr["basis_used"] == "l1"
    assert r["cash"] == "99004.99"


def test_l1_close_minute_buy_worse_side(authed_client):
    st = authed_client.app.state
    _insert_order(st, order_id="l1-cm2", order_type="buy", qty=100,
                  trigger={"op": "le", "price": 9.90})
    eodengine.settle_account(
        st, DEMO, "2026-09-07",
        l1_map={"600000": [
            ("2026-09-07T14:58:00", 9.95),
            ("2026-09-07T15:00:00", 9.85),      # 已触达（<=9.90），仍按保守口径
        ]},
        close_map={"600000": 9.95},               # max(X=9.90, C=9.95)=9.95
    )
    tr = _fetch(st, "SELECT * FROM trades WHERE order_id='l1-cm2'")[0]
    assert tr["price"] == 9.95 and tr["quality"] == "close_minute_fill"


def test_l1_sell_take_profit_two_days(authed_client):
    st = authed_client.app.state
    _insert_order(st, order_id="l1-d1", order_type="buy", qty=100,
                  trigger={"op": "le", "price": 10.10}, created="2026-09-07T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-07",
        series_map={"600000": [("2026-09-07T09:31:00", 10.00)]},
        close_map={"600000": 10.00},
    )
    _insert_order(st, order_id="l1-d2", order_type="sell_take_profit", qty=50,
                  trigger={"op": "ge", "price": 10.20}, created="2026-09-08T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-08",
        l1_map={"600000": [
            ("2026-09-08T10:00:00", 10.10),      # 未触达（<10.20）
            ("2026-09-08T10:01:00", 10.30),      # 相邻分钟确认上穿
        ]},
        close_map={"600000": 10.30},                      # 与序列末价 10.30 一致
        prev_close_map={"600000": 10.00},
    )
    tr = _fetch(st, "SELECT * FROM trades WHERE order_id='l1-d2'")[0]
    assert tr["side"] == "sell" and tr["price"] == 10.20      # 按条件价（非采样 10.30）
    assert tr["basis_used"] == "l1" and tr["quality"] == ""  # 末价 10.30 vs 收 10.40 偏差<0.3%
    lot = _fetch(
        st,
        "SELECT l.remaining FROM lots l JOIN holdings h ON h.id=l.holding_id"
        " WHERE l.account_id=? AND h.symbol='600000'",
        (DEMO,),
    )[0]
    assert lot["remaining"] == 50


def test_l1_sell_stop_ohlc_intrabar_cross(authed_client):
    st = authed_client.app.state
    _insert_order(st, order_id="l1-o1", order_type="buy", qty=100,
                  trigger={"op": "le", "price": 10.10}, created="2026-09-07T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-07",
        series_map={"600000": [("2026-09-07T09:31:00", 10.00)]},
        close_map={"600000": 10.00},
    )
    _insert_order(st, order_id="l1-o2", order_type="sell_stop", qty=100,
                  trigger={"op": "le", "price": 9.90}, created="2026-09-08T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-08",
        l1_map={"600000": [
            ("2026-09-08T10:00:00", 10.20, 10.30, 10.05, 10.15),   # low 未破 9.90
            ("2026-09-08T10:01:00", 10.10, 10.15, 9.80, 10.00),   # intra low 破 9.90
        ]},
        close_map={"600000": 10.00},
        prev_close_map={"600000": 10.00},
    )
    tr = _fetch(st, "SELECT * FROM trades WHERE order_id='l1-o2'")[0]
    assert tr["side"] == "sell" and tr["price"] == 9.90          # 盘中触及即判触达，按 X 成交
    assert tr["basis_used"] == "l1"
    assert not _fetch(st, "SELECT * FROM lots WHERE account_id=?", (DEMO,))


def test_l1_market_order_is_explicit_gap(authed_client):
    st = authed_client.app.state
    _insert_order(st, order_id="l1-mkt", order_type="buy", qty=100, trigger=None,
                  price_type="market")
    with pytest.raises(eodengine.EngineGapError):
        eodengine.settle_account(
            st, DEMO, "2026-09-07",
            l1_map={"600000": [("2026-09-07T10:00:00", 9.90)]},
            close_map={"600000": 9.90},
        )
    assert _cash(st) == 100000.0
    assert not _fetch(st, "SELECT * FROM settlement_log WHERE account_id=?", (DEMO,))
    assert _fetch(st, "SELECT status FROM condition_orders WHERE id='l1-mkt'")[0]["status"] == "active"


def test_l1_and_l0_symbols_mixed_in_one_day(authed_client):
    st = authed_client.app.state
    _insert_order(st, order_id="mx-l1", order_type="buy", qty=100,
                  trigger={"op": "le", "price": 10.05}, created="2026-09-07T09:00:00")
    _insert_order(st, order_id="mx-l0", order_type="buy", qty=100,
                  trigger={"op": "le", "price": 10.05}, created="2026-09-07T09:00:00")
    conn = state_conn(st)
    with write_txn(conn) as c:
        c.execute(
            "UPDATE condition_orders SET symbol='600001' WHERE id='mx-l0'"
        )
    eodengine.settle_account(
        st, DEMO, "2026-09-07",
        series_map={"600001": [("2026-09-07T09:31:00", 10.00)]},
        l1_map={"600000": [
            ("2026-09-07T09:59:00", 10.30),
            ("2026-09-07T10:00:00", 9.90),
        ]},
        close_map={"600000": 9.90, "600001": 10.00},
    )
    tr = {r["order_id"]: r for r in _fetch(st, "SELECT * FROM trades")}
    assert tr["mx-l0"]["basis_used"] == "l0" and tr["mx-l0"]["price"] == 10.0
    assert tr["mx-l1"]["basis_used"] == "l1" and tr["mx-l1"]["price"] == 10.05
    sl = _fetch(st, "SELECT granularity_used FROM settlement_log WHERE account_id=?", (DEMO,))[0]
    assert json.loads(sl["granularity_used"]) == {"600000": "l1", "600001": "l0"}

"""EOD 引擎切片测试（spec-01 §3/§6 支持子集的确定性单测，价格由测试注入）。"""
from __future__ import annotations

import json

import pytest

from core import eodengine
from core.db import state_conn, write_txn

DEMO = "agent-demo-001"


def _insert_order(state, *, order_id, order_type, direction, qty, trigger, symbol="600000",
                  created="2026-09-07T09:00:00", validity="today", price_type="limit", reason=""):
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
                order_id, DEMO, order_type, direction, "single", symbol,
                json.dumps(trigger), "replay_l0", "absolute", qty, price_type, validity,
                0, "active", created, "agent-demo-001", reason,
            ),
        )


def _fetch(state, sql, args=()):
    conn = state_conn(state)
    with conn:  # 隐式读事务
        return conn.execute(sql, args).fetchall()


def _cash(state) -> float:
    return _fetch(state, "SELECT cash FROM accounts WHERE id=?", (DEMO,))[0]["cash"]


@pytest.fixture()
def demo_account():
    return DEMO


def test_eod_buy_day1(authed_client):
    st = authed_client.app.state
    _insert_order(st, order_id="co-d1", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 10.05})
    # 同日止盈单 20 元（当日未触达）→ 盘中保持 active、日终 expired
    _insert_order(st, order_id="co-d1b", order_type="sell_take_profit", direction="sell", qty=100,
                  trigger={"op": "ge", "price": 20.0}, created="2026-09-07T09:30:00")
    r = eodengine.settle_account(
        st, DEMO, "2026-09-07",
        series_map={"600000": [("2026-09-07T09:31:00", 10.20), ("2026-09-07T09:32:00", 10.00)]},
        close_map={"600000": 10.90},
    )
    assert r["already_settled"] is False
    assert r["cash"] == "98994.99"
    assert abs(float(r["total_pnl"]) - 84.99) < 0.02
    # 成交 100 股 @10.00；费用 5.01（佣金最低 5 + 过户 0.01）
    tr = _fetch(st, "SELECT * FROM trades WHERE order_id='co-d1'")[0]
    assert tr["side"] == "buy" and tr["qty"] == 100 and tr["price"] == 10.0
    assert tr["fee_total"] == 5.01 and tr["quality"] == "degraded"   # 序列末 10.00 vs 收盘 10.90 偏差 >0.3%
    lots = _fetch(st, "SELECT * FROM lots WHERE buy_trade_id=?", (tr["id"],))
    assert len(lots) == 1 and lots[0]["remaining"] == 100
    h = _fetch(st, "SELECT quantity, avg_cost FROM holdings WHERE account_id=? AND symbol='600000'", (DEMO,))[0]
    assert h["quantity"] == 100 and abs(h["avg_cost"] - 10.0501) < 1e-4
    assert _fetch(st, "SELECT status FROM condition_orders WHERE id='co-d1b'")[0]["status"] == "expired"
    # 幂等：同日重跑 → already_settled，不重复写
    r2 = eodengine.settle_account(
        st, DEMO, "2026-09-07",
        series_map={"600000": [("2026-09-07T13:00:00", 11.0)]},
        close_map={"600000": 10.90},
    )
    assert r2["already_settled"] is True
    assert len(_fetch(st, "SELECT * FROM settlement_log WHERE account_id=?", (DEMO,))) == 1


def test_eod_t1_fifo_sell_day2(authed_client):
    st = authed_client.app.state
    _insert_order(st, order_id="co-s1", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 10.05}, created="2026-09-07T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-07",
        series_map={"600000": [("2026-09-07T09:31:00", 10.00)]},
        close_map={"600000": 10.90},
    )
    # 次一交易日：卖出当日 lot（buy_date=昨日）→ 可卖
    _insert_order(st, order_id="co-s2", order_type="sell_take_profit", direction="sell", qty=50,
                  trigger={"op": "ge", "price": 11.2}, created="2026-09-08T09:00:00")
    r = eodengine.settle_account(
        st, DEMO, "2026-09-08",
        series_map={"600000": [("2026-09-08T09:31:00", 11.10), ("2026-09-08T09:32:00", 11.25)]},
        close_map={"600000": 11.00},
        prev_close_map={"600000": 10.90},
    )
    assert r["already_settled"] is False
    tr = _fetch(st, "SELECT * FROM trades WHERE order_id='co-s2'")[0]
    assert tr["side"] == "sell" and tr["qty"] == 50 and tr["price"] == 11.25
    assert tr["stamp_tax"] > 0
    lot = _fetch(
        st,
        "SELECT l.remaining FROM lots l JOIN holdings h ON h.id=l.holding_id"
        " WHERE l.account_id=? AND h.symbol='600000'",
        (DEMO,),
    )[0]
    assert lot["remaining"] == 50                              # FIFO 核销一半
    h = _fetch(st, "SELECT quantity FROM holdings WHERE account_id=? AND symbol='600000'", (DEMO,))[0]
    assert h["quantity"] == 50

    # ---- 次一结算日（同一天单次 settle）内的 T+1 场景 ----
    # 新插入当日订单，再对 2026-09-09 做一次结算：当日新买 lot 不可同日卖出
    _insert_order(st, order_id="co-b2", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 9.9}, created="2026-09-09T10:00:00",
                  reason="测试当日买入")
    _insert_order(st, order_id="co-s3", order_type="sell_stop", direction="sell", qty=100,
                  trigger={"op": "le", "price": 9.0}, created="2026-09-09T10:05:00")
    r2 = eodengine.settle_account(
        st, DEMO, "2026-09-09",
        series_map={"600000": [
            ("2026-09-09T10:30:00", 9.5),
            ("2026-09-09T10:40:00", 8.9),   # 触发 co-s3，但可卖仅昨日剩余 50
        ]},
        close_map={"600000": 9.0},
        prev_close_map={"600000": 11.00},
    )
    assert r2["already_settled"] is False
    assert _fetch(st, "SELECT * FROM trades WHERE order_id='co-b2'")[0]["qty"] == 100
    # T+1：当日新买 lot 不可卖 → 可卖 50 < 100 → 记 insufficient 事件、不成交、日终 expired
    assert not _fetch(st, "SELECT * FROM trades WHERE order_id='co-s3'")
    o3 = _fetch(st, "SELECT status, insufficient_events FROM condition_orders WHERE id='co-s3'")[0]
    assert o3["status"] == "expired" and o3["insufficient_events"] == 1
    h2 = _fetch(st, "SELECT quantity FROM holdings WHERE account_id=? AND symbol='600000'", (DEMO,))[0]
    assert h2["quantity"] == 150            # 50(旧) + 100(当日买入，未卖出)
    # 幂等：同日重跑不重复
    assert eodengine.settle_account(
        st, DEMO, "2026-09-09",
        series_map={"600000": [("2026-09-09T10:30:00", 9.5)]},
        close_map={"600000": 9.0},
        prev_close_map={"600000": 11.00},
    )["already_settled"] is True


def test_eod_insufficient_funds_then_fill(authed_client):
    st = authed_client.app.state
    _insert_order(st, order_id="co-ins", order_type="buy", direction="buy", qty=9900,
                  trigger={"op": "le", "price": 10.5}, created="2026-09-07T09:00:00")
    r = eodengine.settle_account(
        st, DEMO, "2026-09-07",
        series_map={"600000": [("2026-09-07T09:31:00", 10.20), ("2026-09-07T09:32:00", 10.00)]},
        close_map={"600000": 10.00},
    )
    o = _fetch(st, "SELECT status, insufficient_events FROM condition_orders WHERE id='co-ins'")[0]
    assert o["status"] == "filled" and o["insufficient_events"] == 1
    assert abs(_cash(st) - 974.26) < 0.02
    tr = _fetch(st, "SELECT price FROM trades WHERE order_id='co-ins'")[0]
    assert tr["price"] == 10.0


def test_eod_qty_rule_invalid(authed_client):
    st = authed_client.app.state
    _insert_order(st, order_id="co-qty", order_type="buy", direction="buy", qty=150,
                  trigger={"op": "le", "price": 20.0}, created="2026-09-07T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-07",
        series_map={"600000": [("2026-09-07T09:31:00", 10.00)]},
        close_map={"600000": 10.00},
    )
    o = _fetch(st, "SELECT status, invalid_reason FROM condition_orders WHERE id='co-qty'")[0]
    assert o["status"] == "invalid" and o["invalid_reason"] == "qty_rule"
    assert not _fetch(st, "SELECT * FROM trades WHERE order_id='co-qty'")


def test_eod_gap_refuses_and_writes_nothing(authed_client):
    st = authed_client.app.state
    _insert_order(st, order_id="co-gap", order_type="basket", direction="buy", qty=100,
                  trigger={}, created="2026-09-07T09:00:00")
    with pytest.raises(eodengine.EngineGapError):
        eodengine.settle_account(
            st, DEMO, "2026-09-07",
            series_map={"600000": [("2026-09-07T09:31:00", 10.00)]},
            close_map={"600000": 10.00},
        )
    assert _cash(st) == 100000.0
    assert not _fetch(st, "SELECT * FROM settlement_log WHERE account_id=?", (DEMO,))
    assert _fetch(st, "SELECT status FROM condition_orders WHERE id='co-gap'")[0]["status"] == "active"


def test_eod_l2_hist_range_fill_at_official_close(authed_client):
    """L2 日线区间档：触达恒以官方收盘价成交，basis_used='l2'，结算日志记档。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-l2", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 9.5}, created="2026-09-07T09:00:00")
    r = eodengine.settle_account(
        st, DEMO, "2026-09-07",
        l2_map={"600000": {"high": 11.20, "low": 9.20}},
        close_map={"600000": 9.80},
    )
    assert r["already_settled"] is False
    tr = _fetch(st, "SELECT * FROM trades WHERE order_id='co-l2'")[0]
    assert tr["side"] == "buy" and tr["qty"] == 100 and tr["price"] == 9.8
    assert tr["basis_used"] == "l2" and tr["quality"] == ""
    assert tr["trade_time"] == "2026-09-07T15:00:00"     # L2 恒以官方收盘时点成交
    sl = _fetch(st, "SELECT * FROM settlement_log WHERE account_id=?", (DEMO,))[0]
    assert json.loads(sl["granularity_used"]) == {"600000": "l2"}


def test_eod_l2_not_touched_stays_and_expires(authed_client):
    """L2 区间未触达 → 当日不成交、日终 expired；收盘无现金 → insufficient。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-l2b", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 8.0}, created="2026-09-07T09:00:00")
    _insert_order(st, order_id="co-l2c", order_type="buy", direction="buy", qty=1_000_000,
                  trigger={"op": "le", "price": 9.5}, created="2026-09-07T09:01:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-07",
        l2_map={"600000": {"high": 11.20, "low": 9.20}},
        close_map={"600000": 9.80},
    )
    # 未触达（区间低 9.20 > 触发 8.0）→ 无单成交、日终 expired
    assert not _fetch(st, "SELECT * FROM trades WHERE order_id='co-l2b'")
    o = _fetch(st, "SELECT status FROM condition_orders WHERE id='co-l2b'")[0]
    assert o["status"] == "expired"
    # 触达但收盘现金不足（100 万股 @9.8 远超余额）→ insufficient、不成交
    o2 = _fetch(st, "SELECT status, insufficient_events FROM condition_orders WHERE id='co-l2c'")[0]
    assert o2["status"] == "expired" and o2["insufficient_events"] == 1
    assert not _fetch(st, "SELECT * FROM trades WHERE order_id='co-l2c'")


def test_eod_l2_sell_uses_t1_prev_lots(authed_client):
    """L2 卖出同样受 T+1：仅历史持仓可卖，当日新买不可同日卖。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-p", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 9.0}, created="2026-09-07T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-07",
        l2_map={"600000": {"high": 9.10, "low": 8.90}},
        close_map={"600000": 9.00},
    )
    _insert_order(st, order_id="co-bd", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 9.5}, created="2026-09-08T09:30:00")
    _insert_order(st, order_id="co-sd", order_type="sell_take_profit", direction="sell", qty=150,
                  trigger={"op": "ge", "price": 8.0}, created="2026-09-08T09:31:00")
    r = eodengine.settle_account(
        st, DEMO, "2026-09-08",
        l2_map={"600000": {"high": 9.30, "low": 9.10}},
        close_map={"600000": 9.20},
        prev_close_map={"600000": 9.00},
    )
    # 当日新买 100 不可卖 → 可卖仅昨日 100 < 150 → 卖出不成交
    assert not _fetch(st, "SELECT * FROM trades WHERE order_id='co-sd'")
    o = _fetch(st, "SELECT status, insufficient_events FROM condition_orders WHERE id='co-sd'")[0]
    assert o["status"] == "expired" and o["insufficient_events"] == 1
    h = _fetch(st, "SELECT quantity FROM holdings WHERE account_id=? AND symbol='600000'", (DEMO,))[0]
    assert h["quantity"] == 200


def test_eod_l1_l2_duplicated_tier_rejected(authed_client):
    """档位须按票唯一：同一 symbol 同时给 L1 与 L2 或 L0 与 L2 → EngineError，零写入。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-du1", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 9.5}, created="2026-09-07T09:00:00")
    with pytest.raises(eodengine.EngineError, match="档位须按票唯一"):
        eodengine.settle_account(
            st, DEMO, "2026-09-07",
            series_map={"600000": [("2026-09-07T09:31:00", 9.30)]},
            l2_map={"600000": {"high": 9.5, "low": 9.1}},
            close_map={"600000": 9.30},
        )
    assert _cash(st) == 100000.0
    assert not _fetch(st, "SELECT * FROM settlement_log WHERE account_id=?", (DEMO,))


def test_eod_mixed_tier_per_symbol_orders(authed_client):
    """多票同日混合档位：600000 走 L0、600001 走 L2，各自按其档位判定成交。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-m1", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 10.05}, symbol="600000",
                  created="2026-09-07T09:00:00")
    _insert_order(st, order_id="co-m2", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 12.0}, symbol="600001",
                  created="2026-09-07T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-07",
        series_map={"600000": [("2026-09-07T09:31:00", 10.00)]},
        l2_map={"600001": {"high": 13.0, "low": 11.5}},
        close_map={"600000": 10.20, "600001": 12.50},
    )
    t1 = _fetch(st, "SELECT price, basis_used FROM trades WHERE order_id='co-m1'")[0]
    t2 = _fetch(st, "SELECT price, basis_used FROM trades WHERE order_id='co-m2'")[0]
    assert t1["price"] == 10.0 and t1["basis_used"] == "l0"
    assert t2["price"] == 12.5 and t2["basis_used"] == "l2"
    sl = _fetch(st, "SELECT * FROM settlement_log WHERE account_id=?", (DEMO,))[0]
    assert json.loads(sl["granularity_used"]) == {"600000": "l0", "600001": "l2"}


def test_eod_future_dated_order_untouched_by_earlier_settle(authed_client):
    """多日连跑护栏：结算早于某 order 创建日 → 该单保持 active，不被误 expired/invalid。"""
    st = authed_client.app.state
    # 次日单在当日结算前就已落库（试运行连跑/回放常见）→ 09-07 结算不得处置它
    _insert_order(st, order_id="co-fut", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 10.05}, created="2026-09-08T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-07",
        series_map={"600000": [("2026-09-07T09:31:00", 9.90)]},
        close_map={"600000": 9.90},
    )
    o = _fetch(st, "SELECT status FROM condition_orders WHERE id='co-fut'")[0]
    assert o["status"] == "active"            # 未过期、未置 invalid
    assert not _fetch(st, "SELECT * FROM trades WHERE order_id='co-fut'")


def test_eod_conservation_break_rolls_back_whole_txn(authed_client, monkeypatch):
    """对账自检发现账实不平 → EngineError → 单事务整体回滚：零成交/零日志/现金不动（带病不落账）。"""
    import core.eodengine as _e
    st = authed_client.app.state
    _insert_order(st, order_id="co-rec", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 10.05}, created="2026-09-07T09:00:00")
    real_audit = _e._audit_conservation

    def corrupt(c, account_id, trade_date, **kw):
        c.execute(
            """
            INSERT INTO trades(id, account_id, order_id, symbol, side, qty, price, amount,
                fee_total, commission, stamp_tax, transfer_fee, trade_time, basis_requested,
                basis_used, quality, settle_date, reason, strategy_version_no)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            ("tphantom", account_id, "co-rec", "600000", "sell", 100, 1.0, 100.0,
             0.0, 0.0, 0.0, 0.0, "2026-09-07T15:00:00", "replay_l0", "l0", "",
             trade_date, "注入：账实不平", ""),
        )
        return real_audit(c, account_id, trade_date, **kw)   # 幻影成交未被现金路径跟随 → 式一不平

    monkeypatch.setattr(_e, "_audit_conservation", corrupt)
    with pytest.raises(_e.EngineError, match="对账式一"):
        _e.settle_account(
            st, DEMO, "2026-09-07",
            series_map={"600000": [("2026-09-07T09:31:00", 10.00)]},
            close_map={"600000": 10.90},
        )
    monkeypatch.undo()
    assert _cash(st) == 100000.0
    assert _fetch(st, "SELECT COUNT(*) AS n FROM trades WHERE account_id=?", (DEMO,))[0]["n"] == 0
    assert _fetch(st, "SELECT COUNT(*) AS n FROM holdings WHERE account_id=?", (DEMO,))[0]["n"] == 0
    assert _fetch(st, "SELECT COUNT(*) AS n FROM settlement_log WHERE account_id=?", (DEMO,))[0]["n"] == 0
    assert _fetch(st, "SELECT status FROM condition_orders WHERE id='co-rec'")[0]["status"] == "active"


def test_settle_products_visible_via_api(authed_client):
    """结算产物（成交/结算日志）经只读 API 序列化可见。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-api", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 10.05}, created="2026-09-07T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-07",
        series_map={"600000": [("2026-09-07T09:31:00", 10.00)]},
        close_map={"600000": 10.90},
    )
    tr = authed_client.get(f"/api/accounts/{DEMO}/trades")
    assert tr.status_code == 200, tr.text
    rows = tr.json()["trades"]
    assert len(rows) == 1
    t = rows[0]
    assert t["symbol"] == "600000" and t["side"] == "buy"
    assert float(t["price"]) == 10.0 and float(t["qty"]) == 100
    assert t["settle_date"] == "2026-09-07"
    # settle_date 过滤
    assert authed_client.get(f"/api/accounts/{DEMO}/trades?settle_date=2026-09-08").json()["trades"] == []
    sl = authed_client.get(f"/api/accounts/{DEMO}/settlements")
    assert sl.status_code == 200
    logs = sl.json()["settlements"]
    assert len(logs) == 1
    assert logs[0]["settle_key"] == "2026-09-07:agent-demo-001"
    assert logs[0]["granularity_used"] == {"600000": "l0"}
    assert logs[0]["status"] == "done"

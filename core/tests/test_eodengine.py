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


def test_eod_kind_trigger_price_le_fills_like_legacy(authed_client):
    """spec-01 §2.4 canonical kind 触发（price_le）与 legacy op 等价成交。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-k1", order_type="buy", direction="buy", qty=100,
                  trigger={"kind": "price_le", "price": 10.05}, created="2026-09-07T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-07",
        series_map={"600000": [("2026-09-07T09:31:00", 10.20), ("2026-09-07T09:32:00", 10.00)]},
        close_map={"600000": 10.90},
    )
    tr = _fetch(st, "SELECT * FROM trades WHERE order_id='co-k1'")[0]
    assert tr["side"] == "buy" and tr["qty"] == 100 and tr["price"] == 10.0


def test_eod_trail_no_holding_ins_then_expired(authed_client):
    """sell_trail 无持仓/无历史基准 → ins 事件、保持 active 至日终 expired、零成交。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-tr", order_type="sell_trail", direction="sell", qty=100,
                  trigger={"kind": "trail", "drop_pct": 3}, created="2026-09-07T09:00:00")
    r = eodengine.settle_account(
        st, DEMO, "2026-09-07",
        series_map={"600000": [("2026-09-07T09:31:00", 10.00), ("2026-09-07T09:32:00", 10.50)]},
        close_map={"600000": 10.00},
    )
    assert r["already_settled"] is False
    assert _cash(st) == 100000.0
    assert not _fetch(st, "SELECT * FROM trades WHERE order_id='co-tr'")
    o = _fetch(st, "SELECT status FROM condition_orders WHERE id='co-tr'")[0]
    assert o["status"] == "expired"


def test_eod_trail_l0_drop_fills_at_sample(authed_client):
    """移动止盈 L0（spec-01 §4.1 ✅）：回撤基准=hist_high 与采样累计最大，跌破 drop 线成交。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-tr-l0-buy", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 10.05}, created="2026-09-07T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-07",
        series_map={"600000": [("2026-09-07T09:31:00", 10.00)]},
        close_map={"600000": 10.90},
    )
    _insert_order(st, order_id="co-tr-l0", order_type="sell_trail", direction="sell", qty=100,
                  trigger={"kind": "trail", "drop_pct": 10}, created="2026-09-08T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-08",
        series_map={"600000": [("2026-09-08T09:31:00", 12.00), ("2026-09-08T09:32:00", 11.50),
                               ("2026-09-08T09:33:00", 10.90), ("2026-09-08T09:34:00", 10.70)]},
        close_map={"600000": 10.70},
        prev_close_map={"600000": 10.90},
        hist_high_map={"600000": 12.0},
    )
    tr = _fetch(st, "SELECT * FROM trades WHERE order_id='co-tr-l0'")[0]
    assert tr["side"] == "sell" and tr["qty"] == 100 and tr["price"] == 10.7
    # hist_high=12 → 回撤线 12×0.9=10.8；09:33 价 10.9 未破线，09:34 价 10.7 跌破成交
    assert _fetch(st, "SELECT COUNT(*) AS n FROM holdings WHERE account_id=? AND symbol='600000'",
                  (DEMO,))[0]["n"] == 0


def test_eod_trail_l1_degraded_drop_fill(authed_client):
    """移动止盈 L1 退化（spec-01 §4.1 ⚠️）：分钟 high 累计最大为基准，跌破按线价成交并标 degraded。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-tr-l1-buy", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 10.05}, created="2026-09-07T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-07",
        l1_map={"600000": [("2026-09-07T09:31:00", 10.00)]},
        close_map={"600000": 10.90},
    )
    _insert_order(st, order_id="co-tr-l1", order_type="sell_trail", direction="sell", qty=100,
                  trigger={"kind": "trail", "drop_pct": 5}, created="2026-09-08T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-08",
        l1_map={"600000": [("2026-09-08T09:31:00", 10.8, 12.5, 12.0, 12.0),
                           ("2026-09-08T09:32:00", 12.5, 13.0, 11.5, 11.7)]},
        close_map={"600000": 11.7},
        prev_close_map={"600000": 10.90},
        hist_high_map={"600000": 12.0},
    )
    tr = _fetch(st, "SELECT * FROM trades WHERE order_id='co-tr-l1'")[0]
    assert tr["side"] == "sell" and tr["qty"] == 100 and tr["quality"] == "degraded"
    # 09:31 high 12.5 抬高基准 → 线 12.5×0.95=11.875（low 12.0 未破）；
    # 09:32 high 13.0 再抬高基准 → 线 13×0.95=12.35，low 11.5 跌破，按线价成交
    assert abs(float(tr["price"]) - 12.35) < 1e-9


def test_eod_trail_l2_degraded_single_point(authed_client):
    """移动止盈 L2 退化（spec-01 §3.3 写死口径）：H=max(买入以来日线 high, 当日 high)，
    当日 low ≤ H×(1−drop) 即触达，以官方收盘价成交并标 degraded。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-tr-l2-buy", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 9.5}, created="2026-09-07T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-07",
        l2_map={"600000": {"high": 9.8, "low": 9.2}},
        close_map={"600000": 9.8},
    )
    _insert_order(st, order_id="co-tr-l2", order_type="sell_trail", direction="sell", qty=100,
                  trigger={"kind": "trail", "drop_pct": 10}, created="2026-09-08T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-08",
        l2_map={"600000": {"high": 12.6, "low": 11.0}},
        close_map={"600000": 11.2},
        prev_close_map={"600000": 9.8},
        hist_high_map={"600000": 13.0},
    )
    tr = _fetch(st, "SELECT * FROM trades WHERE order_id='co-tr-l2'")[0]
    assert tr["side"] == "sell" and tr["qty"] == 100 and tr["price"] == 11.2
    assert tr["quality"] == "degraded" and tr["basis_used"] == "l2"
    assert tr["trade_time"] == "2026-09-08T15:00:00"


def test_eod_trail_l2_no_trigger_expired(authed_client):
    """L2 trail 未跌破回撤线 → 保持 active 至日终 expired，零成交。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-tr-l2x-buy", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 9.5}, created="2026-09-07T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-07",
        l2_map={"600000": {"high": 9.8, "low": 9.2}},
        close_map={"600000": 9.8},
    )
    _insert_order(st, order_id="co-tr-l2x", order_type="sell_trail", direction="sell", qty=100,
                  trigger={"kind": "trail", "drop_pct": 10}, created="2026-09-08T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-08",
        l2_map={"600000": {"high": 11.0, "low": 10.9}},
        close_map={"600000": 10.9},
        prev_close_map={"600000": 9.8},
        hist_high_map={"600000": 10.0},
    )
    assert not _fetch(st, "SELECT * FROM trades WHERE order_id='co-tr-l2x'")
    assert _fetch(st, "SELECT status FROM condition_orders WHERE id='co-tr-l2x'"
                  )[0]["status"] == "expired"


def test_lock_limit_px_boards():
    """涨跌停价按 spec-01 §3.6 板块系数与 §0 四舍五入（L0/L1/L2 封死判定的价口径）。"""
    d = eodengine._limit_px
    D = eodengine._D
    assert d(D("10.00"), "main") == (D("11.00"), D("9.00"))
    assert d(D("10.00"), "gem") == (D("12.00"), D("8.00"))
    assert d(D("10.00"), "star") == (D("12.00"), D("8.00"))
    assert d(D("10.00"), "bj") == (D("13.00"), D("7.00"))
    assert d(D("10.00"), "st_main") == (D("10.50"), D("9.50"))
    assert d(D("10.00"), "unknown") == (D("11.00"), D("9.00"))   # 未知板块回退 main
    assert d(D("3.33"), "main") == (D("3.66"), D("3.00"))        # §0 half-up 到分


def test_lock_l0_buy_skips_pinned_up_then_fills_on_gap(authed_client):
    """涨停封死段买入不成交、盘中开板恢复（L0，main ±10%）。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-lock-l0", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 11.0}, created="2026-09-08T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-08",
        series_map={"600000": [("2026-09-08T09:31:00", 11.00), ("2026-09-08T09:32:00", 11.00),
                               ("2026-09-08T09:33:00", 10.98), ("2026-09-08T09:34:00", 10.98)]},
        close_map={"600000": 10.98},
        prev_close_map={"600000": 10.00},
        board_map={"600000": "main"},
    )
    tr = _fetch(st, "SELECT * FROM trades WHERE order_id='co-lock-l0'")
    assert len(tr) == 1
    assert tr[0]["price"] == 10.98   # 09:31/09:32 封死涨停不成交；09:33 开板成交
    assert _fetch(st, "SELECT status FROM condition_orders WHERE id='co-lock-l0'"
                  )[0]["status"] == "filled"


def test_lock_l0_sell_blocked_at_pinned_down_expires(authed_client):
    """跌停封死段卖出不成交（L0）：整日一字跌停 → 止损永不触发 → 日终 expired，持仓保留。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-lock-buy", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 10.05}, created="2026-09-07T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-07",
        series_map={"600000": [("2026-09-07T09:31:00", 10.00)]},
        close_map={"600000": 10.00},
    )
    _insert_order(st, order_id="co-lock-sell", order_type="sell_stop", direction="sell", qty=100,
                  trigger={"op": "le", "price": 9.30}, created="2026-09-08T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-08",
        series_map={"600000": [("2026-09-08T09:31:00", 9.00), ("2026-09-08T09:32:00", 9.00),
                               ("2026-09-08T09:33:00", 9.00), ("2026-09-08T09:34:00", 9.00)]},
        close_map={"600000": 9.00},
        prev_close_map={"600000": 10.00},
        board_map={"600000": "main"},
    )
    assert not _fetch(st, "SELECT * FROM trades WHERE order_id='co-lock-sell'")
    assert _fetch(st, "SELECT status FROM condition_orders WHERE id='co-lock-sell'"
                  )[0]["status"] == "expired"
    assert _fetch(st, "SELECT quantity FROM holdings WHERE account_id=? AND symbol='600000'",
                  (DEMO,))[0]["quantity"] == 100


def test_lock_l1_pinned_minute_blocks_then_open_minute_fill(authed_client):
    """L1 整分钟封死（一字分钟）卖不成交；开板分钟恢复成交（§3.5 近似口径）。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-lockl1-buy", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 10.05}, created="2026-09-07T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-07",
        l1_map={"600000": [("2026-09-07T09:31:00", 10.00)]},
        close_map={"600000": 10.00},
    )
    _insert_order(st, order_id="co-lockl1", order_type="sell_stop", direction="sell", qty=100,
                  trigger={"op": "le", "price": 9.30}, created="2026-09-08T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-08",
        l1_map={"600000": [("2026-09-08T09:31:00", 9.00, 9.00, 9.00, 9.00),
                           ("2026-09-08T09:32:00", 9.00, 9.02, 8.95, 9.01)]},
        close_map={"600000": 9.01},
        prev_close_map={"600000": 10.00},
        board_map={"600000": "main"},
    )
    tr = _fetch(st, "SELECT * FROM trades WHERE order_id='co-lockl1'")
    assert len(tr) == 1
    assert tr[0]["basis_used"] == "l1"
    assert abs(float(tr[0]["price"]) - 9.3) < 1e-9   # 首分钟一字封死跳过，开板分钟按线价成交
    assert _fetch(st, "SELECT COUNT(*) AS n FROM holdings WHERE account_id=? AND symbol='600000'",
                  (DEMO,))[0]["n"] == 0


def test_lock_l2_oneword_down_sell_expires(authed_client):
    """L2 一字跌停全天（日线档近似口径 hi==lo==跌停价）卖出不成交 → 日终 expired。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-lockl2-buy", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 9.9}, created="2026-09-07T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-07",
        l2_map={"600000": {"high": 9.6, "low": 9.2}},
        close_map={"600000": 9.6},
    )
    _insert_order(st, order_id="co-lockl2", order_type="sell_stop", direction="sell", qty=100,
                  trigger={"op": "le", "price": 9.30}, created="2026-09-08T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-08",
        l2_map={"600000": {"high": 9.00, "low": 9.00}},
        close_map={"600000": 9.00},
        prev_close_map={"600000": 10.00},
        board_map={"600000": "main"},
    )
    assert not _fetch(st, "SELECT * FROM trades WHERE order_id='co-lockl2'")
    assert _fetch(st, "SELECT status FROM condition_orders WHERE id='co-lockl2'"
                  )[0]["status"] == "expired"
    assert _fetch(st, "SELECT quantity FROM holdings WHERE account_id=? AND symbol='600000'",
                  (DEMO,))[0]["quantity"] == 100


def test_eod_pct_l0_not_limit_blocks_pinned_down(authed_client):
    """pct_chg（昨收 −5%，le）L0：not_limit 在跌停封死采样点不触发，开板后按采样价成交。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-pctl0", order_type="buy", direction="buy", qty=100,
                  trigger={"kind": "pct_chg", "op": "le", "pct": -5, "not_limit": True},
                  created="2026-09-08T09:00:00", price_type="market")
    eodengine.settle_account(
        st, DEMO, "2026-09-08",
        series_map={"600000": [("2026-09-08T09:31:00", 9.00), ("2026-09-08T09:32:00", 9.20),
                               ("2026-09-08T09:33:00", 9.20)]},
        close_map={"600000": 9.20},
        prev_close_map={"600000": 10.00},
        board_map={"600000": "main"},
    )
    tr = _fetch(st, "SELECT * FROM trades WHERE order_id='co-pctl0'")
    assert len(tr) == 1 and tr[0]["price"] == 9.2   # 09:31 一字跌停(9.00)跳过；09:32 开板成交
    assert tr[0]["basis_used"] == "l0"


def test_eod_pct_l1_fills_at_converted_price(authed_client):
    """pct_chg L1：折算触发价 X=昨收×(1+pct%)，按条件价成交（同价格类口径）。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-pctl1-buy", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 10.05}, created="2026-09-07T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-07",
        l1_map={"600000": [("2026-09-07T09:31:00", 10.00)]},
        close_map={"600000": 10.00},
    )
    _insert_order(st, order_id="co-pctl1", order_type="sell_stop", direction="sell", qty=100,
                  trigger={"kind": "pct_chg", "op": "le", "pct": -5},
                  created="2026-09-08T09:00:00", price_type="market")
    eodengine.settle_account(
        st, DEMO, "2026-09-08",
        l1_map={"600000": [("2026-09-08T09:31:00", 9.6, 9.6, 9.2, 9.3),
                           ("2026-09-08T09:32:00", 9.3, 9.4, 9.2, 9.3)]},
        close_map={"600000": 9.3},
        prev_close_map={"600000": 10.00},
    )
    tr = _fetch(st, "SELECT * FROM trades WHERE order_id='co-pctl1'")
    assert len(tr) == 1 and tr[0]["basis_used"] == "l1"
    assert abs(float(tr[0]["price"]) - 9.5) < 1e-9   # 昨收 10 → −5% 折算 X=9.50，按条件价成交


def test_eod_pct_l2_range_fill_at_close(authed_client):
    """pct_chg L2：日线区间触及折算价 → 官方收盘价成交（区间触达近似）。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-pctl2-buy", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 10.05}, created="2026-09-07T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-07",
        l2_map={"600000": {"high": 9.6, "low": 9.3}},
        close_map={"600000": 9.6},
    )
    _insert_order(st, order_id="co-pctl2", order_type="sell_stop", direction="sell", qty=100,
                  trigger={"kind": "pct_chg", "op": "le", "pct": -5},
                  created="2026-09-08T09:00:00", price_type="market")
    eodengine.settle_account(
        st, DEMO, "2026-09-08",
        l2_map={"600000": {"high": 9.6, "low": 9.1}},
        close_map={"600000": 9.4},
        prev_close_map={"600000": 10.00},
    )
    tr = _fetch(st, "SELECT * FROM trades WHERE order_id='co-pctl2'")
    assert len(tr) == 1 and tr[0]["basis_used"] == "l2"
    assert tr[0]["price"] == 9.4   # low 9.1 ≤ 折算 9.5 → 区间触达，官方收盘价成交


def test_eod_vscost_l0_dynamic_threshold(authed_client):
    """vs_cost L0：基准=可卖 lot 买入均价，折算 X=成本×（1+pct%），按采样价成交。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-vs-buy", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 10.05}, created="2026-09-07T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-07",
        series_map={"600000": [("2026-09-07T09:31:00", 10.00)]},
        close_map={"600000": 10.00},
    )
    _insert_order(st, order_id="co-vs", order_type="sell_take_profit", direction="sell", qty=100,
                  trigger={"kind": "vs_cost", "op": "ge", "pct": 8},
                  created="2026-09-08T09:00:00", price_type="market")
    eodengine.settle_account(
        st, DEMO, "2026-09-08",
        series_map={"600000": [("2026-09-08T09:31:00", 10.60), ("2026-09-08T09:32:00", 10.90)]},
        close_map={"600000": 10.90},
        prev_close_map={"600000": 10.00},
    )
    tr = _fetch(st, "SELECT * FROM trades WHERE order_id='co-vs'")
    assert len(tr) == 1
    # 成本 10.00 → X=10.80；09:31 10.60 未达，09:32 10.90 触发按采样价成交
    assert tr[0]["price"] == 10.9
    assert _fetch(st, "SELECT COUNT(*) AS n FROM holdings WHERE account_id=? AND symbol='600000'",
                  (DEMO,))[0]["n"] == 0


def test_eod_vscost_l1_basis_uses_sellable_lots(authed_client):
    """vs_cost L1：多次买入（FIFO 不同买价）后成本基准=可卖 lot 均价，动态折算按条件价成交。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-vsl1-b1", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 10.05}, created="2026-09-07T09:00:00")
    _insert_order(st, order_id="co-vsl1-b2", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 12.05}, created="2026-09-07T09:31:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-07",
        l1_map={"600000": [("2026-09-07T09:31:00", 10.00), ("2026-09-07T09:32:00", 12.00)]},
        close_map={"600000": 12.00},
    )
    _insert_order(st, order_id="co-vsl1", order_type="sell_take_profit", direction="sell",
                  qty=100, trigger={"kind": "vs_cost", "op": "ge", "pct": 8},
                  created="2026-09-08T09:00:00", price_type="market")
    eodengine.settle_account(
        st, DEMO, "2026-09-08",
        l1_map={"600000": [("2026-09-08T09:31:00", 11.4, 11.4, 11.2, 11.3),
                           ("2026-09-08T09:32:00", 11.3, 12.1, 11.3, 11.8)]},
        close_map={"600000": 11.8},
        prev_close_map={"600000": 11.0},
    )
    tr = _fetch(st, "SELECT * FROM trades WHERE order_id='co-vsl1'")
    assert len(tr) == 1 and tr[0]["basis_used"] == "l1"
    # L1 买价按条件价 10.05/12.05 → 成本 = 11.05 → X = 11.05×1.08 = 11.934；
    # 09:31 high 11.4 未达；09:32 high 12.1 触发按 X 成交
    assert abs(float(tr[0]["price"]) - 11.934) < 1e-9


def test_eod_vscost_l2_no_basis_skips_and_expires(authed_client):
    """vs_cost 无成本基准（无当日可卖 lot）→ 各采样点不触达，日终 expired 且零 insufficient。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-vsl2x", order_type="sell_stop", direction="sell", qty=100,
                  trigger={"kind": "vs_cost", "op": "le", "pct": -5},
                  created="2026-09-08T09:00:00", price_type="market")
    eodengine.settle_account(
        st, DEMO, "2026-09-08",
        l2_map={"600000": {"high": 8.0, "low": 7.0}},
        close_map={"600000": 7.5},
    )
    assert not _fetch(st, "SELECT * FROM trades WHERE order_id='co-vsl2x'")
    row = _fetch(st, "SELECT status, insufficient_events FROM condition_orders WHERE id='co-vsl2x'"
                  )[0]
    assert row["status"] == "expired" and row["insufficient_events"] == 0


def test_eod_pct_missing_prev_close_raises(authed_client):
    """pct_chg 触发缺少昨收基准 → 显式 gap 整事务拒绝（无半截写入）。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-pct-nopc", order_type="buy", direction="buy", qty=100,
                  trigger={"kind": "pct_chg", "op": "le", "pct": -5},
                  created="2026-09-08T09:00:00", price_type="market")
    with pytest.raises(eodengine.EngineError, match="昨收"):
        eodengine.settle_account(
            st, DEMO, "2026-09-08",
            series_map={"600000": [("2026-09-08T09:31:00", 9.0)]},
            close_map={"600000": 9.0},
        )
    assert not _fetch(st, "SELECT * FROM trades")


def test_eod_restricted_st_ipo_buy_blocked_default(authed_client):
    """restrict_map 命中且账户未豁免 → 买入单开盘前 invalid（restricted_buy），普通票照常成交。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-rst", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 10.05}, symbol="600000",
                  created="2026-09-08T09:00:00")
    _insert_order(st, order_id="co-rst2", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 10.05}, symbol="600001",
                  created="2026-09-08T09:00:00")
    _insert_order(st, order_id="co-rfree", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 10.05}, symbol="600002")
    eodengine.settle_account(
        st, DEMO, "2026-09-08",
        series_map={"600000": [("2026-09-08T09:31:00", 10.00)],
                    "600001": [("2026-09-08T09:31:00", 10.00)],
                    "600002": [("2026-09-08T09:31:00", 10.00)]},
        close_map={"600000": 10.0, "600001": 10.0, "600002": 10.0},
        restrict_map={"600000": "st", "600001": "ipo, st"},
    )
    a = _fetch(st, "SELECT status, invalid_reason FROM condition_orders WHERE id='co-rst'")[0]
    assert a["status"] == "invalid" and a["invalid_reason"] == "restricted_buy:st"
    b = _fetch(st, "SELECT status, invalid_reason FROM condition_orders WHERE id='co-rst2'")[0]
    assert b["status"] == "invalid" and b["invalid_reason"] == "restricted_buy:ipo,st"
    assert not _fetch(st, "SELECT * FROM trades WHERE order_id IN ('co-rst','co-rst2')")
    assert _fetch(st, "SELECT * FROM trades WHERE order_id='co-rfree'")


def test_eod_restricted_buy_exempt_account(authed_client):
    """accounts.buy_exempt=['st'] → ST 买入豁免成交（IPO 未豁免仍拦）。"""
    st = authed_client.app.state
    conn = state_conn(st)
    with write_txn(conn) as c:
        c.execute("UPDATE accounts SET buy_exempt=? WHERE id=?", ('["st"]', DEMO))
    _insert_order(st, order_id="co-xst", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 10.05}, symbol="600000")
    _insert_order(st, order_id="co-xipo", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 10.05}, symbol="600001",
                  created="2026-09-08T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-08",
        series_map={"600000": [("2026-09-08T09:31:00", 10.00)],
                    "600001": [("2026-09-08T09:31:00", 10.00)]},
        close_map={"600000": 10.0, "600001": 10.0},
        restrict_map={"600000": "st", "600001": "ipo"},
    )
    assert _fetch(st, "SELECT * FROM trades WHERE order_id='co-xst'")
    b = _fetch(st, "SELECT status, invalid_reason FROM condition_orders WHERE id='co-xipo'")[0]
    assert b["status"] == "invalid" and b["invalid_reason"] == "restricted_buy:ipo"


def test_eod_restricted_sell_not_intercepted(authed_client):
    """拦截仅作用于买入；卖出类（含已持仓 ST 减仓）不受 restrict_map 影响。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-rsell", order_type="sell_stop", direction="sell", qty=100,
                  trigger={"op": "le", "price": 9.5}, symbol="600000",
                  created="2026-09-08T09:30:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-08",
        series_map={"600000": [("2026-09-08T09:31:00", 9.40)]},
        close_map={"600000": 9.40},
        restrict_map={"600000": "st"},
    )
    row = _fetch(st, "SELECT status, invalid_reason, insufficient_events "
                     "FROM condition_orders WHERE id='co-rsell'")[0]
    assert row["invalid_reason"] == ""
    assert row["status"] in ("active", "expired")  # 缺持仓 → insufficient 而非 restricted 拦截


def _buy_then(state, day1="2026-09-07", qty=100, price=10.0):
    _insert_order(state, order_id="co-hd", order_type="buy", direction="buy", qty=qty,
                  trigger={"op": "le", "price": 10.05}, created=f"{day1}T09:00:00")
    eodengine.settle_account(
        state, DEMO, day1,
        series_map={"600000": [(f"{day1}T09:31:00", price)]},
        close_map={"600000": price},
    )


def test_eod_open_board_fills_on_first_reopen(authed_client):
    """涨停封死后首次开板采样点触发卖出（§3.5：曾封板后首次 price<涨停价）。"""
    st = authed_client.app.state
    _buy_then(st)
    _insert_order(st, order_id="co-ob", order_type="sell_open_board", direction="sell", qty=100,
                  trigger={"kind": "open_board"}, created="2026-09-08T09:29:00")
    r = eodengine.settle_account(
        st, DEMO, "2026-09-08",
        series_map={"600000": [("2026-09-08T09:31:00", 11.00),
                               ("2026-09-08T09:32:00", 11.00),
                               ("2026-09-08T09:33:00", 10.90)]},
        close_map={"600000": 10.90},
        prev_close_map={"600000": 10.0},
        board_map={"600000": "main"},
    )
    tr = _fetch(st, "SELECT * FROM trades WHERE order_id='co-ob'")
    assert len(tr) == 1
    assert tr[0]["side"] == "sell" and abs(float(tr[0]["price"]) - 10.90) < 1e-9
    assert tr[0]["basis_used"] == "l0" and tr[0]["quality"] == ""
    assert not _fetch(st, "SELECT 1 FROM holdings WHERE account_id=? AND symbol='600000'", (DEMO,))
    assert _fetch(st, "SELECT status FROM condition_orders WHERE id='co-ob'")[0]["status"] == "filled"


def test_eod_open_board_full_day_pinned_expires(authed_client):
    """全天一字/封死无开板 → 卖出不触发，today 单 expired（无半截成交）。"""
    st = authed_client.app.state
    _buy_then(st)
    _insert_order(st, order_id="co-ob2", order_type="sell_open_board", direction="sell", qty=100,
                  trigger={"kind": "open_board"}, created="2026-09-08T09:29:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-08",
        series_map={"600000": [("2026-09-08T09:31:00", 11.00),
                               ("2026-09-08T09:32:00", 11.00)]},
        close_map={"600000": 11.00},
        prev_close_map={"600000": 10.0},
        board_map={"600000": "main"},
    )
    row = _fetch(st, "SELECT status, insufficient_events FROM condition_orders WHERE id='co-ob2'")[0]
    assert row["status"] == "expired" and row["insufficient_events"] == 0
    assert not _fetch(st, "SELECT * FROM trades WHERE order_id='co-ob2'")


def test_eod_open_board_never_pinned_expires(authed_client):
    """当日未触及涨停 → 不构成'曾封板'，today 单 expired。"""
    st = authed_client.app.state
    _buy_then(st)
    _insert_order(st, order_id="co-ob3", order_type="sell_open_board", direction="sell", qty=100,
                  trigger={"kind": "open_board"}, created="2026-09-08T09:29:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-08",
        series_map={"600000": [("2026-09-08T09:31:00", 10.80),
                               ("2026-09-08T09:32:00", 10.70)]},
        close_map={"600000": 10.70},
        prev_close_map={"600000": 10.0},
        board_map={"600000": "main"},
    )
    assert _fetch(st, "SELECT status FROM condition_orders WHERE id='co-ob3'")[0]["status"] == "expired"
    assert not _fetch(st, "SELECT * FROM trades WHERE order_id='co-ob3'")


@pytest.mark.parametrize("feed", ["l1", "l2"])
def test_eod_open_board_requires_l0_invalid(authed_client, feed):
    """非 L0 档 → 显式 invalid basis_requires_l0（§4.1 矩阵事件行，不静默）。"""
    st = authed_client.app.state
    _insert_order(st, order_id=f"co-ob{feed}", order_type="sell_open_board", direction="sell",
                  qty=100, trigger={"kind": "open_board"}, created="2026-09-08T09:29:00")
    kw = {"l1_map": {"600000": [("2026-09-08T09:31:00", 10.90)]}} if feed == "l1" else {
        "l2_map": {"600000": {"high": 11.5, "low": 10.2}}}
    eodengine.settle_account(
        st, DEMO, "2026-09-08",
        close_map={"600000": 10.90},
        prev_close_map={"600000": 10.0},
        **kw,
    )
    row = _fetch(st, "SELECT status, invalid_reason, settled_on FROM condition_orders "
                     f"WHERE id='co-ob{feed}'")[0]
    assert row["status"] == "invalid"
    assert row["invalid_reason"] == "basis_requires_l0"
    assert row["settled_on"] == "2026-09-08"


def test_eod_time_l0_buy_fills_at_clock(authed_client):
    """L0 定时买入：到 at 时刻首个采样点成交（此前采样点不成交）。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-t0", order_type="time", direction="buy", qty=100,
                  trigger={"kind": "time", "at": "14:50"}, created="2026-09-08T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-08",
        series_map={"600000": [("2026-09-08T14:49:00", 10.00),
                               ("2026-09-08T14:50:00", 10.50),
                               ("2026-09-08T14:51:00", 10.60)]},
        close_map={"600000": 10.50},
    )
    tr = _fetch(st, "SELECT * FROM trades WHERE order_id='co-t0'")
    assert len(tr) == 1
    assert abs(float(tr[0]["price"]) - 10.50) < 1e-9
    assert tr[0]["trade_time"] == "2026-09-08T14:50:00" and tr[0]["basis_used"] == "l0"
    assert _fetch(st, "SELECT status FROM condition_orders WHERE id='co-t0'")[0]["status"] == "filled"


def test_eod_time_created_after_at_expires(authed_client):
    """created_at 晚于 at → 防前视不触发，today 单 expired。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-t1", order_type="time", direction="buy", qty=100,
                  trigger={"kind": "time", "at": "14:50"}, created="2026-09-08T14:55:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-08",
        series_map={"600000": [("2026-09-08T14:50:00", 10.00),
                               ("2026-09-08T14:55:00", 10.00)]},
        close_map={"600000": 10.00},
    )
    assert _fetch(st, "SELECT status FROM condition_orders WHERE id='co-t1'")[0]["status"] == "expired"
    assert not _fetch(st, "SELECT * FROM trades WHERE order_id='co-t1'")


def test_eod_time_l1_sell_at_minute_close(authed_client):
    """L1 定时卖出：at 整分钟按该分钟收盘价成交（不需价格触发穿越）。"""
    st = authed_client.app.state
    _buy_then(st)
    _insert_order(st, order_id="co-t2", order_type="time", direction="sell", qty=100,
                  trigger={"kind": "time", "at": "14:50"}, created="2026-09-08T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-08",
        l1_map={"600000": [("2026-09-08T14:49:00", 10.0, 10.2, 9.9, 10.1),
                           ("2026-09-08T14:50:00", 10.5, 10.7, 10.4, 10.6)]},
        close_map={"600000": 10.6},
        prev_close_map={"600000": 10.1},
    )
    tr = _fetch(st, "SELECT * FROM trades WHERE order_id='co-t2'")
    assert len(tr) == 1
    assert abs(float(tr[0]["price"]) - 10.6) < 1e-9
    assert tr[0]["trade_time"] == "2026-09-08T14:50:00" and tr[0]["basis_used"] == "l1"
    assert not _fetch(st, "SELECT 1 FROM holdings WHERE account_id=? AND symbol='600000'", (DEMO,))


def test_eod_time_l2_buy_at_official_close(authed_client):
    """L2 日线近似档定时买入：按官方收盘价成交（无日内分钟）。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-t3", order_type="time", direction="buy", qty=100,
                  trigger={"kind": "time", "at": "14:50"}, created="2026-09-08T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-08",
        l2_map={"600000": {"high": 11.0, "low": 10.1}},
        close_map={"600000": 10.45},
    )
    tr = _fetch(st, "SELECT * FROM trades WHERE order_id='co-t3'")
    assert len(tr) == 1
    assert abs(float(tr[0]["price"]) - 10.45) < 1e-9
    assert tr[0]["basis_used"] == "l2"


def test_eod_time_malformed_trigger_invalid(authed_client):
    """at 非法/kind 不符 → 定时单 invalid（trigger_schema）。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-t4", order_type="time", direction="buy", qty=100,
                  trigger={"kind": "time", "at": "2500"}, created="2026-09-08T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-08",
        series_map={"600000": [("2026-09-08T14:50:00", 10.00)]},
        close_map={"600000": 10.00},
    )
    row = _fetch(st, "SELECT status, invalid_reason FROM condition_orders WHERE id='co-t4'")[0]
    assert row["status"] == "invalid" and row["invalid_reason"] == "trigger_schema"


def _empty_settle(st, day):
    """无订单的空结算：仅推进 settlement_log 会话日序（卖出跟踪 N 日计数用）。"""
    eodengine.settle_account(st, DEMO, day, close_map={})


def _exit_day(st, day, close=None, *, high=None, low=None, bench=None, track_days=None):
    _empty_settle(st, day)
    mkt: dict[str, dict] = {}
    if close is not None:
        md = {"close": close}
        if high is not None:
            md["high"] = high
        if low is not None:
            md["low"] = low
        mkt["600000"] = md
    if bench is not None:
        mkt[eodengine.EXIT_BENCH_SYMBOL] = {"close": bench}
    eodengine.settle_exits(st, DEMO, day, market=mkt, track_days=track_days)


def _sell_all(st, *, day="2026-09-02", px=11.0, reason="主动", bench=3000.0):
    _insert_order(st, order_id="co-ex-s", order_type="sell_take_profit", direction="sell",
                  qty=100, trigger={"op": "ge", "price": 5.0}, created=f"{day}T09:00:00",
                  reason=reason)
    eodengine.settle_account(
        st, DEMO, day,
        series_map={"600000": [(f"{day}T09:31:00", px)]},
        close_map={"600000": px},
        prev_close_map={"600000": 10.0},
        exit_market={eodengine.EXIT_BENCH_SYMBOL: {"close": bench}},
    )


def test_eod_exit_register_track_and_close_early(authed_client):
    """卖出登记 → 逐结算日推进极值 → 第 10 交易日确定性结清（卖早 + 损失案例标注）。"""
    st = authed_client.app.state
    _buy_then(st, day1="2026-09-01")
    _sell_all(st, day="2026-09-02", px=11.0, reason="止盈", bench=3000.0)
    rows = _fetch(st, "SELECT * FROM exit_trackings WHERE account_id=?", (DEMO,))
    assert len(rows) == 1
    r = rows[0]
    assert r["sell_date"] == "2026-09-02" and r["status"] == "tracking"
    assert abs(float(r["sell_price"]) - 11.0) < 1e-9
    assert r["sell_reason"] == "止盈" and r["bench_sell_close"] == 3000.0

    for i in range(3, 13):                       # 09-03..09-12 共 10 个卖后交易日
        day = f"2026-09-{i:02d}"
        kw = {"close": 12.0}
        if i == 4:
            kw.update(high=13.0, low=11.0)       # 推进期极值：高点冲高、回落
        if i == 5:
            kw.update(high=12.0, low=10.2)
        _exit_day(st, day, bench=3090.0, **kw)
    rows = _fetch(st, "SELECT * FROM exit_trackings WHERE account_id=?", (DEMO,))
    assert len(rows) == 1 and rows[0]["status"] == "done"
    r = rows[0]
    assert r["conclusion"] == "卖早" and r["is_loss_case"] == 1
    assert abs(float(r["fwd_return_pct"]) - 9.0909) < 1e-3   # (12-11)/11
    assert abs(float(r["bench_return_pct"]) - 3.0) < 1e-6
    assert abs(float(r["excess_pct"]) - 6.0909) < 1e-3
    assert abs(float(r["period_high"]) - 13.0) < 1e-9
    assert abs(float(r["period_low"]) - 10.2) < 1e-9
    assert r["track_end_date"] == "2026-09-12" and r["quality"] == ""

    again = eodengine.settle_exits(
        st, DEMO, "2026-09-12",
        market={"600000": {"close": 12.0}, eodengine.EXIT_BENCH_SYMBOL: {"close": 3090.0}},
    )
    assert again["closed"] == 0                 # 幂等：不重复结清/推进


def test_eod_exit_sell_right_boundary(authed_client):
    """阈值边界：fwd/excess = −3% 整 → 卖对。"""
    st = authed_client.app.state
    _buy_then(st, day1="2026-09-01")
    _sell_all(st, day="2026-09-02", px=11.0)
    _exit_day(st, "2026-09-03", close=11.0, bench=3000.0, track_days=2)
    _exit_day(st, "2026-09-04", close=10.67, bench=3000.0, track_days=2)   # (10.67-11)/11 = -3.0%
    r = _fetch(st, "SELECT * FROM exit_trackings WHERE account_id=?", (DEMO,))[0]
    assert r["status"] == "done" and r["conclusion"] == "卖对"
    assert abs(float(r["fwd_return_pct"]) + 3.0) < 1e-6 and r["is_loss_case"] == 0


def test_eod_exit_stale_close_when_price_missing(authed_client):
    """结清日个股官方价缺失 → 按最近可得价 stale 结清（不虚构价，quality 标注）。"""
    st = authed_client.app.state
    _buy_then(st, day1="2026-09-01")
    _sell_all(st, day="2026-09-02", px=11.0)
    _exit_day(st, "2026-09-03", close=10.9, bench=3000.0, track_days=3)
    _exit_day(st, "2026-09-04", close=10.8, bench=3000.0, track_days=3)
    _exit_day(st, "2026-09-05", bench=3000.0, track_days=3)                 # 当日股票价缺 → stale
    r = _fetch(st, "SELECT * FROM exit_trackings WHERE account_id=?", (DEMO,))[0]
    assert r["status"] == "done" and r["quality"] == "stale_close"
    assert r["conclusion"] == "卖平"                          # -1.82% 区间
    assert abs(float(r["fwd_return_pct"]) + 1.8182) < 1e-3


def test_eod_partial_sell_zero_lot_leaves_remaining(authed_client):
    """卖出可零股（§3.7）：100 股持仓分 50 股卖出 → FIFO 余 50，不拒单。"""
    st = authed_client.app.state
    _buy_then(st)
    _insert_order(st, order_id="co-zt", order_type="sell_take_profit", direction="sell", qty=50,
                  trigger={"op": "ge", "price": 5.0}, created="2026-09-08T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-08",
        series_map={"600000": [("2026-09-08T09:31:00", 11.0)]},
        close_map={"600000": 11.0},
        prev_close_map={"600000": 10.0},
    )
    lot = _fetch(
        st,
        "SELECT l.remaining FROM lots l JOIN holdings h ON h.id=l.holding_id"
        " WHERE l.account_id=? AND h.symbol='600000'",
        (DEMO,),
    )[0]
    assert lot["remaining"] == 50
    h = _fetch(st, "SELECT quantity FROM holdings WHERE account_id=? AND symbol='600000'", (DEMO,))[0]
    assert h["quantity"] == 50


def _buy_holding(st, *, day="2026-09-01", order_id="co-fz-base", px=10.0):
    _insert_order(st, order_id=order_id, order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 10.5}, created=f"{day}T09:00:00", validity="long")
    eodengine.settle_account(
        st, DEMO, day,
        series_map={"600000": [(f"{day}T09:31:00", px)]},
        close_map={"600000": px},
    )


def test_eod_circuit_freeze_blocks_buys_keeps_active_then_resumes(authed_client):
    """熔断日买入冻结（§7 D5）：买入不成交/不推进/标事件，卖出照常；多日冻结逐日计数；解冻恢复。"""
    st = authed_client.app.state
    _buy_holding(st)                                    # 09-01 建仓 100 股（供保护性卖出）
    # 09-07 熔断日：保护性卖单 + 长期买入单 + 当日买入单
    _insert_order(st, order_id="co-fz-sell", order_type="sell_take_profit", direction="sell",
                  qty=100, trigger={"op": "ge", "price": 9.0}, created="2026-09-07T09:00:00")
    _insert_order(st, order_id="co-fz2", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 10.5}, created="2026-09-07T09:00:00", validity="long")
    _insert_order(st, order_id="co-fz3", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 10.5}, created="2026-09-07T09:00:00")
    r = eodengine.settle_account(
        st, DEMO, "2026-09-07",
        series_map={"600000": [("2026-09-07T09:31:00", 9.5)]},
        close_map={"600000": 9.5},
        prev_close_map={"600000": 10.0},
        circuit_freeze=True,
    )
    assert r["circuit_blocked"] == 2                    # co-fz2 + co-fz3 被冻结
    assert _fetch(st, "SELECT * FROM trades WHERE order_id='co-fz-sell'")[0]["side"] == "sell"
    assert _fetch(st, "SELECT * FROM trades WHERE order_id='co-fz2'") == []
    assert _fetch(st, "SELECT * FROM trades WHERE order_id='co-fz3'") == []
    o2 = _fetch(st, "SELECT status, circuit_break_events, insufficient_events, invalid_reason"
                    " FROM condition_orders WHERE id='co-fz2'")[0]
    assert o2["status"] == "active" and o2["circuit_break_events"] == 1
    assert o2["insufficient_events"] == 0 and o2["invalid_reason"] == ""
    o3 = _fetch(st, "SELECT status, circuit_break_events FROM condition_orders WHERE id='co-fz3'")[0]
    assert o3["status"] == "expired" and o3["circuit_break_events"] == 1   # today 单窗口已过照常到期
    aud = _fetch(st, "SELECT COUNT(*) AS n FROM audit_logs WHERE action='trade.circuit_break'"
                     " AND object_id IN ('co-fz2','co-fz3')")[0]["n"]
    assert aud == 2
    # 次日仍熔断：long 单不推进，事件再计一次
    eodengine.settle_account(
        st, DEMO, "2026-09-08",
        series_map={"600000": [("2026-09-08T09:31:00", 9.5)]},
        close_map={"600000": 9.5},
        prev_close_map={"600000": 9.5},
        circuit_freeze=True,
    )
    o2 = _fetch(st, "SELECT status, circuit_break_events FROM condition_orders WHERE id='co-fz2'")[0]
    assert o2["status"] == "active" and o2["circuit_break_events"] == 2
    assert _fetch(st, "SELECT * FROM trades WHERE order_id='co-fz2'") == []
    # 解冻日：挂单恢复参与 → 条件触达成交，事件计数保留
    eodengine.settle_account(
        st, DEMO, "2026-09-09",
        series_map={"600000": [("2026-09-09T09:31:00", 9.5)]},
        close_map={"600000": 9.5},
        prev_close_map={"600000": 9.5},
    )
    tr = _fetch(st, "SELECT * FROM trades WHERE order_id='co-fz2'")
    assert len(tr) == 1 and tr[0]["side"] == "buy"
    o2 = _fetch(st, "SELECT status, circuit_break_events FROM condition_orders WHERE id='co-fz2'")[0]
    assert o2["status"] == "filled" and o2["circuit_break_events"] == 2


def test_eod_long_buy_insufficient_keeps_active_then_recheck_next_day(authed_client):
    """D2 跨日复判：long 买入单资金不足日终保持 active（不 expired/不填），次日价格回落资金
    满足 → 复判成交（spec-01 §2.3：日终仍不足但 active（long 单）继续跨日）。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-ins-xd", order_type="buy", direction="buy", qty=20000,
                  trigger={"op": "le", "price": 7.5}, created="2026-09-01T09:00:00", validity="long")
    r = eodengine.settle_account(
        st, DEMO, "2026-09-01",
        series_map={"600000": [("2026-09-01T09:31:00", 7.5)]},
        close_map={"600000": 7.5},
    )
    assert r["circuit_blocked"] == 0
    assert _fetch(st, "SELECT * FROM trades WHERE order_id='co-ins-xd'") == []
    o = _fetch(st, "SELECT status, insufficient_events, settled_on FROM condition_orders"
                   " WHERE id='co-ins-xd'")[0]
    assert o["status"] == "active" and o["insufficient_events"] >= 1 and o["settled_on"] == ""
    # 次日价格跌至资金可覆盖 → 复判成交（金额 ~4.5×20000=90000 < 余额）
    eodengine.settle_account(
        st, DEMO, "2026-09-02",
        series_map={"600000": [("2026-09-02T09:31:00", 4.5)]},
        close_map={"600000": 4.5},
    )
    tr = _fetch(st, "SELECT * FROM trades WHERE order_id='co-ins-xd'")
    assert len(tr) == 1 and tr[0]["side"] == "buy" and abs(float(tr[0]["price"]) - 4.5) < 1e-9
    o = _fetch(st, "SELECT status, insufficient_events FROM condition_orders WHERE id='co-ins-xd'")[0]
    assert o["status"] == "filled" and o["insufficient_events"] >= 1


def test_eod_long_sell_t1_insufficient_keeps_active_then_recheck_next_day(authed_client):
    """T+1 卖出跨日复判：当日买入 lot 当日卖不可卖（insufficient，保持 active），次日 T+1
    解冻 → 可卖满足复判成交（spec-01 §6.2 / §2.3 可卖不足语义，卖出可零股侧）。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-t1s-b", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 10.5}, created="2026-09-01T09:00:00", validity="long")
    _insert_order(st, order_id="co-t1s-s", order_type="sell_stop", direction="sell", qty=100,
                  trigger={"op": "ge", "price": 9.0}, created="2026-09-01T09:00:00", validity="long")
    eodengine.settle_account(
        st, DEMO, "2026-09-01",
        series_map={"600000": [("2026-09-01T09:31:00", 9.5)]},
        close_map={"600000": 9.5},
    )
    assert _fetch(st, "SELECT * FROM trades WHERE order_id='co-t1s-s'") == []
    o = _fetch(st, "SELECT status, insufficient_events FROM condition_orders WHERE id='co-t1s-s'")[0]
    assert o["status"] == "active" and o["insufficient_events"] >= 1   # T+1：当日新 lot 不可卖
    # 次日 lot 解冻 → 卖出复判成交
    eodengine.settle_account(
        st, DEMO, "2026-09-02",
        series_map={"600000": [("2026-09-02T09:31:00", 9.5)]},
        close_map={"600000": 9.5},
        prev_close_map={"600000": 9.5},        # 期初持仓按前收估值（首日已建仓）
    )
    tr = _fetch(st, "SELECT * FROM trades WHERE order_id='co-t1s-s'")
    assert len(tr) == 1 and tr[0]["side"] == "sell"
    o = _fetch(st, "SELECT status, insufficient_events FROM condition_orders WHERE id='co-t1s-s'")[0]
    assert o["status"] == "filled" and o["insufficient_events"] >= 1


def _set_cap(st, cap) -> None:
    conn = state_conn(st)
    with conn:
        conn.execute("UPDATE accounts SET single_stock_cap=? WHERE id=?", (cap, DEMO))


def test_eod_default_cap_100pct_full_cash_buy_fills(authed_client):
    """§7 硬红线默认上限 1.00（满仓策略）：全现金单票买入恒通过，不误伤满仓成交。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-cap-100", order_type="buy", direction="buy", qty=9900,
                  trigger={"op": "le", "price": 10.05}, created="2026-09-07T09:00:00")
    r = eodengine.settle_account(
        st, DEMO, "2026-09-07",
        series_map={"600000": [("2026-09-07T09:31:00", 10.00)]},
        close_map={"600000": 10.00},
    )
    assert r["already_settled"] is False
    tr = _fetch(st, "SELECT * FROM trades WHERE order_id='co-cap-100'")
    assert len(tr) == 1 and tr[0]["qty"] == 9900 and tr[0]["price"] == 10.0
    o = _fetch(st, "SELECT status, insufficient_events FROM condition_orders WHERE id='co-cap-100'")[0]
    assert o["status"] == "filled" and o["insufficient_events"] == 0


def test_eod_single_stock_cap_blocks_overlimit_buy(authed_client):
    """§7 事故性单票上限（cap<1 可配）：成交后单票市值将超红线 → 拒采样点、记事件、
    today 单到期 expired 且零成交、现金分毫不动。"""
    st = authed_client.app.state
    _set_cap(st, 0.2)
    _insert_order(st, order_id="co-cap-lo", order_type="buy", direction="buy", qty=9900,
                  trigger={"op": "le", "price": 10.5}, created="2026-09-07T09:00:00")
    cash_before = _cash(st)
    r = eodengine.settle_account(
        st, DEMO, "2026-09-07",
        series_map={"600000": [
            ("2026-09-07T09:31:00", 10.20), ("2026-09-07T09:40:00", 10.00),
        ]},
        close_map={"600000": 10.00},
    )
    assert r["already_settled"] is False
    assert _fetch(st, "SELECT * FROM trades WHERE order_id='co-cap-lo'") == []
    o = _fetch(st, "SELECT status, insufficient_events FROM condition_orders WHERE id='co-cap-lo'")[0]
    assert o["status"] == "expired" and o["insufficient_events"] == 2
    assert _cash(st) == cash_before


def test_eod_single_stock_cap_preholding_relative(authed_client):
    """cap 相对既有持仓计：期初已持 6000 股（60% 权益），再买使单票市值超 50% 红线 → 拒；
    上限调回 1.00 后同条件可成交。"""
    st = authed_client.app.state
    _set_cap(st, 0.5)
    conn = state_conn(st)
    with conn:
        conn.execute(
            "INSERT INTO holdings(id, account_id, symbol, quantity, avg_cost, updated_ts)"
            " VALUES ('cap-h', ?, '600000', 6000, 9.9, '2026-09-07T09:00:00')",
            (DEMO,),
        )
        conn.execute(
            "UPDATE accounts SET cash=40000 WHERE id=?", (DEMO,),
        )
    _insert_order(st, order_id="co-cap-pre", order_type="buy", direction="buy", qty=4000,
                  trigger={"op": "le", "price": 10.05}, created="2026-09-07T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-07",
        series_map={"600000": [("2026-09-07T09:31:00", 10.0)]},
        close_map={"600000": 10.0},
        prev_close_map={"600000": 9.9},
    )
    assert _fetch(st, "SELECT * FROM trades WHERE order_id='co-cap-pre'") == []
    o = _fetch(st, "SELECT status, insufficient_events FROM condition_orders WHERE id='co-cap-pre'")[0]
    assert o["status"] == "expired" and o["insufficient_events"] == 1


def test_eod_suspended_today_expires_long_kept_without_series(authed_client):
    """停牌日（spec-01 §3.3 #54）：无当日序列但有 suspend_map 估值 → 订单不判——
    today 单到期 expired、long 单保持 active（复牌日恢复判定），零成交零事件，不报 gap。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-susp-t", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 10.05}, created="2026-09-07T09:00:00")
    _insert_order(st, order_id="co-susp-l", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 10.05}, created="2026-09-07T09:00:00",
                  validity="long")
    r = eodengine.settle_account(
        st, DEMO, "2026-09-07",
        series_map={}, close_map={}, suspend_map={"600000": 9.5},
    )
    assert r["already_settled"] is False
    assert r["suspended_symbols"] == ["600000"]
    assert _fetch(st, "SELECT * FROM trades WHERE account_id=?", (DEMO,)) == []
    o = _fetch(st, "SELECT status, insufficient_events, settled_on FROM condition_orders WHERE id='co-susp-t'")[0]
    assert o["status"] == "expired" and o["insufficient_events"] == 0 and o["settled_on"] == "2026-09-07"
    o = _fetch(st, "SELECT status, insufficient_events, settled_on FROM condition_orders WHERE id='co-susp-l'")[0]
    assert o["status"] == "active" and o["insufficient_events"] == 0 and o["settled_on"] == ""


def test_eod_suspended_holding_valued_at_last_close(authed_client):
    """停牌持仓估值：按停牌前最近官方收盘（数据侧真实历史价）估值计入 NAV/对账守恒，
    不虚构替代价；当日无成交、现金不动。"""
    st = authed_client.app.state
    conn = state_conn(st)
    with conn:
        conn.execute(
            "INSERT INTO holdings(id, account_id, symbol, quantity, avg_cost, updated_ts)"
            " VALUES ('sus-h', ?, '600000', 1000, 9.0, '2026-09-07T09:00:00')",
            (DEMO,),
        )
    r = eodengine.settle_account(
        st, DEMO, "2026-09-07",
        series_map={}, close_map={},
        prev_close_map={"600000": 9.4},
        suspend_map={"600000": 9.5},
    )
    assert r["already_settled"] is False and r["suspended_symbols"] == ["600000"]
    assert r["cash"] == "100000.00" and r["nav"] == "1.0950" and r["today_pnl"] == "100.00"
    assert abs(float(r["total_pnl"]) - 9500.0) < 0.01
    assert state_conn(st).execute(
        "SELECT COUNT(*) FROM trades WHERE account_id=?", (DEMO,)
    ).fetchone()[0] == 0


def test_eod_suspended_symbol_does_not_block_other_fills(authed_client):
    """票级隔离：同账户停牌票挂起不影响可交易票当日成交，也不把停牌误报为数据缺口。"""
    st = authed_client.app.state
    _insert_order(st, order_id="co-susp-b", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 10.05}, created="2026-09-07T09:00:00")
    _insert_order(st, order_id="co-ok-b", order_type="buy", direction="buy", qty=100,
                  trigger={"op": "le", "price": 10.05}, symbol="600519",
                  created="2026-09-07T09:00:00")
    r = eodengine.settle_account(
        st, DEMO, "2026-09-07",
        series_map={"600519": [("2026-09-07T09:31:00", 10.0)]},
        close_map={"600519": 10.0}, suspend_map={"600000": 9.5},
    )
    assert r["already_settled"] is False and r["suspended_symbols"] == ["600000"]
    trs = _fetch(st, "SELECT order_id FROM trades WHERE account_id=?", (DEMO,))
    assert [t["order_id"] for t in trs] == ["co-ok-b"]
    assert _fetch(st, "SELECT status FROM condition_orders WHERE id='co-susp-b'")[0]["status"] == "expired"

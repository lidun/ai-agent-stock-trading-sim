"""信号注册表切片测试（spec-01 §2.5/§8：登记、质量传播、trial 隔离、N 日前瞻结算）。"""
from __future__ import annotations

import json

from core import accountstore, eodengine, signalstore
from core.db import state_conn, write_txn

DEMO = "agent-demo-001"


def _rows(state, sql, args=()):
    return state_conn(state).execute(sql, args).fetchall()


def _insert_order(state, *, account_id, order_id, order_type, direction, qty, trigger,
                  symbol="600000", created="2026-09-07T09:00:00", reason=""):
    conn = state_conn(state)
    with write_txn(conn) as c:
        c.execute(
            """
            INSERT INTO condition_orders(id, account_id, order_type, direction, scope, symbol,
                trigger, basis, price_ref, qty, price_type, validity, priority, status,
                created_at, creator, reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (order_id, account_id, order_type, direction, "single", symbol,
             json.dumps(trigger), "replay_l0", "absolute", qty, "limit", "today", 0,
             "active", created, account_id, reason),
        )


def _buy_day(state, account_id=DEMO, *, day="2026-09-07", oid="co-sig-b", price=10.05):
    _insert_order(state, account_id=account_id, order_id=oid, order_type="buy",
                  direction="buy", qty=100, trigger={"op": "le", "price": price},
                  created=f"{day}T09:00:00")
    return eodengine.settle_account(
        state, account_id, day,
        series_map={"600000": [(f"{day}T09:31:00", price + 0.15),
                               (f"{day}T09:32:00", price - 0.05)]},
        close_map={"600000": price + 0.85},
    )


def test_buy_signal_registered_with_degraded_quality(authed_client):
    """买入成交 → signal_registry 落 buy 信号，quality 取票级 degraded_reason（传播）。"""
    st = authed_client.app.state
    _buy_day(st)
    rows = _rows(st, "SELECT * FROM signal_registry WHERE account_id=? AND sig_type='buy'",
                 (DEMO,))
    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "600000" and r["reg_date"] == "2026-09-07"
    assert abs(float(r["ref_price"]) - 10.90) < 1e-9   # 登记日官方收盘为前瞻基准
    assert r["fwd_end_date"] == "" and r["trial_flag"] == 0
    assert r["strategy_version_no"] == ""


def test_buy_signal_quality_from_degraded_marks(authed_client):
    """票级 degraded 标记经 settlement 传给信号 quality（spec-03 §7 传播到注册表）。"""
    st = authed_client.app.state
    _insert_order(st, account_id=DEMO, order_id="co-sig-q", order_type="buy",
                  direction="buy", qty=100, trigger={"op": "le", "price": 10.05},
                  created="2026-09-07T09:00:00")
    eodengine.settle_account(
        st, DEMO, "2026-09-07",
        series_map={"600000": [("2026-09-07T09:31:00", 10.20),
                               ("2026-09-07T09:32:00", 10.00)]},
        close_map={"600000": 10.90},
        quality_marks={"600000": {"quality": "degraded",
                                  "degraded_reason": "coverage_gap", "notes": "缺 5 根"}},
    )
    r = _rows(st, "SELECT * FROM signal_registry WHERE account_id=? AND sig_type='buy'",
              (DEMO,))[0]
    assert r["quality"] == "coverage_gap"


def test_sell_signal_registered_alongside_exit_tracking(authed_client):
    """卖出成交 → 同时登记 sell 信号与 exit_trackings（§8.1 退出信号特例）。"""
    st = authed_client.app.state
    _buy_day(st, day="2026-09-01", oid="co-sig-b1", price=10.0)
    _insert_order(st, account_id=DEMO, order_id="co-sig-s", order_type="sell_take_profit",
                  direction="sell", qty=100, trigger={"op": "ge", "price": 5.0},
                  created="2026-09-02T09:00:00", reason="止盈")
    eodengine.settle_account(
        st, DEMO, "2026-09-02",
        series_map={"600000": [("2026-09-02T09:31:00", 11.0)]},
        close_map={"600000": 11.0}, prev_close_map={"600000": 10.0},
        exit_market={eodengine.EXIT_BENCH_SYMBOL: {"close": 3000.0}},
    )
    sig = _rows(st, "SELECT * FROM signal_registry WHERE account_id=? AND sig_type='sell'",
                (DEMO,))
    assert len(sig) == 1
    assert sig[0]["reg_date"] == "2026-09-02"
    assert abs(float(sig[0]["ref_price"]) - 11.0) < 1e-9
    assert len(_rows(st, "SELECT 1 FROM exit_trackings WHERE account_id=?", (DEMO,))) == 1


def test_trial_account_signals_flagged(authed_client):
    """试运行账户信号 trial_flag=1（spec-05 统计默认排除，主账户=0）。"""
    st = authed_client.app.state
    accountstore.create_trial_agent(st, agent_id="agent-sig-trial", name="信号试运行",
                                    window_days=5)
    tid = "agent-sig-trial.trial"
    _buy_day(st, account_id=tid, oid="co-sig-tb", price=10.05)
    r = _rows(st, "SELECT * FROM signal_registry WHERE account_id=?", (tid,))[0]
    assert r["trial_flag"] == 1


def test_candidate_signal_n_day_forward_return(authed_client):
    """候选信号经 N 个交易日结算写入前瞻收益，登记日基准为 ref_price。"""
    st = authed_client.app.state
    signalstore.register_candidate(st, DEMO, "600000", "2026-09-01",
                                   concept_tag="高股息", ref_price=10.0)
    for i in range(2, 12):                     # 09-02..09-11 共 10 个交易日
        out = signalstore.settle_due(st, DEMO, f"2026-09-{i:02d}",
                                     market={"600000": {"close": 11.0}})
    assert out["closed"] == 1
    r = _rows(st, "SELECT * FROM signal_registry WHERE account_id=?", (DEMO,))[0]
    assert r["fwd_end_date"] == "2026-09-11"
    assert abs(float(r["fwd_return_pct"]) - 10.0) < 1e-6   # (11-10)/10*100
    assert not any(x == "stale_close" for x in r["quality"].split("+"))
    # 已结清不再重复推进
    assert signalstore.settle_due(st, DEMO, "2026-09-12",
                                  market={"600000": {"close": 12.0}})["updated"] == 0


def test_signal_settle_stale_close_when_due_day_missing(authed_client):
    """到期日缺价但此前有最近可得价 → stale_close 结清（不虚构价）。"""
    st = authed_client.app.state
    signalstore.register_candidate(st, DEMO, "600000", "2026-09-01", ref_price=10.0)
    for i in range(2, 11):                     # 前 9 个交易日有价
        signalstore.settle_due(st, DEMO, f"2026-09-{i:02d}",
                               market={"600000": {"close": 11.0}})
    out = signalstore.settle_due(st, DEMO, "2026-09-11", market={})   # 到期日缺价
    assert out["closed"] == 1
    r = _rows(st, "SELECT * FROM signal_registry WHERE account_id=?", (DEMO,))[0]
    assert "stale_close" in r["quality"].split("+")
    assert abs(float(r["fwd_return_pct"]) - 10.0) < 1e-6


def test_settle_due_without_ref_stays_pending(authed_client):
    """无登记日基准价的候选信号保持在途（不虚构收益），补基准后才结清。"""
    st = authed_client.app.state
    signalstore.register_candidate(st, DEMO, "600000", "2026-09-01")   # 未给 ref_price
    for i in range(2, 12):
        signalstore.settle_due(st, DEMO, f"2026-09-{i:02d}",
                               market={"600000": {"close": 11.0}})
    r = _rows(st, "SELECT * FROM signal_registry WHERE account_id=?", (DEMO,))[0]
    assert r["fwd_end_date"] == "" and int(r["sessions_done"]) == 0
    # 提供登记日基准价（base_prices 兜底）→ 正常推进并结清
    signalstore.settle_due(st, DEMO, "2026-09-12",
                           market={"600000": {"close": 11.0}},
                           base_prices={"600000": 10.0})
    assert float(_rows(st, "SELECT * FROM signal_registry WHERE account_id=?",
                       (DEMO,))[0]["ref_price"]) == 10.0


def test_scheduler_advances_inflight_signals(authed_client):
    """调度器在结算会话日推进在途信号（_advance_signals 接线，缺价不虚构）。"""
    from _feedkit import FakeFeed
    from core.settle_scheduler import EodSettleTrigger
    st = authed_client.app.state
    signalstore.register_candidate(st, DEMO, "600000", "2026-08-31", ref_price=10.0)
    trig = EodSettleTrigger(st, feed=FakeFeed())
    accounts, symbols = trig._signal_state("2026-09-04")
    assert accounts == [DEMO] and symbols == ["600000"]
    out = trig._advance_signals("2026-09-04")
    assert out["updated"] == 1 and out["closed"] == 0
    r = _rows(st, "SELECT * FROM signal_registry WHERE account_id=?", (DEMO,))[0]
    assert int(r["sessions_done"]) == 1 and r["last_seen"] == "2026-09-04"
    assert float(r["last_close"]) > 0


def test_list_signals_filters_and_counts(authed_client):
    """列表只读接口：按类型/结清态过滤，settled 判定=fwd_end_date 非空。"""
    st = authed_client.app.state
    signalstore.register_candidate(st, DEMO, "600000", "2026-09-01", ref_price=10.0)
    signalstore.register_pitfall(st, DEMO, "600000", "2026-09-02",
                                 pitfall_id="KB-1", exception=1, ref_price=10.5)
    all_sig = signalstore.list_signals(st, DEMO)
    assert all_sig["total"] == 2
    pits = signalstore.list_signals(st, DEMO, sig_type="pitfall_intercept")
    assert pits["total"] == 1 and pits["items"][0]["exception"] is True
    assert pits["items"][0]["pitfall_id"] == "KB-1"
    assert len(signalstore.list_signals(st, DEMO, settled=False)["items"]) == 2
    assert signalstore.list_signals(st, DEMO, settled=True)["total"] == 0

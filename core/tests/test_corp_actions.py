"""除权日送转（split）存量 lot 同步重写切片测试（spec-01 §2.2/§6.6/§10）。

送转在结算事务首步确定性应用：buy_date < ex_date 存量 lot 数量 ×(1+s) 向下取整到股、
成本 ÷(1+s) 摊薄、holdings 重建、corp_action_flags 幂等 tag、审计留痕；当日新买入 lot
不受影响；dividend 等未实现 kind 显式 EngineGapError 且整事务回滚（不虚构入账）。
"""
from __future__ import annotations

import json

import pytest

from core import eodengine, settle_day
from core.db import state_conn, write_txn
from _feedkit import MultiDayL2Feed

DEMO = "agent-demo-001"


def _row(d: str) -> dict:
    from pathlib import Path
    from core import quotes_tencent as q
    rows = q.parse_day_rows(
        (Path(__file__).parent / "fixtures" / "tencent_day_sh600000.json").read_text("utf-8")
    )
    return [r for r in rows if str(r["date"]) == d][0]


def _insert_order(state, *, order_id, order_type, direction, qty, trigger, day,
                  validity="today"):
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
                order_id, DEMO, order_type, direction, "single", "600000",
                json.dumps(trigger), "replay_l0", "absolute", qty, "limit", validity,
                0, "active", f"{day}T09:00:00", "agent-demo-001", "送转切片测试",
            ),
        )


def _fetch(state, sql, args=()):
    conn = state_conn(state)
    with conn:
        return conn.execute(sql, args).fetchall()


def _buy(state, *, day="2026-09-07", qty=1000, price=10.0):
    """引擎直连买入成交（series 等值采样触发限价，官方收盘同价）。"""
    _insert_order(state, order_id=f"b-{day}", order_type="buy", direction="buy",
                  qty=qty, trigger={"op": "le", "price": price}, day=day)
    r = eodengine.settle_account(
        state, DEMO, day,
        series_map={"600000": [(f"{day}T09:31:00", price), (f"{day}T09:32:00", price)]},
        close_map={"600000": price}, prev_close_map={"600000": price},
    )
    assert not r["already_settled"]


def test_corp_split_rewrites_existing_lots(authed_client):
    """买入 1000@10 → ex 日 1:1 送转：数量×2、成本÷2、holdings 重建、幂等 tag、审计留痕。"""
    st = authed_client.app.state
    _buy(st)
    day = "2026-09-08"
    r = eodengine.settle_account(
        st, DEMO, day,
        close_map={"600000": 5.0}, prev_close_map={"600000": 10.0},
        corp_events={"600000": {"kind": "split", "ratio": 1}},
    )
    assert r["corp_applied"] == 1 and r["corp_symbols"] == ["600000"]
    conn = state_conn(st)
    lot = conn.execute("SELECT * FROM lots WHERE account_id=? AND buy_date='2026-09-07'",
                       (DEMO,)).fetchone()
    assert lot["quantity"] == 2000 and lot["remaining"] == 2000 and lot["buy_price"] == 5.0
    tags = json.loads(lot["corp_action_flags"])
    assert "split:2026-09-08:600000" in tags
    h = conn.execute(
        "SELECT quantity, avg_cost FROM holdings WHERE account_id=? AND symbol='600000'",
        (DEMO,)).fetchone()
    assert h["quantity"] == 2000
    # 买入费 5.1（佣金 5 + 过户 0.1）→ 含费 avg 10.0051 ÷2 = 5.00255 → 成本位舍入 5.0026
    assert abs(h["avg_cost"] - 5.0026) < 1e-4
    au = conn.execute(
        "SELECT detail FROM audit_logs WHERE action='corp_action.split'").fetchall()
    assert len(au) == 1
    d = json.loads(au[0]["detail"])
    assert d["symbol"] == "600000" and d["ratio"] == "1"
    assert d["lots"][0]["qty"] == ["1000", "2000"]
    assert d["lots"][0]["price"] == ["10", "5"]
    assert d["qty_tail"] == "0"


def test_corp_split_then_t1_sell_uses_rewritten_remaining(authed_client):
    """送转后 T+1 卖出 2000 股全额核销（remaining 重写贯通卖出可卖数）；现金按金额−费结算。"""
    st = authed_client.app.state
    _buy(st, qty=1000, price=10.0)
    ex = "2026-09-08"
    r = eodengine.settle_account(
        st, DEMO, ex, close_map={"600000": 5.0}, prev_close_map={"600000": 10.0},
        corp_events={"600000": {"kind": "split", "ratio": 1}},
    )
    assert r["corp_applied"] == 1
    day3 = "2026-09-09"
    _insert_order(st, order_id="sell-split", order_type="sell_take_profit", direction="sell",
                  qty=2000, trigger={"op": "ge", "price": 5.0}, day=day3)
    r3 = eodengine.settle_account(
        st, DEMO, day3,
        series_map={"600000": [(f"{day3}T09:31:00", 5.0), (f"{day3}T09:32:00", 5.0)]},
        close_map={"600000": 5.0}, prev_close_map={"600000": 5.0},
    )
    assert not r3["already_settled"]
    conn = state_conn(st)
    tr = conn.execute("SELECT * FROM trades WHERE order_id='sell-split'").fetchone()
    assert tr["side"] == "sell" and tr["qty"] == 2000 and tr["price"] == 5.0
    # 重写后 remaining 2000 全数核销 → 空 lot/holding 清理（既有卖核销语义）
    lots = conn.execute("SELECT * FROM lots WHERE account_id=?", (DEMO,)).fetchall()
    assert lots == []
    h = conn.execute(
        "SELECT quantity FROM holdings WHERE account_id=? AND symbol='600000'", (DEMO,)
    ).fetchone()
    assert h is None or h["quantity"] == 0
    # 买入扣 10005.1（金额 10000 + 费 5.1）→ 卖出收 9989.9（金额 10000 − 费 10.1：
    # 佣金 5 + 印花 5 + 过户 0.1）
    cash = conn.execute("SELECT cash FROM accounts WHERE id=?", (DEMO,)).fetchone()[0]
    assert abs(cash - (100000.0 - 10005.1 + 9989.9)) < 0.01


def test_corp_split_event_level_idempotent(authed_client):
    """事件级幂等：同日同事件重跑（绕过 settle_key 幂等）→ 不再重写、数量不再翻倍。"""
    st = authed_client.app.state
    _buy(st)
    ex = "2026-09-08"
    corp = {"600000": {"kind": "split", "ratio": 1}}
    r1 = eodengine.settle_account(st, DEMO, ex, close_map={"600000": 5.0},
                                   prev_close_map={"600000": 10.0}, corp_events=corp)
    assert r1["corp_applied"] == 1
    conn = state_conn(st)
    with write_txn(conn) as c:
        c.execute("DELETE FROM settlement_log WHERE settle_key=?", (f"{ex}:{DEMO}",))
    r2 = eodengine.settle_account(st, DEMO, ex, close_map={"600000": 5.0},
                                   prev_close_map={"600000": 10.0}, corp_events=corp)
    assert r2["corp_applied"] == 0 and r2["corp_symbols"] == []
    lot = conn.execute("SELECT * FROM lots WHERE account_id=?", (DEMO,)).fetchone()
    assert lot["quantity"] == 2000 and lot["buy_price"] == 5.0   # 未翻倍成 4000 / 2.5
    n = conn.execute(
        "SELECT COUNT(*) FROM audit_logs WHERE action='corp_action.split'"
    ).fetchone()[0]
    assert n == 1


def test_corp_split_ignores_ex_date_new_lot(authed_client):
    """除权日当日新建仓：当日买入价已含除权 → 不重写（buy_date == ex_date 排除）。"""
    st = authed_client.app.state
    ex = "2026-09-08"
    _insert_order(st, order_id="b-ex", order_type="buy", direction="buy",
                  qty=100, trigger={"op": "le", "price": 10.0}, day=ex)
    r = eodengine.settle_account(
        st, DEMO, ex,
        series_map={"600000": [(f"{ex}T09:31:00", 10.0), (f"{ex}T09:32:00", 10.0)]},
        close_map={"600000": 10.0},
        corp_events={"600000": {"kind": "split", "ratio": 1}},
    )
    assert not r["already_settled"]
    assert r["corp_applied"] == 0      # 无存量 lot → 无可重写对象
    lot = state_conn(st).execute(
        "SELECT * FROM lots WHERE buy_trade_id=(SELECT id FROM trades WHERE order_id='b-ex')"
    ).fetchone()
    assert lot["quantity"] == 100 and lot["buy_price"] == 10.0
    assert lot["corp_action_flags"] == "[]"


def test_corp_dividend_credit_cash_no_tax(authed_client):
    """现金分红（默认不计税）：按存量 lot remaining × dps 净额入账，成本/数量不变。"""
    st = authed_client.app.state
    _buy(st, qty=1000, price=10.0)
    ex = "2026-09-08"
    r = eodengine.settle_account(
        st, DEMO, ex, close_map={"600000": 10.0}, prev_close_map={"600000": 10.0},
        corp_events={"600000": {"kind": "dividend", "cash_per_share": 0.5}},
    )
    assert not r["already_settled"]
    assert r["corp_applied"] == 1 and r["corp_symbols"] == ["600000"]
    conn = state_conn(st)
    cash = conn.execute("SELECT cash FROM accounts WHERE id=?", (DEMO,)).fetchone()[0]
    assert abs(cash - (100000.0 - 10005.1 + 500.0)) < 0.01
    h = conn.execute(
        "SELECT quantity, avg_cost FROM holdings WHERE account_id=? AND symbol='600000'",
        (DEMO,)).fetchone()
    assert h["quantity"] == 1000
    assert abs(h["avg_cost"] - 10.0051) < 1e-4       # 成本不因分红调整
    au = conn.execute(
        "SELECT detail FROM audit_logs WHERE action='corp_action.dividend'").fetchall()
    assert len(au) == 1
    d = json.loads(au[0]["detail"])
    assert d["cash_per_share"] == "0.5" and d["tax_enabled"] is False
    assert d["lots"][0]["dividend"] == "500" and d["tax_total"] == "0"
    assert d["net_credit"] == "500"


def test_corp_dividend_multi_tax_brackets(authed_client):
    """红利税启用：按各 lot 持有期自然月整月取档逐 lot 计税分摊（1 月内 20% / 1 月~1 年 10%）。"""
    st = authed_client.app.state
    _buy(st, day="2026-07-20", qty=100, price=10.0)     # ex 时持有约 2 整月 → 10%
    _buy(st, day="2026-09-07", qty=900, price=10.0)     # ex 时不满 1 月 → 20%
    ex = "2026-09-08"
    r = eodengine.settle_account(
        st, DEMO, ex, close_map={"600000": 10.0}, prev_close_map={"600000": 10.0},
        corp_events={"600000": {"kind": "dividend", "cash_per_share": 0.5, "tax": True}},
    )
    assert r["corp_applied"] == 1
    # lot1 100 股分红 50 ×10% = 税 5；lot2 900 股分红 450 ×20% = 税 90；净 405
    # 买入费：100@10 → 5.01；900@10 → 5.09（佣金 5 + 过户 0.09）
    conn = state_conn(st)
    cash = conn.execute("SELECT cash FROM accounts WHERE id=?", (DEMO,)).fetchone()[0]
    assert abs(cash - (100000.0 - 1005.01 - 9005.09 + 405.0)) < 0.01
    au = conn.execute(
        "SELECT detail FROM audit_logs WHERE action='corp_action.dividend'").fetchall()
    assert len(au) == 1
    d = json.loads(au[0]["detail"])
    assert d["tax_enabled"] is True
    by_rate = {lot["tax_rate"]: lot for lot in d["lots"]}
    assert by_rate["0.1"]["dividend"] == "50" and by_rate["0.1"]["tax"] == "5"
    assert by_rate["0.2"]["dividend"] == "450" and by_rate["0.2"]["tax"] == "90"
    assert d["dividend_total"] == "500" and d["tax_total"] == "95"
    assert d["net_credit"] == "405"


def test_dividend_tax_rate_brackets():
    """红利税档边界（按持有自然日折算）：≤30 日 20% / 31~365 日 10% / >365 日免。"""
    from decimal import Decimal
    f = eodengine._dividend_tax_rate
    assert f("2026-09-07", "2026-09-08") == Decimal("0.20")   # 1 日
    assert f("2026-09-07", "2026-10-06") == Decimal("0.20")   # 29 日（1 个月内）
    assert f("2026-09-07", "2026-10-07") == Decimal("0.20")   # 30 日（恰 1 个月折算）
    assert f("2026-09-07", "2026-10-08") == Decimal("0.10")   # 31 日（>1 个月）
    assert f("2026-09-07", "2026-11-07") == Decimal("0.10")   # 61 日
    assert f("2025-09-07", "2026-09-07") == Decimal("0.10")   # 365 日（恰 1 年 → 仍 10%）
    assert f("2025-09-07", "2026-09-08") == Decimal("0")      # 366 日 → 超 1 年免
    assert f("2025-09-07", "2026-08-06") == Decimal("0.10")   # 333 日
    assert f("2025-09-07", "2026-10-07") == Decimal("0")      # 395 日 → 免



def test_corp_split_run_day_passthrough(authed_client):
    """run_day 透传接线：真实多日 L2 回放下，ex 日持仓自动重写（送转事件由调度侧注入）。"""
    st = authed_client.app.state
    buy_day, ex_day = "2026-08-31", "2026-09-01"
    low = float(_row(buy_day)["low"])
    _insert_order(st, order_id="rd-buy", order_type="buy", direction="buy",
                  qty=500, trigger={"op": "le", "price": low}, day=buy_day)
    r1 = settle_day.run_day(st, buy_day, feed=MultiDayL2Feed(), account_ids=[DEMO])
    acct1 = [a for a in r1["accounts"] if a["account_id"] == DEMO][0]
    assert acct1.get("error") is not True and not acct1.get("skipped")
    lot = state_conn(st).execute(
        "SELECT * FROM lots WHERE account_id=? AND buy_date=?", (DEMO, buy_day)).fetchone()
    assert lot is not None and lot["remaining"] == 500

    r2 = settle_day.run_day(st, ex_day, feed=MultiDayL2Feed(), account_ids=[DEMO],
                            corp_events={"600000": {"kind": "split", "ratio": 1}})
    acct2 = [a for a in r2["accounts"] if a["account_id"] == DEMO][0]
    assert acct2.get("error") is not True
    assert acct2["corp_applied"] == 1 and acct2["corp_symbols"] == ["600000"]
    lot2 = state_conn(st).execute(
        "SELECT * FROM lots WHERE account_id=? AND buy_date=?", (DEMO, buy_day)).fetchone()
    assert lot2["quantity"] == 1000 and lot2["remaining"] == 1000
    half = float(_row(buy_day)["close"]) / 2.0
    assert abs(lot2["buy_price"] - half) < 1e-4


def test_corp_dividend_run_day_passthrough(authed_client):
    """run_day 分红透传：ex 日现金分红净额经 corp_cash 注入，守恒式一通过、无额外成交。"""
    st = authed_client.app.state
    buy_day, ex_day = "2026-08-31", "2026-09-01"
    low = float(_row(buy_day)["low"])
    _insert_order(st, order_id="rd-dbuy", order_type="buy", direction="buy",
                  qty=500, trigger={"op": "le", "price": low}, day=buy_day)
    settle_day.run_day(st, buy_day, feed=MultiDayL2Feed(), account_ids=[DEMO])
    conn = state_conn(st)
    cash_before = conn.execute(
        "SELECT cash FROM accounts WHERE id=?", (DEMO,)).fetchone()[0]

    r2 = settle_day.run_day(st, ex_day, feed=MultiDayL2Feed(), account_ids=[DEMO],
                            corp_events={"600000": {"kind": "dividend",
                                                    "cash_per_share": 0.5}})
    acct2 = [a for a in r2["accounts"] if a["account_id"] == DEMO][0]
    assert acct2.get("error") is not True
    assert acct2["corp_applied"] == 1 and acct2["corp_symbols"] == ["600000"]
    cash_after = conn.execute(
        "SELECT cash FROM accounts WHERE id=?", (DEMO,)).fetchone()[0]
    assert abs(cash_after - cash_before - 250.0) < 0.01      # 500 股 × 0.5 全额入账
    h = conn.execute(
        "SELECT quantity, avg_cost FROM holdings WHERE account_id=? AND symbol='600000'",
        (DEMO,)).fetchone()
    assert h["quantity"] == 500                                # 分红不动数量与成本
    n = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE account_id=? AND settle_date=?",
        (DEMO, ex_day)).fetchone()[0]
    assert n == 0                                              # 无成交、纯现金事件



def test_corp_rights_default_waived(authed_client):
    """配股默认放弃：不动现金/持仓，仅审计留痕 + 幂等 tag。"""
    st = authed_client.app.state
    _buy(st, qty=1000, price=10.0)
    ex = "2026-09-08"
    r = eodengine.settle_account(
        st, DEMO, ex, close_map={"600000": 10.0}, prev_close_map={"600000": 10.0},
        corp_events={"600000": {"kind": "rights", "ratio": 0.5, "price": 8.0}},
    )
    assert r["corp_applied"] == 1 and r["corp_symbols"] == ["600000"]
    conn = state_conn(st)
    cash = conn.execute("SELECT cash FROM accounts WHERE id=?", (DEMO,)).fetchone()[0]
    assert abs(cash - (100000.0 - 10005.1)) < 0.01        # 未缴款
    h = conn.execute(
        "SELECT quantity FROM holdings WHERE account_id=? AND symbol='600000'",
        (DEMO,)).fetchone()
    assert h["quantity"] == 1000                          # 未增股
    au = conn.execute(
        "SELECT result FROM audit_logs WHERE action='corp_action.rights'").fetchone()
    assert au["result"] == "waived"


def test_corp_rights_participate_pays_and_creates_lot(authed_client):
    """配股参与：现金缴款、新增配股 lot（buy_date=ex，T+1 后可卖）、avg 加权摊薄。"""
    st = authed_client.app.state
    _buy(st, qty=1000, price=10.0)
    ex = "2026-09-08"
    r = eodengine.settle_account(
        st, DEMO, ex, close_map={"600000": 10.0}, prev_close_map={"600000": 10.0},
        corp_events={"600000": {"kind": "rights", "ratio": 0.5, "price": 8.0,
                                "participate": True}},
    )
    assert r["corp_applied"] == 1
    conn = state_conn(st)
    cash = conn.execute("SELECT cash FROM accounts WHERE id=?", (DEMO,)).fetchone()[0]
    assert abs(cash - (100000.0 - 10005.1 - 4000.0)) < 0.01   # 缴款 500 股 × 8
    h = conn.execute(
        "SELECT quantity, avg_cost FROM holdings WHERE account_id=? AND symbol='600000'",
        (DEMO,)).fetchone()
    assert h["quantity"] == 1500
    # avg = (10.0051×1000 + 4000) / 1500 ≈ 9.3367
    assert abs(h["avg_cost"] - (10.0051 * 1000 + 4000) / 1500) < 1e-3
    rlot = conn.execute(
        "SELECT * FROM lots WHERE account_id=? AND buy_date=? AND buy_trade_id=''",
        (DEMO, ex)).fetchone()
    assert rlot["quantity"] == 500 and rlot["remaining"] == 500 and rlot["buy_price"] == 8.0
    assert json.loads(rlot["corp_action_flags"]) == [f"rights:{ex}:600000"]
    au = conn.execute(
        "SELECT result, detail FROM audit_logs WHERE action='corp_action.rights'").fetchone()
    assert au["result"] == "paid"
    assert json.loads(au["detail"])["subscribe_qty"] == "500"
    assert json.loads(au["detail"])["payable"] == "4000"


def test_corp_rights_participate_insufficient_auto_waive(authed_client):
    """参与配股但现金不足 → 自动放弃（declined_insufficient），不产生负现金。"""
    st = authed_client.app.state
    _buy(st, qty=1000, price=10.0)
    ex = "2026-09-08"
    r = eodengine.settle_account(
        st, DEMO, ex, close_map={"600000": 10.0}, prev_close_map={"600000": 10.0},
        corp_events={"600000": {"kind": "rights", "ratio": 10.0, "price": 50.0,
                                "participate": True}},   # 需缴 10000 股 ×50 ≫ 现金
    )
    assert r["corp_applied"] == 1
    conn = state_conn(st)
    cash = conn.execute("SELECT cash FROM accounts WHERE id=?", (DEMO,)).fetchone()[0]
    assert cash >= 0 and abs(cash - (100000.0 - 10005.1)) < 0.01   # 未扣款
    h = conn.execute(
        "SELECT quantity FROM holdings WHERE account_id=? AND symbol='600000'",
        (DEMO,)).fetchone()
    assert h["quantity"] == 1000                                # 未增股
    au = conn.execute(
        "SELECT result, detail FROM audit_logs WHERE action='corp_action.rights'").fetchone()
    assert au["result"] == "declined_insufficient"
    assert "禁负现金" in json.loads(au["detail"])["note"]


def test_corp_multi_event_same_day_split_and_dividend(authed_client):
    """同票同日多事件（列表序应用）：先派息后送转——红利按登记日原持股 1000 计，再送转 ×2。"""
    st = authed_client.app.state
    _buy(st, qty=1000, price=10.0)
    ex = "2026-09-08"
    r = eodengine.settle_account(
        st, DEMO, ex, close_map={"600000": 5.0}, prev_close_map={"600000": 10.0},
        corp_events={"600000": [
            {"kind": "dividend", "cash_per_share": 0.5},     # 1000 股 → 500
            {"kind": "split", "ratio": 1},                    # 之后 2000 股 @5
        ]},
    )
    assert not r["already_settled"]
    assert r["corp_applied"] == 2 and r["corp_symbols"] == ["600000"]
    conn = state_conn(st)
    cash = conn.execute("SELECT cash FROM accounts WHERE id=?", (DEMO,)).fetchone()[0]
    assert abs(cash - (100000.0 - 10005.1 + 500.0)) < 0.01    # 派息按送转前 1000 股
    lot = conn.execute(
        "SELECT * FROM lots WHERE account_id=? AND buy_date='2026-09-07'",
        (DEMO,)).fetchone()
    assert lot["quantity"] == 2000 and lot["remaining"] == 2000 and lot["buy_price"] == 5.0
    tags = set(json.loads(lot["corp_action_flags"]))
    assert tags == {f"dividend:{ex}:600000", f"split:{ex}:600000"}
    au = conn.execute(
        "SELECT action, COUNT(*) AS n FROM audit_logs WHERE action LIKE 'corp_action.%'"
        " GROUP BY action ORDER BY action").fetchall()
    assert {(a["action"], a["n"]) for a in au} == \
        {("corp_action.dividend", 1), ("corp_action.split", 1)}

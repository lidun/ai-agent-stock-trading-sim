"""结算调度编排测试：feed 以 fixtures 确定性注入，验证真实交易日 L1 回放端到端与幂等。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from core import l0store, settle_day
from core.db import state_conn, write_txn
from _feedkit import FakeFeed, L2OnlyFeed

DEMO = "agent-demo-001"
DATE = "2026-09-04"


def _insert_buy_order(state, *, order_id, account_id=DEMO, symbol="600000",
                      trigger=None, qty=100, created=f"{DATE}T09:00:00"):
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
                order_id, account_id, "buy", "buy", "single", symbol,
                json.dumps(trigger or {"op": "le", "price": 9.28}), "replay_l0",
                "absolute", qty, "limit", "today", 0, "active", created,
                "agent-demo-001", "settle-day 集成测试",
            ),
        )


def _seed_agent_account(state, agent_id: str):
    conn = state_conn(state)
    with write_txn(conn) as c:
        c.execute(
            "INSERT OR IGNORE INTO agents (id, name, role, status, created_ts)"
            " VALUES (?,?,?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
            (agent_id, "结算测试子 Agent", "strategy", "running"),
        )
        c.execute(
            """
            INSERT OR IGNORE INTO accounts
                (id, agent_id, role, parent_agent_id,
                 initial_capital, cash, nav, shares, total_pnl, today_pnl,
                 granularity, granularity_history, settle_key, status, active_version_no,
                 created_ts, updated_ts)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (agent_id, agent_id, "main", agent_id, 100000.0, 100000.0, 1.0, 100000.0,
             0.0, 0.0, "eod_replay", "[]", "", "normal", "", "", ""),
        )


def test_settle_day_l1_real_session_e2e(authed_client):
    st = authed_client.app.state
    _insert_buy_order(st, order_id="sd-1")
    report = settle_day.run_day(st, DATE, feed=FakeFeed())
    acct = [a for a in report["accounts"] if a["account_id"] == DEMO][0]
    assert acct.get("error") is not True and acct.get("skipped") is not True
    assert acct["trade_date"] == DATE or "trade_date" not in acct
    assert acct["already_settled"] is False
    conn = state_conn(st)
    tr = conn.execute("SELECT * FROM trades WHERE order_id='sd-1'").fetchone()
    assert tr is not None
    assert tr["price"] == 9.28 and tr["basis_used"] == "l1"
    assert tr["settle_date"] == DATE and tr["quality"] == ""
    assert tr["trade_time"] >= f"{DATE}T09:30:00"
    lot = conn.execute("SELECT remaining FROM lots WHERE buy_trade_id=?", (tr["id"],)).fetchone()
    assert lot["remaining"] == 100
    sl = conn.execute("SELECT * FROM settlement_log WHERE account_id=?", (DEMO,)).fetchone()
    assert json.loads(sl["granularity_used"]) == {"600000": "l1"}
    # 幂等：再跑同一天不重复写
    again = settle_day.run_day(st, DATE, feed=FakeFeed())
    got = [a for a in again["accounts"] if a["account_id"] == DEMO][0]
    assert got["already_settled"] is True
    assert conn.execute(
        "SELECT COUNT(*) FROM settlement_log WHERE account_id=?", (DEMO,)
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM trades WHERE account_id=?", (DEMO,)
    ).fetchone()[0] == 1


def test_settle_day_gap_reports_per_account_no_writes(authed_client):
    st = authed_client.app.state
    _insert_buy_order(st, order_id="sd-gap", symbol="600001")
    conn = state_conn(st)
    # 另一独立账户数据齐备 → 缺口账户不影响其结算
    _seed_agent_account(st, "agent-settle-b")
    _insert_buy_order(st, order_id="sd-ok", symbol="600000", account_id="agent-settle-b")
    report = settle_day.run_day(st, DATE, feed=FakeFeed())
    by_id = {a["account_id"]: a for a in report["accounts"]}
    assert by_id[DEMO]["error"] is True and "600001" in by_id[DEMO]["reason"]
    ok = by_id["agent-settle-b"]
    assert ok.get("error") is not True and ok.get("skipped") is not True
    assert conn.execute(
        "SELECT COUNT(*) FROM settlement_log WHERE account_id=?", (DEMO,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM trades WHERE account_id=?", (DEMO,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT status FROM condition_orders WHERE id='sd-gap'"
    ).fetchone()["status"] == "active"
    tr = conn.execute("SELECT price, basis_used FROM trades WHERE order_id='sd-ok'").fetchone()
    assert tr is not None and tr["price"] == 9.28 and tr["basis_used"] == "l1"


def test_settle_day_l2_fallback_when_minutes_unavailable(authed_client):
    """分钟缺口 → run_day 自动回退 L2 日线区间：触达以官方收盘成交、basis_used='l2'。"""
    st = authed_client.app.state
    on = _day_row_on()
    _insert_buy_order(st, order_id="sd-l2", trigger={"op": "le", "price": on["low"]})
    report = settle_day.run_day(st, DATE, feed=L2OnlyFeed())
    acct = [a for a in report["accounts"] if a["account_id"] == DEMO][0]
    assert acct.get("error") is not True and acct.get("skipped") is not True
    conn = state_conn(st)
    tr = conn.execute("SELECT * FROM trades WHERE order_id='sd-l2'").fetchone()
    assert tr is not None and tr["basis_used"] == "l2"
    assert tr["price"] == on["close"] and tr["trade_time"] == f"{DATE}T15:00:00"
    sl = conn.execute("SELECT * FROM settlement_log WHERE account_id=?", (DEMO,)).fetchone()
    assert json.loads(sl["granularity_used"]) == {"600000": "l2"}


def _day_row_on():
    from core import quotes_tencent as q
    from _feedkit import FIX
    rows = q.parse_day_rows((FIX / "tencent_day_sh600000.json").read_text("utf-8"))
    row = next(r for r in rows if r["date"] == DATE)
    return {"high": float(row["high"]), "low": float(row["low"]), "close": float(row["close"])}


def test_settle_day_empty_account_skipped(authed_client):
    st = authed_client.app.state
    report = settle_day.run_day(st, DATE, feed=FakeFeed())
    got = [a for a in report["accounts"] if a["account_id"] == DEMO][0]
    assert got.get("skipped") is True
    assert state_conn(st).execute(
        "SELECT COUNT(*) FROM settlement_log WHERE account_id=?", (DEMO,)
    ).fetchone()[0] == 0


def test_settle_day_suspended_symbol_skips_feed_and_keeps_others(authed_client):
    """编排层透传 suspend_map：停牌票不做当日行情拉取（源无当日行属正常态），
    其余票照常成交，停牌 today 单到期、账户不报数据缺口错误。"""
    st = authed_client.app.state
    _insert_buy_order(st, order_id="sd-susp", symbol="000001",
                      trigger={"op": "le", "price": 9.28})
    _insert_buy_order(st, order_id="sd-ok", symbol="600000",
                      trigger={"op": "le", "price": 10.5})
    report = settle_day.run_day(
        st, DATE, feed=FakeFeed(),
        suspend_map={"000001": {"close": 9.5, "prev": 9.5}},
    )
    acct = [a for a in report["accounts"] if a["account_id"] == DEMO][0]
    assert acct.get("error") is not True and acct.get("skipped") is not True
    assert acct["suspended_symbols"] == ["000001"]
    conn = state_conn(st)
    assert conn.execute("SELECT * FROM trades WHERE order_id='sd-susp'").fetchone() is None
    tr = conn.execute("SELECT order_id FROM trades WHERE order_id='sd-ok'").fetchone()
    assert tr is not None and tr["order_id"] == "sd-ok"
    assert conn.execute(
        "SELECT status FROM condition_orders WHERE id='sd-susp'"
    ).fetchone()["status"] == "expired"


def _l0_session_rows():
    """09-04 全时段 3 秒采样（覆盖率 100%），盘中 09:35-09:40 触及 9.28 后回 9.43。"""
    rows = []
    price = "9.43"
    for start in (datetime(2026, 9, 4, 9, 30), datetime(2026, 9, 4, 13, 0)):
        end = start.replace(hour=11, minute=30) if start.hour == 9 \
            else start.replace(hour=15, minute=0)
        t = start
        while t < end:
            px = "9.28" if datetime(2026, 9, 4, 9, 35) <= t < datetime(2026, 9, 4, 9, 40) \
                else price
            rows.append({"symbol": "600000", "trade_date": DATE,
                         "ts": t.isoformat(timespec="seconds"), "price": px,
                         "prev_close": "9.4", "pct_chg": "0", "cum_turnover": "0",
                         "status": "normal", "is_extended": 0, "source": "fake"})
            t += timedelta(seconds=3)
    return rows


def test_settle_day_l0_local_series_e2e(authed_client):
    """本地 L0 高覆盖（≥90%）→ settle_day 走数据服务 L0 档：series_map 成交、basis_used='l0'。"""
    st = authed_client.app.state
    l0store.upsert_l0_ticks(st, _l0_session_rows())
    l0store.update_coverage(st, "600000", DATE)
    _insert_buy_order(st, order_id="sd-l0", trigger={"op": "le", "price": 9.28})
    report = settle_day.run_day(st, DATE, feed=FakeFeed())
    acct = [a for a in report["accounts"] if a["account_id"] == DEMO][0]
    assert acct.get("error") is not True and acct.get("skipped") is not True
    conn = state_conn(st)
    tr = conn.execute("SELECT * FROM trades WHERE order_id='sd-l0'").fetchone()
    assert tr is not None and tr["basis_used"] == "l0" and tr["price"] == 9.28
    assert tr["trade_time"] >= f"{DATE}T09:35:00"
    sl = conn.execute("SELECT * FROM settlement_log WHERE account_id=?", (DEMO,)).fetchone()
    assert json.loads(sl["granularity_used"]) == {"600000": "l0"}

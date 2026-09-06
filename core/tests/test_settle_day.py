"""结算调度编排测试：feed 以 fixtures 确定性注入，验证真实交易日 L1 回放端到端与幂等。"""
from __future__ import annotations

import json

from core import settle_day
from core.db import state_conn, write_txn
from _feedkit import FakeFeed

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


def test_settle_day_empty_account_skipped(authed_client):
    st = authed_client.app.state
    report = settle_day.run_day(st, DATE, feed=FakeFeed())
    got = [a for a in report["accounts"] if a["account_id"] == DEMO][0]
    assert got.get("skipped") is True
    assert state_conn(st).execute(
        "SELECT COUNT(*) FROM settlement_log WHERE account_id=?", (DEMO,)
    ).fetchone()[0] == 0

"""模拟账户存储访问（spec-01 §2.1/§6.1，accounts N:1 子 Agent）。

金额字段 REAL 存储；对外序列化为带小数精度的字符串，避免浮点显示误差。
生命周期状态由 agents.status 映射（总纲 §3.5），账户 status 保存同源副本。
v6（#63）：accounts.agent_id/role/parent_agent_id——主账户 role=main、parent=自身；
trial/validation 账户 parent 指向父 Agent（spec-01 §2.8）。
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from core.db import read_txn, state_conn, write_txn

_MONEY = Decimal("0.01")
_NAV = Decimal("0.0001")

_ACCOUNT_COLS = (
    "ac.id, ac.agent_id, ac.role, ac.parent_agent_id,"
    " ac.initial_capital, ac.cash, ac.nav, ac.shares,"
    " ac.total_pnl, ac.today_pnl, ac.granularity, ac.settle_key,"
    " ac.status, ac.active_version_no, ac.created_ts, ac.updated_ts"
)


def _money(v: float) -> str:
    return str(Decimal(str(v)).quantize(_MONEY, rounding=ROUND_HALF_UP))


def _nav(v: float) -> str:
    return str(Decimal(str(v)).quantize(_NAV, rounding=ROUND_HALF_UP))


def _serialize(row: dict) -> dict:
    return {
        "id": row["id"],
        "agent_id": row["agent_id"],
        "role": row["role"],
        "parent_agent_id": row["parent_agent_id"],
        "agent_name": row["agent_name"],
        "agent_role": row["agent_role"],
        "agent_status": row["agent_status"],
        "initial_capital": _money(row["initial_capital"]),
        "cash": _money(row["cash"]),
        "nav": _nav(row["nav"]),
        "shares": _money(row["shares"]),
        "total_pnl": _money(row["total_pnl"]),
        "today_pnl": _money(row["today_pnl"]),
        "granularity": row["granularity"],
        "settle_key": row["settle_key"],
        "status": row["status"],
        "active_version_no": row["active_version_no"],
        "created_ts": row["created_ts"],
        "updated_ts": row["updated_ts"],
    }


def list_accounts(state) -> list[dict]:
    conn = state_conn(state)
    with read_txn(conn) as c:
        rows = c.execute(
            f"""
            SELECT {_ACCOUNT_COLS},
                   ag.name AS agent_name, ag.role AS agent_role, ag.status AS agent_status
              FROM accounts ac
              JOIN agents ag ON ag.id = ac.agent_id
             ORDER BY CASE ag.role WHEN 'manager' THEN 0 ELSE 1 END,
                      ac.agent_id,
                      CASE ac.role WHEN 'main' THEN 0 ELSE 1 END,
                      ac.id
            """
        ).fetchall()
    return [_serialize(dict(r)) for r in rows]


def get_account(state, account_id: str) -> dict | None:
    conn = state_conn(state)
    with read_txn(conn) as c:
        row = c.execute(
            f"""
            SELECT {_ACCOUNT_COLS},
                   ag.name AS agent_name, ag.role AS agent_role, ag.status AS agent_status
              FROM accounts ac
              JOIN agents ag ON ag.id = ac.agent_id
             WHERE ac.id = ?
            """,
            (account_id,),
        ).fetchone()
    return _serialize(dict(row)) if row else None


def accounts_for_agent(state, agent_id: str) -> list[dict]:
    """某策略 Agent 的全部账户（主 + trial/validation，spec-01 §2.8 #63）。"""
    conn = state_conn(state)
    with read_txn(conn) as c:
        rows = c.execute(
            f"""
            SELECT {_ACCOUNT_COLS},
                   ag.name AS agent_name, ag.role AS agent_role, ag.status AS agent_status
              FROM accounts ac
              JOIN agents ag ON ag.id = ac.agent_id
             WHERE ac.agent_id = ?
             ORDER BY CASE ac.role WHEN 'main' THEN 0 ELSE 1 END, ac.id
            """,
            (agent_id,),
        ).fetchall()
    return [_serialize(dict(r)) for r in rows]


def create_trial_agent(state, *, agent_id: str, name: str) -> dict:
    """开通策略子 Agent：agent 进入试运行（status=trial），随建主/trial 双账户（#63）。

    主账户 role=main parent=自身 status=normal（10 万种子）；trial 账户 role=trial
    status=trial、id=agent_id.trial，parent 指向主 Agent——试运行回放落 trial 账户，
    主账户零污染（spec-01 §2.8）。整体单事务。
    """
    conn = state_conn(state)
    with write_txn(conn) as c:
        exists = c.execute("SELECT 1 FROM agents WHERE id=?", (agent_id,)).fetchone()
        if exists:
            raise LookupError(f"Agent {agent_id} 已存在")
        c.execute(
            "INSERT INTO agents (id, name, role, status, created_ts)"
            " VALUES (?,?,?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
            (agent_id, name, "strategy", "trial"),
        )
        ts = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"
        for role, suffix, acct_status in (
            ("main", "", "normal"),
            ("trial", ".trial", "trial"),
        ):
            c.execute(
                f"""
                INSERT INTO accounts
                    (id, agent_id, role, parent_agent_id,
                     initial_capital, cash, nav, shares, total_pnl, today_pnl,
                     granularity, granularity_history, settle_key, status,
                     active_version_no, created_ts, updated_ts)
                VALUES (?,?,?,?, 100000.0,100000.0,1.0,100000.0,0.0,0.0,
                        'eod_replay','[]','',?, '', {ts}, {ts})
                """,
                (agent_id + suffix, agent_id, role, agent_id, acct_status),
            )
    return {
        "agent": {"id": agent_id, "name": name, "role": "strategy", "status": "trial"},
        "accounts": accounts_for_agent(state, agent_id),
    }


def finish_trial(state, *, agent_id: str, decision: str,
                 verdict: str = "") -> dict:
    """试运行验收归档留证（spec-05 §6.2/#63）：launch 通过 / reject 否决。

    决策后 trial 账户整体归档为不可变证据：settlement_log 结算日 + 订单/成交/持仓/底仓
    计数快照写入 trial_archives（一次写入，不再变更），trial 账户转 archived 停止参与
    结算；主账户零污染不动（launch→agent running，reject→agent archived）。整体单事务。
    """
    if decision not in ("launch", "reject"):
        raise LookupError(f"未知验收决策: {decision}（仅 launch/reject）")
    conn = state_conn(state)
    archive_id = agent_id + ".ta"
    ts = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"
    with write_txn(conn) as c:
        agent = c.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        if agent is None:
            raise LookupError(f"Agent {agent_id} 不存在")
        if agent["status"] != "trial":
            raise LookupError(f"Agent {agent_id} 不在试运行期（status={agent['status']}）")
        trial = c.execute(
            "SELECT * FROM accounts WHERE agent_id=? AND role='trial'", (agent_id,)
        ).fetchone()
        if trial is None or trial["status"] != "trial":
            raise LookupError(f"Agent {agent_id} 无试运行 trial 账户或已归档")
        main = c.execute(
            "SELECT * FROM accounts WHERE agent_id=? AND role='main'", (agent_id,)
        ).fetchone()

        def _n(sql: str) -> int:
            return c.execute(sql, (trial["id"],)).fetchone()[0]

        settle_dates = [
            r[0] for r in c.execute(
                "SELECT trade_date FROM settlement_log WHERE account_id=?"
                " ORDER BY trade_date", (trial["id"],)).fetchall()
        ]
        snapshot = {
            "decision": decision,
            "verdict": verdict.strip(),
            "account": {
                "id": trial["id"], "agent_id": agent_id, "role": "trial",
                "initial_capital": trial["initial_capital"],
                "cash": trial["cash"], "nav": trial["nav"],
                "shares": trial["shares"], "total_pnl": trial["total_pnl"],
                "status": trial["status"],
            },
            "counts": {
                "settle_days": len(settle_dates),
                "orders": _n("SELECT COUNT(*) FROM condition_orders WHERE account_id=?"),
                "trades": _n("SELECT COUNT(*) FROM trades WHERE account_id=?"),
                "holdings": _n("SELECT COUNT(*) FROM holdings WHERE account_id=?"),
                "lots": _n("SELECT COUNT(*) FROM lots WHERE account_id=?"),
            },
            "settle_dates": settle_dates,
        }
        import json as _json
        snapshot_json = _json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        next_status = "running" if decision == "launch" else "archived"
        c.execute("UPDATE agents SET status=? WHERE id=?", (next_status, agent_id))
        c.execute(
            "UPDATE accounts SET status='archived', "
            f"updated_ts={ts} WHERE id=?", (trial["id"],))
        c.execute(
            "INSERT INTO trial_archives (id, agent_id, account_id, decision, verdict,"
            " snapshot, archived_ts) VALUES (?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
            (archive_id, agent_id, trial["id"], decision, verdict.strip(), snapshot_json),
        )
    return {
        "archive_id": archive_id,
        "agent": {"id": agent_id, "name": agent["name"], "role": "strategy",
                  "status": next_status},
        "trial_account_id": trial["id"],
        "snapshot": snapshot,
    }

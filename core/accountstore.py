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


def create_trial_agent(state, *, agent_id: str, name: str,
                       window_days: int = 5) -> dict:
    """开通策略子 Agent：agent 进入试运行（status=trial），随建主/trial 双账户（#63）。

    主账户 role=main parent=自身 status=normal（10 万种子）；trial 账户 role=trial
    status=trial、id=agent_id.trial，parent 指向主 Agent——试运行回放落 trial 账户，
    主账户零污染（spec-01 §2.8）。试运行回放窗口 5-20 交易日默认 5（spec-05 §6.2，
    与 #18 N≥5 对齐），随建 trial_replays 台账；回放会话由 EodSettleTrigger
    run_trial_backfill 逐日计入，满窗口自动 done。整体单事务。
    """
    if not isinstance(window_days, int) or not (5 <= window_days <= 20):
        raise LookupError(f"试运行回放窗口须为 5-20 交易日，收到 {window_days!r}")
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
        c.execute(
            f"""
            INSERT INTO trial_replays
                (agent_id, trial_account_id, window_days, status, created_ts, updated_ts)
            VALUES (?,?,?, 'in_progress', {ts}, {ts})
            """,
            (agent_id, agent_id + ".trial", window_days),
        )
    return {
        "agent": {"id": agent_id, "name": name, "role": "strategy", "status": "trial",
                  "trial_window_days": window_days},
        "accounts": accounts_for_agent(state, agent_id),
        "replay": trial_replay(state, agent_id),
    }


def trial_replay(state, agent_id: str) -> dict | None:
    """试运行回放台账：{window_days, status, sessions: [..], sessions_done}。"""
    conn = state_conn(state)
    with read_txn(conn) as c:
        row = c.execute(
            "SELECT * FROM trial_replays WHERE agent_id=?", (agent_id,)
        ).fetchone()
        if row is None:
            return None
        days = [r[0] for r in c.execute(
            "SELECT trade_date FROM replay_sessions WHERE agent_id=?"
            " ORDER BY trade_date", (agent_id,)).fetchall()]
    return {
        "agent_id": agent_id,
        "trial_account_id": row["trial_account_id"],
        "window_days": row["window_days"],
        "status": row["status"],
        "sessions": days,
        "sessions_done": len(days),
    }


def add_trial_session(state, agent_id: str, trade_date: str) -> dict:
    """回放会话记账：逐历史日计数（agent+trade_date 唯一，重跑幂等）。满窗口转 done。

    返回台账最新状态；done 后由 finish_trial（归档留证）收口。未知 agent / 非试运行
    期回放一律拒绝（防越权向非试运行账户记会话）。
    """
    conn = state_conn(state)
    with write_txn(conn) as c:
        row = c.execute(
            "SELECT r.*, ag.status AS agent_status FROM trial_replays r"
            " JOIN agents ag ON ag.id = r.agent_id WHERE r.agent_id=?",
            (agent_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"Agent {agent_id} 无试运行回放台账")
        if row["status"] == "done":
            raise LookupError(f"Agent {agent_id} 回放窗口已完成（等验收归档）")
        if row["agent_status"] != "trial":
            raise LookupError(f"Agent {agent_id} 不在试运行期（status={row['agent_status']}）")
        c.execute(
            "INSERT INTO replay_sessions (agent_id, account_id, trade_date, created_ts)"
            " VALUES (?,?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
            " ON CONFLICT(agent_id, trade_date) DO NOTHING",
            (agent_id, row["trial_account_id"], trade_date),
        )
        cnt = c.execute(
            "SELECT COUNT(*) AS n FROM replay_sessions WHERE agent_id=?",
            (agent_id,),
        ).fetchone()["n"]
        if cnt >= row["window_days"] and row["status"] == "in_progress":
            c.execute(
                "UPDATE trial_replays SET status='done', "
                f"updated_ts=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE agent_id=?",
                (agent_id,),
            )
    return trial_replay(state, agent_id)


def control_agent(state, *, agent_id: str, op: str,
                  operator: str = "user") -> dict:
    """用户直控（spec-06 §6.3 → spec-01 直控接口）：主账户状态即时切换，秒级生效。

    op ∈ pause_buy（冻结买入：保留卖出与风控）/ halt（冻结证券/熔断，买卖全停）/
    resume（解除冻结恢复 normal）。Agent 保持 running 不断结算；撮合闸门按账户
    status + 订单 direction 判定（冻结买入期间 direction=sell 仍可成交，见 orderstore）。
    仅运行中的策略 Agent 可操作；审计由路由层落 account.control_*。
    """
    if op not in ("pause_buy", "halt", "resume"):
        raise LookupError(f"未知直控操作: {op}（仅 pause_buy/halt/resume）")
    conn = state_conn(state)
    with write_txn(conn) as c:
        agent = c.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        if agent is None:
            raise LookupError(f"Agent {agent_id} 不存在")
        if agent["role"] != "strategy":
            raise LookupError(f"Agent {agent_id} 为非策略 Agent，无交易账户可直控")
        main = c.execute(
            "SELECT * FROM accounts WHERE agent_id=? AND role='main'", (agent_id,)
        ).fetchone()
        if main is None:
            raise LookupError(f"Agent {agent_id} 无主账户（可能未开通）")
        if agent["status"] != "running":
            raise LookupError(
                f"Agent {agent_id} 不在运行期（status={agent['status']}），仅运行中可直控")
        cur = main["status"]
        target = {
            "pause_buy": "paused_buy", "halt": "halted",
        }.get(op, "normal")
        if cur == target:
            raise LookupError(f"账户已是 {cur}，无变更（重复直控幂等拒绝）")
        ts = "strftime('%Y-%m-%dT%H:%M:%SZ','now')"
        c.execute(
            f"UPDATE accounts SET status=?, updated_ts={ts} WHERE id=?",
            (target, main["id"]))
    updated = get_account(state, main["id"])
    return {"agent_id": agent_id, "op": op, "account": updated,
            "from": cur, "to": updated["status"]}


def trial_progress(state, agent_id: str) -> dict | None:
    """试运行验收进度（spec-05 §6.1 门槛预览，供 UI 验收看板）。

    与 finish_trial 同口径的只读快照：窗口回放数/挂单尝试/结算异常日 + 三门槛
    ok/reason。决定时刻仍以 finish_trial 内复核为准（单写事务，不双源裁决）。
    """
    conn = state_conn(state)
    with read_txn(conn) as c:
        agent = c.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        if agent is None:
            return None
        replay = c.execute(
            "SELECT * FROM trial_replays WHERE agent_id=?", (agent_id,)
        ).fetchone()
        trial = c.execute(
            "SELECT * FROM accounts WHERE agent_id=? AND role='trial'", (agent_id,)
        ).fetchone()

        def _n(sql: str, acc_id: str) -> int:
            return c.execute(sql, (acc_id,)).fetchone()[0]

        replay_dates = [
            r[0] for r in c.execute(
                "SELECT trade_date FROM replay_sessions WHERE agent_id=?"
                " ORDER BY trade_date", (agent_id,)).fetchall()
        ]
        settle_dates = [
            r[0] for r in c.execute(
                "SELECT trade_date FROM settlement_log WHERE account_id=?"
                " ORDER BY trade_date", (trial["id"],)).fetchall()
        ] if trial else []
        cond_orders = _n(
            "SELECT COUNT(*) FROM condition_orders WHERE account_id=?", trial["id"]
        ) if trial else 0
        abnormal = [
            d for d in replay_dates
            if trial and c.execute(
                "SELECT 1 FROM condition_orders WHERE account_id=?"
                " AND substr(created_at, 1, 10)=? LIMIT 1",
                (trial["id"], d)).fetchone()
            and not c.execute(
                "SELECT 1 FROM settlement_log WHERE account_id=? AND trade_date=?",
                (trial["id"], d)).fetchone()
        ]
    if replay is None or trial is None:
        replay = None
    window_days = replay["window_days"] if replay else 5
    sessions_done = len(replay_dates)
    window_ok = bool(replay and replay["status"] == "done") and sessions_done >= window_days
    attempt_ok = cond_orders >= 1
    abnormal_ok = not abnormal
    reasons: list[str] = []
    if not window_ok:
        reasons.append(
            f"试运行回放未满 {window_days} 个交易日（已回放 {sessions_done}）")
    if not attempt_ok:
        reasons.append("试运行期无任何条件单尝试")
    if not abnormal_ok:
        reasons.append(
            "存在结算异常日：" + "、".join(abnormal[:5])
            + (f" 等 {len(abnormal)} 日" if len(abnormal) > 5 else "")
            + "（有订单但当日结算未落账）")
    return {
        "agent_id": agent_id,
        "agent_status": agent["status"],
        "trial_status": trial["status"] if trial else None,
        "window_days": window_days,
        "replay_status": replay["status"] if replay else "",
        "sessions": replay_dates,
        "sessions_done": sessions_done,
        "settle_days": len(settle_dates),
        "condition_orders": cond_orders,
        "abnormal_dates": abnormal,
        "gates": {"window_ok": window_ok, "attempt_ok": attempt_ok,
                  "abnormal_ok": abnormal_ok},
        "reasons": reasons,
    }


def finish_trial(state, *, agent_id: str, decision: str,
                 verdict: str = "") -> dict:
    """试运行验收归档留证（spec-05 §6.2/#63）：launch 通过 / reject 否决。

    决策后 trial 账户整体归档为不可变证据：settlement_log 结算日 + 订单/成交/持仓/底仓
    计数快照写入 trial_archives（一次写入，不再变更），trial 账户转 archived 停止参与
    结算；主账户零污染不动（launch→agent running，reject→agent archived）。整体单事务。
    launch 需通过 §6.1 硬门槛：回放窗口满（N≥5 交易日）且 ≥1 次条件单尝试；reject 无门槛。
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
        replay = c.execute(
            "SELECT window_days, status FROM trial_replays WHERE agent_id=?",
            (agent_id,)).fetchone()
        replay_dates = [
            r[0] for r in c.execute(
                "SELECT trade_date FROM replay_sessions WHERE agent_id=?"
                " ORDER BY trade_date", (agent_id,)).fetchall()
        ]
        if decision == "launch":
            # spec-05 §6.1 硬门槛（确定性）：回放窗口满（N≥5）且含 ≥1 次条件单尝试。
            # 窗口上限由 create_trial_agent 约束 5-20，故满窗口即满足 N≥5。
            if replay is None or replay["status"] != "done":
                want = replay["window_days"] if replay else 5
                raise LookupError(
                    f"Agent {agent_id} 试运行回放未满 {want} 个交易日"
                    f"（已回放 {len(replay_dates)}，spec-05 §6.1 N≥5 硬门槛），不可 launch")
            if _n("SELECT COUNT(*) FROM condition_orders WHERE account_id=?") < 1:
                raise LookupError(
                    f"Agent {agent_id} 试运行期无任何条件单尝试"
                    "（spec-05 §6.1 硬门槛），不可 launch")
            # spec-05 §6.1 规则级异常不允许上线：回放日当天有订单尝试却无 settlement_log
            # （引擎对账不平/数据缺口 → 该日结算未落账）。日志缺失即异常日，点名并拒 launch。
            abnormal = [
                d for d in replay_dates
                if c.execute(
                    "SELECT 1 FROM condition_orders WHERE account_id=?"
                    " AND substr(created_at, 1, 10)=? LIMIT 1",
                    (trial["id"], d)).fetchone()
                and not c.execute(
                    "SELECT 1 FROM settlement_log WHERE account_id=? AND trade_date=?",
                    (trial["id"], d)).fetchone()
            ]
            if abnormal:
                listed = ",".join(abnormal[:5])
                more = "" if len(abnormal) <= 5 else f" 等 {len(abnormal)} 日"
                raise LookupError(
                    f"Agent {agent_id} 试运行回放存在结算异常日 {listed}{more}"
                    "（有订单但当日结算未落账——对账不平或数据缺口，"
                    "spec-05 §6.1 规则级异常不允许上线）")
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
            "replay": {
                "window_days": replay["window_days"] if replay else 0,
                "replay_status": replay["status"] if replay else "",
                "replay_dates": replay_dates,
                "sessions": len(replay_dates),
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
        c.execute(
            "UPDATE trial_replays SET status='done', "
            f"updated_ts={ts} WHERE agent_id=? AND status='in_progress'",
            (agent_id,),
        )
    return {
        "archive_id": archive_id,
        "agent": {"id": agent_id, "name": agent["name"], "role": "strategy",
                  "status": next_status},
        "trial_account_id": trial["id"],
        "snapshot": snapshot,
    }

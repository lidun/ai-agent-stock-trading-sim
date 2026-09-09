"""EVOQUANT 验证窗运行闭环（spec-05 §4.1-§4.3 流程侧 + spec-01 §2.7 v0.6）。

流程（本模块为调度环，存储/状态机在 spec-02 §9 strategy_versions，账户在 spec-01
accountstore，全部按真实引擎事件接线，不虚构任何行情/成交/事件）：

  open_validation：修改提案确认 → 建 draft（checkpoint，parent=现役）+ 随建
    role=validation 独立账户（同初始资金、满仓 #62）并登记本窗口台账；验证账户
    active_version_no=新版本——下单引擎默认按账户快照给订单打版本标（spec-01 §2.5）。

  调度（EodSettleTrigger 在确认的交易日结算完成后调用 advance_windows）：
    按真实结算日记 sessions_done；trade_samples=窗口内验证账户该版本真实成交笔数；
    满窗判定 10 交易日或 ≥trade_target 成交（先到为准，参数可调）→ maybe_adjudicate。

  maybe_adjudicate（确定性、零 token，数据源全部真实记录）：
    - 期望值（含费）EV：窗口内该版本卖出成交 realized_pnl/成本基数的平均（%），
      realized_pnl 由引擎卖出成交时写盘（spec-01 记账产物，含双边费）；
    - 基线 = 主账户同期同窗口真实卖出样本的同类均值（现役版本）；
    - 违规 = 验证账户窗口内 condition_orders invalid 规则级拒绝数；
    - 熔断 = 验证账户窗口内 circuit_break_events 合计；
    结论：
      fuse>0 或违规>0 或 EV < -2%             → rollback（否决候选：status=rolled_back
                                               + 失败原因入记忆；主账户保持现役，spec-05
                                               §4.2 v0.5；候选已非 draft 时 sealed 防误执行）；
     验证账户零卖出样本                       → sealed（证据不足封存，不晋升不执行）；
      EV ≥ 基线 或 EV > 0                      → activate（晋升 + 主账户版本指针切换）；
      其余                                    → sealed（低于同期且非正，不晋升）。
    终态快照（账户/版本/统计真实 JSON）写入 final_snapshot 留证，验证账户转 archived，
    与主账户独立结算互不污染（spec-01 §2.7 v0.6）。

窗口 closed/void 后同一 Agent 才能开新验证窗（部分唯一索引兜底）。
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone

from core.db import read_txn, state_conn, write_txn

DEFAULT_WINDOW_DAYS = 10
DEFAULT_TRADE_TARGET = 20
FAIL_EV_PCT = -2.0          # 失败判定：窗口期望值（含费）< -2%（spec-05 §4.2）
_EV_SAMPLE_DIV = 1e-9       # 成本基数过小保护（真实成交几乎不可能触发）


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _audit(cw, *, actor: str, action: str, result: str, detail: str) -> None:
    cw.execute(
        "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
        " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
        (_now_iso(), actor, action, "evoquant", "", result, detail[:400], ""),
    )


def list_windows(state, agent_id: str) -> list[dict]:
    """窗口台账（created_ts 降序）。"""
    conn = state_conn(state)
    rows = conn.execute(
        "SELECT * FROM strategy_validation_windows WHERE agent_id=?"
        " ORDER BY created_ts DESC, id DESC",
        (agent_id,),
    ).fetchall()
    return [_serialize(dict(r)) for r in rows]


def get_window(state, agent_id: str, version_no: str) -> dict | None:
    row = state_conn(state).execute(
        "SELECT * FROM strategy_validation_windows WHERE agent_id=? AND version_no=?",
        (agent_id, version_no),
    ).fetchone()
    return _serialize(dict(row)) if row else None


def _serialize(r: dict) -> dict:
    def _f(v):
        return v if v is None else float(v)

    def _obj(raw: str, fallback):
        try:
            return json.loads(raw or "")
        except ValueError:
            return fallback

    return {
        "id": r["id"], "agent_id": r["agent_id"], "version_no": r["version_no"],
        "status": r["status"],
        "validation_account_id": r["validation_account_id"],
        "main_account_id": r["main_account_id"],
        "window_days": r["window_days"], "trade_target": r["trade_target"],
        "sessions_done": r["sessions_done"], "trade_samples": r["trade_samples"],
        "rule_violations": r["rule_violations"], "fuse_events": r["fuse_events"],
        "expectation": _f(r["expectation"]),
        "baseline_expectation": _f(r["baseline_expectation"]),
        "decision": r["decision"], "decision_reason": r["decision_reason"],
        "window_start_trade_date": r["window_start_trade_date"],
        "final_snapshot": _obj(r["final_snapshot"], {}),
        "decided_ts": r["decided_ts"],
        "created_ts": r["created_ts"], "updated_ts": r["updated_ts"],
    }


def _active_version_no(state, agent_id: str) -> str:
    row = state_conn(state).execute(
        "SELECT version_no FROM strategy_versions"
        " WHERE agent_id=? AND status='active'", (agent_id,),
    ).fetchone()
    return row["version_no"] if row else ""


def open_validation(state, agent_id: str, *, version_no: str, config: dict,
                    basis: list | None = None, config_diff: dict | None = None,
                    window_days: int = DEFAULT_WINDOW_DAYS,
                    trade_target: int = DEFAULT_TRADE_TARGET,
                    created_by: str = "strategy_agent") -> dict:
    """修改提案确认 → 建 draft + 验证账户 + 窗口台账（EVOQUANT 第②③步，spec-05 §4.2）。

    真实引擎流入口（strategy_agent/manager 决策调用）：策略 config 全量快照入
    strategy_versions（checkpoint），独立验证账户随建（同 Agent 至多一个在跑窗口，
    占用约束由 status='in_progress' 断言），账户 active_version_no 指向待验版本。
    """
    if not isinstance(window_days, int) or not (1 <= window_days <= 60):
        raise ValueError(f"window_days 须为 1-60 整数，收到 {window_days!r}")
    if not isinstance(trade_target, int) or not (1 <= trade_target <= 500):
        raise ValueError(f"trade_target 须为 1-500 整数，收到 {trade_target!r}")
    if created_by not in ("strategy_agent", "manager"):
        raise ValueError("created_by 须为 strategy_agent/manager 之一")
    conn = state_conn(state)
    if conn.execute(
        "SELECT 1 FROM strategy_validation_windows"
        " WHERE agent_id=? AND status='in_progress'", (agent_id,),
    ).fetchone():
        raise LookupError(f"Agent {agent_id} 已有在跑验证窗，不可并发开新窗")
    ag = conn.execute(
        "SELECT id, status FROM agents WHERE id=?", (agent_id,)
    ).fetchone()
    if ag is None:
        raise LookupError(f"Agent 不存在：{agent_id}")

    # 1) spec-02 §9：建 draft（config 快照/演进记忆/审计；同版本重复即拒绝）
    from core import strategy_versions as sv  # noqa: PLC0415
    draft = sv.checkpoint(
        state, agent_id, version_no=version_no, config=config,
        basis=basis or [], config_diff=config_diff or {},
        trial_window={"window_days": window_days, "trade_target": trade_target},
        created_by=created_by,
    )
    # 2) 独立验证账户（同 Agent 复用既有；live Agent 才可开）
    from core import accountstore  # noqa: PLC0415
    account = accountstore.provision_validation_account(state, agent_id=agent_id)
    main_id = accountstore.get_account(state, agent_id)
    if main_id is None:
        raise LookupError(f"Agent {agent_id} 无主账户")
    if main_id["role"] != "main":
        raise LookupError(f"Agent {agent_id} 主账户缺位")

    baseline_version = _active_version_no(state, agent_id)
    now = _now_iso()
    wid = "svw" + secrets.token_hex(10)
    with write_txn(conn) as c:
        c.execute(
            "INSERT INTO strategy_validation_windows"
            " (id, agent_id, version_no, status, validation_account_id,"
            "  main_account_id, window_days, trade_target, sessions_done,"
            "  trade_samples, rule_violations, fuse_events, expectation,"
            "  baseline_expectation, decision, decision_reason,"
            "  window_start_trade_date, final_snapshot, decided_ts,"
            "  created_ts, updated_ts)"
            " VALUES (?,?,?,?,?,?,?,?,0,0,0,0,NULL,NULL,'','','','{}','',?,?)",
            (wid, agent_id, version_no, "in_progress",
             account["id"], main_id["id"], window_days, trade_target,
             now, now),
        )
        # 账户版本指针 → 新版本（下单/引擎默认快照继承，spec-01 §2.5）
        c.execute(
            "UPDATE accounts SET active_version_no=?, updated_ts=? WHERE id=?",
            (version_no, now, account["id"]),
        )
        _audit(c, actor=created_by, action="strategy.window.open", result="draft",
               detail=f"{agent_id} 开验证窗 {version_no}（账户 {account['id']}，"
                      f"窗口 {window_days} 交易日 / ≥{trade_target} 成交，"
                      f"现役基线 {baseline_version or '—'}）")
    return {"draft": draft, "account": account,
            "window": get_window(state, agent_id, version_no),
            "baseline_version_no": baseline_version}


def advance_windows(state, trade_date: str) -> list[dict]:
    """交易日结算完成后推进窗口：登记会话 + 成交样本；满窗即确定性判定收口。

    由 EodSettleTrigger 在**确认的交易日**（当日结算完成/纯跟踪日收盘推进）后调用。
    会话台账 validation_sessions 每 (window, trade_date) 至多一行（幂等重放零重复）；
    空成交日同样计会话（同 trial replay_sessions 语义，spec-05 §6.2 对齐）。
    """
    out: list[dict] = []
    conn = state_conn(state)
    rows = conn.execute(
        "SELECT * FROM strategy_validation_windows"
        " WHERE status='in_progress' ORDER BY created_ts",
    ).fetchall()
    for r in rows:
        w = dict(r)
        res = advance_one(state, w, trade_date)
        if res:
            out.append(res)
    return out


def advance_one(state, w: dict, trade_date: str) -> dict:
    """单窗推进（登记当日会话→重算计数）；达到窗口条件时执行判定。"""
    conn = state_conn(state)
    now = _now_iso()
    with write_txn(conn) as c:
        c.execute(
            "INSERT OR IGNORE INTO validation_sessions"
            " (window_id, account_id, trade_date, created_ts) VALUES (?,?,?,?)",
            (w["id"], w["validation_account_id"], trade_date, now),
        )
    start = w["window_start_trade_date"] or trade_date
    sessions = _sessions_count(state, w["id"])
    samples = _trade_sample_count(state, w["validation_account_id"],
                                  w["version_no"], start, trade_date)
    reached = sessions >= w["window_days"] or samples >= w["trade_target"]
    if not reached:
        if (sessions != w["sessions_done"] or samples != w["trade_samples"]
                or not w["window_start_trade_date"]):
            with write_txn(conn) as c:
                c.execute(
                    "UPDATE strategy_validation_windows SET sessions_done=?,"
                    " trade_samples=?, window_start_trade_date=?, updated_ts=?"
                    " WHERE id=? AND status='in_progress'",
                    (sessions, samples, start, now, w["id"]),
                )
        return {"id": w["id"], "agent_id": w["agent_id"],
                "version_no": w["version_no"], "reached": False,
                "sessions_done": sessions, "trade_samples": samples}
    return decide(state, w, trade_date=trade_date)


def _sessions_count(state, window_id: str) -> int:
    row = state_conn(state).execute(
        "SELECT COUNT(*) AS n FROM validation_sessions WHERE window_id=?",
        (window_id,),
    ).fetchone()
    return int(row["n"])


def _trade_sample_count(state, account_id: str, version_no: str,
                        d0: str, d1: str) -> int:
    row = state_conn(state).execute(
        "SELECT COUNT(*) AS n FROM trades"
        " WHERE account_id=? AND strategy_version_no=? AND settle_date BETWEEN ? AND ?",
        (account_id, version_no, d0, d1),
    ).fetchone()
    return int(row["n"])


def decide(state, w: dict, *, trade_date: str) -> dict:
    """满窗判定（真实记录 → 结论），收口状态机并留证归档（spec-05 §4.2 判定表）。"""
    conn = state_conn(state)
    start = w["window_start_trade_date"] or trade_date
    with read_txn(conn) as c:
        sessions = _sessions_count(state, w["id"])
        samples = _trade_sample_count(state, w["validation_account_id"],
                                      w["version_no"], start, trade_date)
        val_stats = _ev_stats(state, w["validation_account_id"],
                              w["version_no"], start, trade_date)
        baseline_no = _baseline_version_no(state, w["agent_id"], w["version_no"])
        base_stats = {"n": 0, "ev": None}
        if w["main_account_id"] and baseline_no:
            base_stats = _ev_stats(state, w["main_account_id"], baseline_no,
                                   start, trade_date)
        viol = c.execute(
            "SELECT COUNT(*) AS n FROM condition_orders"
            " WHERE account_id=? AND created_at>=? AND created_at<=?"
            " AND invalid_reason<>''", (w["validation_account_id"], start,
                                        trade_date + "T23:59:59"),
        ).fetchone()["n"]
        fuse = c.execute(
            "SELECT COALESCE(SUM(circuit_break_events),0) AS n FROM condition_orders"
            " WHERE account_id=?", (w["validation_account_id"],),
        ).fetchone()["n"]

    ev = val_stats["ev"]          # 窗口期望值（含费，%）
    base = base_stats["ev"]
    ev_n = val_stats["n"]
    decision, reason = "", ""
    if fuse > 0 or int(viol) > 0 or (ev is not None and ev < FAIL_EV_PCT):
        decision = "rollback"
        parts = []
        if fuse > 0:
            parts.append(f"熔断事件 {fuse} 次")
        if int(viol) > 0:
            parts.append(f"规则级违规 {viol} 笔")
        if ev is not None and ev < FAIL_EV_PCT:
            parts.append(f"期望值 {ev:.2f}% < {FAIL_EV_PCT:.1f}%")
        reason = "；".join(parts) or "验证失败"
    elif ev_n == 0:
        decision = "sealed"
        reason = (f"窗口满（{sessions} 会话 / 成交 {samples} 笔）但验证账户"
                  "无卖出样本——证据不足封存，不晋升不执行（防假激活）")
    elif ev >= (base if base is not None else 0.0) or ev > 0.0:
        decision = "activate"
        reason = (f"期望值(含费) {ev:.2f}%（{ev_n} 样本）"
                  + (f" ≥ 现役同期 {base:.2f}%" if base is not None
                     else " > 0，且现役同期无样本"))
    else:
        decision = "sealed"
        reason = (f"期望值(含费) {ev:.2f}% 低于现役同期"
                  + (f"（{base:.2f}%）" if base is not None else "")
                  + "且未超 0——不晋升，封存留证")

    # 1) spec-02 §9 状态机副作用（独立账户语义 spec-05 v0.5：主账户保持现役）
    from core import strategy_memory  # noqa: PLC0415
    from core import strategy_versions as sv  # noqa: PLC0415
    activated = ""
    rejected = False
    if decision == "activate":
        fresh = sv.activate(state, w["agent_id"], w["version_no"],
                            activated_by="strategy_agent")
        activated = fresh["version_no"]
    elif decision == "rollback":
        # 候选（draft）验证失败 → 否决候选本身：status=rolled_back + failure_reason，
        # 主账户现役不变（spec-05 §4.2 v0.5：失败→主账户保持现役+验证账户归档+失败
        # 原因入记忆防重复）。绝不把仍现役的主账户误回退到更老 validated。
        conn = state_conn(state)
        with write_txn(conn) as c:
            cur = c.execute(
                "SELECT * FROM strategy_versions WHERE agent_id=? AND version_no=?",
                (w["agent_id"], w["version_no"]),
            ).fetchone()
            if cur and cur["status"] in ("draft", "validated"):
                c.execute(
                    "UPDATE strategy_versions SET status='rolled_back',"
                    " failure_reason=?, rolled_back_to=? WHERE id=?",
                    (reason[:200], cur["parent_version"], cur["id"]),
                )
                rejected = True
        if not rejected:
            decision, reason = "sealed", (
                f"候选 {w['version_no']} 已非 draft/validated（{cur['status'] if cur else '无'}）"
                "——无法按验证失败否决，封存留证")
        else:
            strategy_memory._append(
                state, w["agent_id"], version_no=w["version_no"],
                body=f"验证失败否决：版本 {w['version_no']} 标 rolled_back"
                     f"（原因：{reason}），主账户保持现役运行（spec-05 §4.2 v0.5）。",
                source="engine:rollback", dedup_key=f"engine:rollback:{w['version_no']}")
    # 2) 终态快照留证 + 账户归档 + 主账户版本指针切换（真实账户终态）
    final = {
        "decision": decision, "reason": reason,
        "window_start": start, "last_advance": trade_date,
        "sessions_done": int(sessions), "trade_samples": int(samples),
        "expectation_pct": ev, "baseline_expectation_pct": base,
        "validation_ev_n": ev_n, "baseline_ev_n": base_stats["n"],
        "rule_violations": int(viol), "fuse_events": int(fuse),
        "activated": activated,
        "candidate_rejected": rejected, "candidate_status": "rolled_back",
    }
    now = _now_iso()
    with write_txn(conn) as c:
        acc = c.execute(
            "SELECT * FROM accounts WHERE id=?", (w["validation_account_id"],)
        ).fetchone()
        main = c.execute(
            "SELECT * FROM accounts WHERE id=?", (w["main_account_id"],)
        ).fetchone()
        snap = {
            "validation_account": {k: acc[k] for k in (
                "id", "role", "cash", "nav", "shares", "total_pnl", "today_pnl",
                "status", "active_version_no")},
            "main_account": {k: main[k] for k in (
                "id", "role", "cash", "nav", "shares", "total_pnl", "today_pnl",
                "status", "active_version_no")} if main else {},
            "final": final,
        }
        c.execute(
            "UPDATE strategy_validation_windows SET status='done',"
            " sessions_done=?, trade_samples=?, rule_violations=?, fuse_events=?,"
            " expectation=?, baseline_expectation=?, decision=?, decision_reason=?,"
            " final_snapshot=?, decided_ts=?, updated_ts=? WHERE id=? AND status='in_progress'",
            (int(sessions), int(samples), int(viol), int(fuse), ev, base,
             decision, reason, json.dumps(snap, ensure_ascii=False),
             now, now, w["id"]),
        )
        c.execute(
            "UPDATE accounts SET status='archived', updated_ts=? WHERE id=?",
            (now, w["validation_account_id"]),
        )
        if activated:
            c.execute(
                "UPDATE accounts SET active_version_no=?, updated_ts=? WHERE id=?",
                (activated, now, w["main_account_id"]),
            )
        _audit(c, actor="strategy_agent",
               action="strategy.window.decide", result=decision,
               detail=f"{w['agent_id']} 验证窗 {w['version_no']} 收口：{reason}")
    return {"id": w["id"], "agent_id": w["agent_id"],
            "version_no": w["version_no"], "reached": True,
            "decision": decision, "reason": reason, "sessions_done": int(sessions),
            "trade_samples": int(samples)}


def _baseline_version_no(state, agent_id: str, version_no: str) -> str:
    """窗口基线 = 待验版本 parent（现役），回退候选语义一致。"""
    row = state_conn(state).execute(
        "SELECT parent_version FROM strategy_versions"
        " WHERE agent_id=? AND version_no=?", (agent_id, version_no),
    ).fetchone()
    return (row["parent_version"] if row and row["parent_version"] else
            _active_version_no(state, agent_id))


def _ev_stats(state, account_id: str, version_no: str, d0: str, d1: str) -> dict:
    """窗口真实卖出样本：单笔已实现净收益率 % = realized_pnl/成本基数×100，取均值。

    成本基数 = 卖出净额 − 已实现盈亏 = avg_cost×qty（含费摊薄，引擎写盘口径）。
    零成交 → n=0、ev=None（统计值，不出结论）。
    """
    from decimal import ROUND_HALF_UP, Decimal  # noqa: PLC0415
    rows = state_conn(state).execute(
        "SELECT amount, fee_total, realized_pnl FROM trades"
        " WHERE account_id=? AND strategy_version_no=? AND side='sell'"
        " AND settle_date BETWEEN ? AND ?",
        (account_id, version_no, d0, d1),
    ).fetchall()
    total = Decimal("0")
    n = 0
    for r in rows:
        net = Decimal(str(r["amount"])) - Decimal(str(r["fee_total"] or 0))
        cost = net - Decimal(str(r["realized_pnl"] or 0))
        if cost <= 0:
            continue
        pct = Decimal(str(r["realized_pnl"] or 0)) / cost * Decimal("100")
        total += pct
        n += 1
    if n == 0:
        return {"n": 0, "ev": None}
    ev = (total / n).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {"n": n, "ev": float(ev)}

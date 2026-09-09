"""策略版本化存储与演进状态机（spec-02 §9 存储/API，EVOQUANT 消费方）。

表 strategy_versions 存"执行层参数全量快照 + 演进状态"，与大纲版本链
（strategy_charter_versions，理念层）解耦：理念=锁定的核心框架；本表=每次
优化提案的 config 快照与 晋升/回滚 状态。

状态机：
  checkpoint(agent, config, basis)  建 draft（parent=现役版本，幂等失败不落行）
  activate(version_no)              验证通过晋升：候选→active(validated_on)，
                                    旧 active 降为 validated（回退候选）
  rollback(agent, reason)           失败回退：现役标 rolled_back(+failure_reason、
                                    rolled_back_to)，最近 validated 重新 active；
                                    无 validated 候选 → 拒绝执行并告警管理 Agent
                                    （spec-02 §9 rollback 边界语义），执行层保持现役。
不变量：每 Agent 至多一个 active（部分唯一索引兜底 + 应用层同事务复位）。
演进记录（memory_entries type=strategy）逐条挂 version_no——本模块作为其
**公开写方（引擎 EVOQUANT 优化流）**，替代 strategy_memory._append 的壳角色。
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone

from core.db import state_conn, write_txn

STATUSES = ("draft", "validated", "active", "rolled_back")
CREATORS = ("strategy_agent", "manager")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _audit(cw, *, actor: str, action: str, result: str, detail: str,
           object_id: str = "") -> None:
    cw.execute(
        "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
        " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
        (_now_iso(), actor, action, "strategy_versions", object_id,
         result, detail[:400], ""),
    )


def _require_agent(state, agent_id: str) -> None:
    if state_conn(state).execute(
        "SELECT 1 FROM agents WHERE id=?", (agent_id,)
    ).fetchone() is None:
        raise LookupError(f"Agent 不存在：{agent_id}")


def _serialize(r) -> dict:
    def _obj(raw: str, fallback):
        try:
            return json.loads(raw or "")
        except ValueError:
            return fallback
    return {
        "id": r["id"], "agent_id": r["agent_id"], "version_no": r["version_no"],
        "parent_version": r["parent_version"], "status": r["status"],
        "config": _obj(r["config"], {}),
        "config_diff": _obj(r["config_diff"], {}),
        "basis": _obj(r["basis"], []),
        "created_by": r["created_by"], "created_ts": r["created_ts"],
        "trial_window": _obj(r["trial_window"], {}),
        "validated_on": r["validated_on"],
        "failure_reason": r["failure_reason"], "rolled_back_to": r["rolled_back_to"],
    }


def list_versions(state, agent_id: str, *, status: str = "",
                  limit: int = 200) -> dict:
    """演进史（created_ts 降序）。"""
    _require_agent(state, agent_id)
    if status and status not in STATUSES:
        raise ValueError(f"未知状态：{status}")
    limit = max(1, min(int(limit), 500))
    c = state_conn(state)
    rows = c.execute(
        "SELECT * FROM strategy_versions WHERE agent_id=? AND (? = '' OR status = ?)"
        " ORDER BY created_ts DESC, id DESC LIMIT ?",
        (agent_id, status, status, limit),
    ).fetchall()
    items = [_serialize(r) for r in rows]
    active = c.execute(
        "SELECT version_no FROM strategy_versions WHERE agent_id=? AND status='active'",
        (agent_id,),
    ).fetchone()
    return {
        "agent_id": agent_id, "total": len(items),
        "active_version": active["version_no"] if active else "",
        "items": items,
    }


def get_version(state, agent_id: str, version_no: str) -> dict | None:
    row = state_conn(state).execute(
        "SELECT * FROM strategy_versions WHERE agent_id=? AND version_no=?",
        (agent_id, version_no),
    ).fetchone()
    return _serialize(row) if row else None


def _record_event(state, agent_id: str, version_no: str, *, event: str,
                  body: str, ref_ids: list | None = None) -> None:
    """演进记忆事件写入（EVOQUANT 消费：记忆逐条挂 version_no，spec-02 §9）。"""
    from core import strategy_memory  # noqa: PLC0415
    try:
        strategy_memory._append(
            state, agent_id, version_no=version_no, body=body,
            ref_ids=ref_ids or [], source=f"engine:{event}",
            dedup_key=f"engine:{event}:{version_no}")
    except LookupError:
        raise
    except Exception:  # noqa: BLE001 - 记忆写入不阻断版本状态机
        pass


def checkpoint(state, agent_id: str, *, version_no: str,
               config: dict, basis: list | None = None,
               config_diff: dict | None = None,
               trial_window: dict | None = None,
               created_by: str = "strategy_agent") -> dict:
    """EVOQUANT 修改提案确认 → 建验证版本（status=draft，parent=现役）。"""
    if created_by not in CREATORS:
        raise ValueError(f"created_by 须为 {'/'.join(CREATORS)} 之一")
    if not isinstance(config, dict) or not config:
        raise ValueError("config 须为非空执行层参数快照对象")
    _require_agent(state, agent_id)
    if get_version(state, agent_id, version_no) is not None:
        raise ValueError(f"版本已存在：{version_no}")
    now = _now_iso()
    vid = "sv" + secrets.token_hex(10)
    parent = ""
    c = state_conn(state)
    with write_txn(c) as cw:
        cur_active = cw.execute(
            "SELECT version_no FROM strategy_versions"
            " WHERE agent_id=? AND status='active'", (agent_id,)).fetchone()
        parent = cur_active["version_no"] if cur_active else ""
        refs = basis or []
        diff = config_diff or {}
        cw.execute(
            "INSERT INTO strategy_versions (id, agent_id, version_no, parent_version,"
            " status, config, config_diff, basis, created_by, created_ts, trial_window)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (vid, agent_id, version_no, parent, "draft",
             json.dumps(config, ensure_ascii=False, sort_keys=True),
             json.dumps(diff, ensure_ascii=False, sort_keys=True),
             json.dumps(refs, ensure_ascii=False),
             created_by, now,
             json.dumps(trial_window or {}, ensure_ascii=False)),
        )
        detail = (f"{agent_id} checkpoint {version_no}（parent={parent or '—'}，"
                  f"basis 引用 {len(refs)} 条）")
        _audit(cw, actor=created_by, action="strategy.checkpoint",
               result="draft", detail=detail)
    ev = version_no or ""
    ref_ids = list(refs)[:5]
    _record_event(
        state, agent_id, ev, event="checkpoint",
        body=f"优化提案确认：checkpoint 版本 {ev}"
             f"（parent={parent or '—'}）。依据={refs or '—'}；"
             f"验证参数={trial_window or '—'}。",
        ref_ids=refs,
    )
    return get_version(state, agent_id, version_no)


def activate(state, agent_id: str, version_no: str, *,
             activated_by: str = "manager",
             event_body: str | None = None) -> dict:
    """晋升全仓：候选（draft/validated）→ active，旧 active 降 validated（回退候选）。

    event_body：EVOQUANT 调用方可将窗口收口证据写入演进记忆正文；默认保留
    通用文案（幂等 dedup=engine:activate:<version>，重复判定不覆盖首次留证）。"""
    _require_agent(state, agent_id)
    if activated_by not in CREATORS:
        raise ValueError(f"activated_by 须为 {'/'.join(CREATORS)} 之一")
    now = _now_iso()
    c = state_conn(state)
    with write_txn(c) as cw:
        row = cw.execute(
            "SELECT * FROM strategy_versions WHERE agent_id=? AND version_no=?",
            (agent_id, version_no),
        ).fetchone()
        if row is None:
            raise LookupError(f"版本不存在：{agent_id}@{version_no}")
        if row["status"] == "active":
            _audit(cw, actor=activated_by, action="strategy.activate",
                   result="active", detail=f"幂等：{version_no} 已是 active")
            return _serialize(row)
        if row["status"] not in ("draft", "validated"):
            raise ValueError(f"版本 {version_no} 状态 {row['status']} 不可晋升")
        cw.execute(
            "UPDATE strategy_versions SET status='validated'"
            " WHERE agent_id=? AND status='active' AND version_no<>?",
            (agent_id, version_no),
        )
        cw.execute(
            "UPDATE strategy_versions SET status='active', validated_on=?"
            " WHERE agent_id=? AND version_no=?",
            (now, agent_id, version_no),
        )
        _audit(cw, actor=activated_by, action="strategy.activate",
               result="active",
               detail=f"{agent_id} 晋升 {version_no}（validated_on={now}）")
    _record_event(
        state, agent_id, version_no, event="activate",
        body=event_body or f"验证通过：版本 {version_no} 晋升全仓（validated_on）。")
    fresh = get_version(state, agent_id, version_no)
    return fresh


def rollback(state, agent_id: str, *, reason: str,
             rolled_back_by: str = "manager") -> dict:
    """失败回退到最近 validated；无候选时拒绝执行并告警管理 Agent（保持现役）。"""
    if not reason or not reason.strip():
        raise ValueError("回退须提供原因")
    reason = reason.strip()
    if rolled_back_by not in CREATORS:
        raise ValueError(f"rolled_back_by 须为 {'/'.join(CREATORS)} 之一")
    _require_agent(state, agent_id)
    now = _now_iso()
    c = state_conn(state)
    with write_txn(c) as cw:
        active = cw.execute(
            "SELECT * FROM strategy_versions WHERE agent_id=? AND status='active'",
            (agent_id,),
        ).fetchone()
        if active is None:
            _audit(cw, actor=rolled_back_by, action="strategy.rollback",
                   result="no_active", detail=f"{agent_id} 无现役版本，回退拒绝")
            return {"ok": False, "reason": "no_active",
                    "detail": "无现役版本可回退"}
        target = cw.execute(
            "SELECT * FROM strategy_versions WHERE agent_id=? AND status='validated'"
            " AND version_no<>? ORDER BY created_ts DESC, id DESC LIMIT 1",
            (agent_id, active["version_no"]),
        ).fetchone()
        if target is None:
            _audit(cw, actor=rolled_back_by, action="strategy.rollback",
                   result="no_validated_target",
                   detail=f"{agent_id} 无 validated 候选，拒绝执行（spec-02 §9）")
        else:
            cw.execute(
                "UPDATE strategy_versions SET status='rolled_back', failure_reason=?,"
                " rolled_back_to=? WHERE id=?",
                (reason, target["version_no"], active["id"]),
            )
            cw.execute(
                "UPDATE strategy_versions SET status='active', validated_on=?"
                " WHERE id=?",
                (now, target["id"]),
            )
            _audit(cw, actor=rolled_back_by, action="strategy.rollback",
                   result="rolled_back",
                   detail=f"{agent_id} 回退 {active['version_no']} → "
                          f"{target['version_no']}（原因：{reason[:200]}）")
    if target is None:
        _alert_manager(state, agent_id, active_version=active["version_no"],
                       reason=reason)
        return {"ok": False, "reason": "no_validated_target",
                "detail": "无已 validated 版本可回退——拒绝执行，保持当前版本运行，"
                          "已转管理 Agent 待办（spec-02 §9 边界语义）",
                "active_version": active["version_no"]}
    _record_event(
        state, agent_id, active["version_no"], event="rollback",
        body=f"验证失败回退：版本 {active['version_no']} 标 rolled_back"
             f"（原因：{reason}），回退至最近 validated {target['version_no']}。")
    return {"ok": True, "rolled_back": active["version_no"],
            "active_version": target["version_no"]}


def _alert_manager(state, agent_id: str, *, active_version: str, reason: str) -> None:
    """无 validated 候选告警 → 管理 Agent 用户会话（spec-04 待办语义）。"""
    from core import chatstore  # noqa: PLC0415
    body = (
        "## 策略回退被拒 · 转管理 Agent 待办（spec-02 §9）\n"
        f"- Agent：{agent_id}\n"
        f"- 现役版本：{active_version}（无已 validated 版本可回退）\n"
        f"- 失败原因：{reason}\n"
        "- 处置：手动暂停 / 指定回退目标；执行层保持现役继续运行"
    )
    payload = json.dumps({"agent_id": agent_id, "active_version": active_version,
                          "reason": reason[:200]}, ensure_ascii=False, sort_keys=True)
    try:
        conv = chatstore.ensure_user_chat(state, "agent-manager")
    except LookupError:
        return
    conn = state_conn(state)
    if conn.execute("SELECT 1 FROM messages WHERE conv_id=? AND payload_ref=? LIMIT 1",
                    (conv["id"], payload)).fetchone():
        return
    try:
        chatstore.insert_message(
            state, conv_id=conv["id"], agent_id="agent-manager", direction="agent",
            msg_type="strategy_alert", body=body, payload_ref=payload,
            status="delivered", delivered_via="web")
    except Exception:  # noqa: BLE001
        pass

"""调度任务表（spec-04 §2.1）与可延迟任务组（§2.5）最小实现。

本切片提供「入队 + 到期认领 + 处理器执行」闭环，供生命周期事件（归档经验提取）
与后续确定性任务（kb_stats 刷新、分层摘要补跑等）复用；tick 循环与资源闸门（§2.2/§7.2）
后续补，当前由 `run_deferrable` 在调用点（手动/空闲窗口）驱动物。

幂等：UNIQUE(agent_id, task_type, trade_date, task_slot, dedup_key)——重复入队返回
既有任务（created=False）；事件触发任务用业务子键区分同日多事件。
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Callable

from core.db import state_conn, write_txn

TASK_STATUS = ("pending", "running", "done", "failed", "skipped", "expired", "partial")
RESOURCE_CLASSES = ("scan", "llm-heavy", "light")

# spec-04 §3.3：interrupt 挂起超时（默认 30 分钟，可配）→ 保守拒绝恢复
INTERRUPT_TIMEOUT_MINUTES = 30

# spec-04 §2.1 task_type 枚举（含可延迟组 §2.5 成员：经验提取/kb_stats刷新/分层摘要补跑…）
TASK_TYPES = (
    "选股", "挂单", "结算", "日报", "总汇报", "复盘", "摘要补跑", "备份",
    "L0清理", "体检报告", "补救启动", "健康检查", "试运行回放", "验证评估",
    "经验提取", "kb_stats刷新",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _audit(state, *, action: str, result: str, object_id: str, actor: str,
           detail: str) -> None:
    with write_txn(state_conn(state)) as c:
        c.execute(
            "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
            " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
            (_now_iso(), actor, action, "task_schedule", object_id, result,
             detail[:400], ""),
        )


def enqueue(state, *, task_type: str, agent_id: str = "", trade_date: str = "",
            task_slot: str = "", dedup_key: str = "", payload: dict | None = None,
            resource_class: str = "light", is_deferrable: bool = True,
            priority: int = 5, scheduled_ts: str = "",
            actor: str = "scheduler") -> dict:
    """入队任务（幂等）；返回 {id, created}。created=False 表示命中既有幂等键。"""
    task_type = (task_type or "").strip()
    if not task_type:
        raise ValueError("task_type 不可为空")
    if resource_class not in RESOURCE_CLASSES:
        raise ValueError(f"未知 resource_class：{resource_class}")
    task_id = "t" + secrets.token_hex(10)
    ts = _now_iso()
    with write_txn(state_conn(state)) as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO task_schedule"
            " (id, agent_id, task_type, task_slot, trade_date, status,"
            "  scheduled_ts, resource_class, is_deferrable, priority, dedup_key,"
            "  payload, created_ts)"
            " VALUES (?,?,?,?,?, 'pending', ?,?,?,?,?,?,?)",
            (task_id, agent_id, task_type, task_slot, trade_date,
             scheduled_ts or ts, resource_class, 1 if is_deferrable else 0,
             int(priority), dedup_key, json.dumps(payload or {}, ensure_ascii=False), ts),
        )
        created = cur.rowcount == 1
        if not created:
            row = c.execute(
                "SELECT id FROM task_schedule WHERE agent_id=? AND task_type=?"
                " AND trade_date=? AND task_slot=? AND dedup_key=?",
                (agent_id, task_type, trade_date, task_slot, dedup_key),
            ).fetchone()
            task_id = row["id"]
    if created:
        _audit(state, action="task.enqueue", result="pending", object_id=task_id,
               actor=actor,
               detail=f"{task_type} agent={agent_id or '-'} deferrable={is_deferrable}")
    return {"id": task_id, "created": created}


def _row(r) -> dict:
    def _loads(s):
        try:
            return json.loads(s or "{}")
        except ValueError:
            return {}
    return {
        "id": r["id"], "agent_id": r["agent_id"], "task_type": r["task_type"],
        "task_slot": r["task_slot"], "trade_date": r["trade_date"],
        "status": r["status"], "scheduled_ts": r["scheduled_ts"],
        "started_ts": r["started_ts"], "ended_ts": r["ended_ts"],
        "attempt_count": r["attempt_count"], "last_error": r["last_error"],
        "resource_class": r["resource_class"],
        "is_deferrable": bool(r["is_deferrable"]), "priority": r["priority"],
        "dedup_key": r["dedup_key"], "payload": _loads(r["payload"]),
        "interrupt_entered_ts": r["interrupt_entered_ts"],
        "approval_id": r["approval_id"], "interrupt_note": r["interrupt_note"],
        "created_ts": r["created_ts"],
    }


def get_task(state, task_id: str) -> dict | None:
    row = state_conn(state).execute(
        "SELECT * FROM task_schedule WHERE id=?", (task_id,)).fetchone()
    return _row(row) if row else None


def list_tasks(state, *, status: str = "", task_type: str = "", agent_id: str = "",
               is_deferrable: bool | None = None, limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM task_schedule WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status=?"
        params.append(status)
    if task_type:
        sql += " AND task_type=?"
        params.append(task_type)
    if agent_id:
        sql += " AND agent_id=?"
        params.append(agent_id)
    if is_deferrable is not None:
        sql += " AND is_deferrable=?"
        params.append(1 if is_deferrable else 0)
    sql += " ORDER BY priority ASC, created_ts ASC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    return [_row(r) for r in state_conn(state).execute(sql, params).fetchall()]


def claim_due(state, *, task_type: str = "", deferrable_only: bool = False,
              limit: int = 10, now: str = "") -> list[dict]:
    """认领到期 pending 任务（scheduled_ts ≤ now）→ running，attempt_count+1。

    单连接内先选后改，避免同 tick 重复投递。tick 循环未落地时由 run_deferrable 驱动。
    """
    now = now or _now_iso()
    sql = ("SELECT id FROM task_schedule WHERE status='pending' AND scheduled_ts<=?")
    params: list = [now]
    if task_type:
        sql += " AND task_type=?"
        params.append(task_type)
    if deferrable_only:
        sql += " AND is_deferrable=1"
    sql += " ORDER BY priority ASC, created_ts ASC LIMIT ?"
    params.append(max(1, min(int(limit), 200)))
    claimed: list[dict] = []
    with write_txn(state_conn(state)) as c:
        ids = [r["id"] for r in c.execute(sql, params).fetchall()]
        for tid in ids:
            c.execute(
                "UPDATE task_schedule SET status='running', started_ts=?,"
                " attempt_count=attempt_count+1 WHERE id=? AND status='pending'",
                (now, tid))
        for tid in ids:
            row = c.execute("SELECT * FROM task_schedule WHERE id=?", (tid,)).fetchone()
            claimed.append(_row(row))
    return claimed


def finish(state, task_id: str, *, ok: bool = True, status: str = "",
           detail: str = "", actor: str = "scheduler") -> dict | None:
    """收尾任务：ok=True→done，ok=False→failed（或显式 status）。"""
    final = status or ("done" if ok else "failed")
    if final not in TASK_STATUS:
        raise ValueError(f"未知任务状态：{final}")
    with write_txn(state_conn(state)) as c:
        c.execute(
            "UPDATE task_schedule SET status=?, ended_ts=?, last_error=? WHERE id=?",
            (final, _now_iso(), "" if ok else detail[:400], task_id))
    _audit(state, action="task.finish", result=final, object_id=task_id,
           actor=actor, detail=detail[:400] or "ok")
    return get_task(state, task_id)


def enter_interrupt(state, task_id: str, *, approval_id: str = "",
                    note: str = "", now: str = "",
                    actor: str = "scheduler") -> dict | None:
    """标记任务进入 interrupt 审批挂起（spec-04 §3.3 节点包装器调用点）。

    图执行已返回但任务行保持 running；写 interrupt_entered_ts，供 tick 超时扫描。
    重复进入（已挂起）保持首次时刻不变，幂等。
    """
    ts = now or _now_iso()
    with write_txn(state_conn(state)) as c:
        c.execute(
            "UPDATE task_schedule SET interrupt_entered_ts=?, approval_id=?,"
            " interrupt_note=? WHERE id=? AND interrupt_entered_ts=''",
            (ts, approval_id, note[:400], task_id),
        )
    _audit(state, action="task.interrupt", result="suspended", object_id=task_id,
           actor=actor,
           detail=f"approval={approval_id or '-'} note={note[:200] or '-'}")
    return get_task(state, task_id)


def scan_interrupt_timeouts(state, *, timeout_minutes: int = INTERRUPT_TIMEOUT_MINUTES,
                            now: datetime | None = None,
                            actor: str = "scheduler") -> list[dict]:
    """扫描 interrupt 超时任务（spec-04 §2.2 第 6 项 / §3.3）。

    对 running 且 interrupt_entered_ts 超过阈值的任务执行「保守拒绝」恢复：
    ① 联动关闭来源审批单（pending → expired，close_note 标注超时拒绝，§4.4）；
    ② 清空 interrupt_entered_ts 防重复扫描；
    ③ 无引擎可 resume 的桩环境下任务收尾为 partial（该分支跳过、不阻塞空闲窗口）。
    接入真实图后由节点包装器在 resume 后自行 finish，扫描仅作超时兜底。
    """
    now_utc = now or datetime.now(timezone.utc)
    cutoff = (now_utc - timedelta(minutes=max(1, int(timeout_minutes)))
              ).isoformat(timespec="seconds")
    c = state_conn(state)
    rows = c.execute(
        "SELECT * FROM task_schedule WHERE status='running'"
        " AND interrupt_entered_ts!='' AND interrupt_entered_ts<=?",
        (cutoff,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        task = _row(r)
        note = "审批超时未处理，维持原状（保守拒绝）"
        approval_id = task["approval_id"]
        closed = False
        if approval_id:
            try:
                from core import approval  # noqa: PLC0415
                closed = approval.close_for_interrupt(
                    state, approval_id, note="图分支已超时拒绝", actor=actor)
            except Exception:  # noqa: BLE001 - 关单失败不阻断扫描
                closed = False
        ts = now_utc.isoformat(timespec="seconds")
        with write_txn(state_conn(state)) as cw:
            cw.execute(
                "UPDATE task_schedule SET status='partial', interrupt_entered_ts='',"
                " interrupt_note=?, ended_ts=?, last_error=?"
                " WHERE id=? AND status='running' AND interrupt_entered_ts!=''",
                (note, ts, note, task["id"]),
            )
        _audit(state, action="task.interrupt_timeout", result="partial",
               object_id=task["id"], actor=actor,
               detail=f"挂起 {timeout_minutes} 分钟超时 → 保守拒绝；"
                      f"审批单 {approval_id or '-'} 关闭={closed}")
        out.append({"id": task["id"], "task_type": task["task_type"],
                    "approval_id": approval_id, "approval_closed": closed,
                    "note": note})
    return out


def _run_retro_extraction(state, task: dict) -> str:
    from core import retrospective  # noqa: PLC0415
    report_id = (task.get("payload") or {}).get("report_id", "")
    if not report_id:
        raise ValueError("经验提取任务缺少 payload.report_id")
    rpt = retrospective.generate_attribution(state, report_id, actor="manager")
    return f"报告 {report_id} 归因={rpt['attribution_status']}"


def _run_kb_stats_refresh(state, task: dict) -> str:
    """日内结算后 kb_stats 刷新的兜底（§2.5⑦）：确定性重算，零 token。"""
    from core import kb  # noqa: PLC0415
    r = kb.recompute_stats(state)
    return f"重算 {r['entries']} 条目 / {r['buckets']} 桶"


_HANDLERS: dict[str, Callable[[object, dict], str]] = {
    "经验提取": _run_retro_extraction,
    "kb_stats刷新": _run_kb_stats_refresh,
}


def run_deferrable(state, *, task_type: str = "", limit: int = 10,
                   actor: str = "scheduler") -> dict:
    """认领并执行到期可延迟任务（§2.5 空闲窗口执行的调用点）。

    无处理器/处理器异常不中断其余任务；LLM 未配置属确定性降级（记 done，detail 标注）。
    """
    claimed = claim_due(state, task_type=task_type, deferrable_only=True, limit=limit)
    items: list[dict] = []
    for task in claimed:
        handler = _HANDLERS.get(task["task_type"])
        if handler is None:
            finish(state, task["id"], ok=False, status="skipped",
                   detail=f"无可延迟处理器：{task['task_type']}", actor=actor)
            items.append({"id": task["id"], "task_type": task["task_type"],
                          "status": "skipped", "detail": "无处理器"})
            continue
        try:
            detail = handler(state, task) or "ok"
            finish(state, task["id"], ok=True, detail=detail, actor=actor)
            items.append({"id": task["id"], "task_type": task["task_type"],
                          "status": "done", "detail": detail})
        except Exception as exc:  # noqa: BLE001 —— 单任务失败不影响整批
            msg = f"{type(exc).__name__}: {exc}"[:400]
            finish(state, task["id"], ok=False, detail=msg, actor=actor)
            items.append({"id": task["id"], "task_type": task["task_type"],
                          "status": "failed", "detail": msg})
    done = sum(1 for i in items if i["status"] == "done")
    return {"claimed": len(claimed), "done": done,
            "failed": sum(1 for i in items if i["status"] == "failed"),
            "skipped": sum(1 for i in items if i["status"] == "skipped"),
            "items": items}

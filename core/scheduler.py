"""调度器 tick 最小实现（spec-04 §2.2/§2.5）。

本切片实现 §2.2 清单中不依赖引擎回调的确定性项：
1. 可延迟任务组窗口判定（§2.5）：非交易时段 ∧ 无 running 任务 ∧ 本地资源余量 ≥30% 余量线；
2. 审批单过期扫描（§4.4）；
3. 空闲窗口满足时按优先级执行到期可延迟任务。

引擎结算/跟踪回调（§2.2 第 2-4 项）与 interrupt 扫描（§3.3）仍由各自模块驱动，待补。
tick 由外部（常驻循环/手动路由）调用，保持幂等：可重复调用不产生重复副作用。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core import approval, resource_gate, tasks
from core.db import state_conn, write_txn

_BJ = timezone(timedelta(hours=8))
_AM = (9 * 60 + 30, 11 * 60 + 30)
_PM = (13 * 60, 15 * 60)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_trading_hours(now_utc: datetime) -> bool:
    """A 股交易时段（北京时间，周一~周五 09:30-11:30 / 13:00-15:00）。

    不含节假日日历（spec-03 calendar 未落地）——节假日会被判为交易时段而不触发空闲窗口，
    属保守行为（宁可不在节假日执行可延迟任务），无副作用。
    """
    bj = now_utc.astimezone(_BJ)
    if bj.weekday() >= 5:
        return False
    minutes = bj.hour * 60 + bj.minute
    return (_AM[0] <= minutes <= _AM[1]) or (_PM[0] <= minutes <= _PM[1])


def _running_count(state) -> int:
    return state_conn(state).execute(
        "SELECT COUNT(*) AS n FROM task_schedule WHERE status='running'").fetchone()["n"]


def idle_window(state, *, now: datetime | None = None,
                capacity: dict | None = None) -> dict:
    """§2.5 空闲窗口判定：非交易时段 ∧ 无 running 任务 ∧ 资源余量 ≥30%。"""
    now_utc = now or datetime.now(timezone.utc)
    cap = capacity or resource_gate.sample_capacity()
    running = _running_count(state)
    trading = _is_trading_hours(now_utc)
    enough = resource_gate.admit_new_tasks(cap)
    idle = (not trading) and running == 0 and enough
    reasons = []
    if trading:
        reasons.append("交易时段")
    if running:
        reasons.append(f"{running} 个任务 running")
    if not enough:
        reasons.append("资源余量低于 30% 线")
    return {"idle": idle, "trading_hours": trading, "running": running,
            "capacity": cap, "reason": "空闲窗口" if idle else "；".join(reasons)}


def tick(state, *, now: datetime | None = None, capacity: dict | None = None,
         deferrable_limit: int = 10, actor: str = "scheduler") -> dict:
    """执行一次 tick：过期清扫 + 空闲窗口可延迟任务执行（幂等）。"""
    now_utc = now or datetime.now(timezone.utc)
    expired = approval.expire_overdue(state, now_utc)
    window = idle_window(state, now=now_utc, capacity=capacity)
    ran = {"claimed": 0, "done": 0, "failed": 0, "skipped": 0, "items": []}
    if window["idle"]:
        ran = tasks.run_deferrable(state, limit=deferrable_limit, actor=actor)
    result = {
        "ts": now_utc.astimezone(_BJ).isoformat(timespec="seconds"),
        "expired_approvals": expired, "idle": window["idle"],
        "window": {k: v for k, v in window.items() if k != "capacity"},
        "capacity": window["capacity"], "deferrable": ran,
    }
    with write_txn(state_conn(state)) as c:
        c.execute(
            "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
            " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
            (_now_iso(), actor, "scheduler.tick", "task_schedule", "", "ok",
             f"过期审批 {expired}，空闲={window['idle']}，"
             f"可延迟执行 {ran['done']}/{ran['claimed']}", ""),
        )
    return result


def status(state, *, now: datetime | None = None,
           capacity: dict | None = None) -> dict:
    """调度现状快照（§7.2 闸门状态 / §6.1 任务态数据源）。"""
    c = state_conn(state)
    pend = c.execute(
        "SELECT COUNT(*) AS n FROM task_schedule WHERE status='pending'").fetchone()["n"]
    pend_def = c.execute(
        "SELECT COUNT(*) AS n FROM task_schedule WHERE status='pending'"
        " AND is_deferrable=1").fetchone()["n"]
    pend_appr = c.execute(
        "SELECT COUNT(*) AS n FROM approval_requests WHERE status='pending'").fetchone()["n"]
    window = idle_window(state, now=now, capacity=capacity)
    return {
        "pending_tasks": pend, "pending_deferrable": pend_def,
        "pending_approvals": pend_appr, "running_tasks": window["running"],
        "idle": window["idle"], "idle_reason": window["reason"],
        "capacity": window["capacity"],
    }

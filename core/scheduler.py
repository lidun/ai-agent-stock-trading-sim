"""调度器 tick 最小实现（spec-04 §2.2/§2.5）。

本切片实现 §2.2 清单中不依赖引擎回调的确定性项：
1. 可延迟任务组窗口判定（§2.5）：非交易时段 ∧ 无 running 任务 ∧ 本地资源余量 ≥30% 余量线；
2. 审批单过期扫描（§4.4）；
3. interrupt 超时扫描（§3.3）：挂起超阈值 → 保守拒绝恢复 + 联动关单（§4.4）；
4. 空闲窗口满足时按优先级执行到期可延迟任务。

引擎结算/跟踪回调（§2.2 第 2-4 项）仍由各自模块驱动，待补。
tick 由外部（常驻循环/手动路由）调用，保持幂等：可重复调用不产生重复副作用。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from core import approval, resource_gate, tasks
from core.db import state_conn, write_txn

log = logging.getLogger(__name__)

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
    recovered = tasks.recover_crashed(state, now=now_utc, actor=actor)
    expired = approval.expire_overdue(state, now_utc)
    timed_out = tasks.scan_interrupt_timeouts(state, now=now_utc, actor=actor)
    # §2.4 时效分级：过期决策任务 skipped / 缺勤日报；盘中无单账户补救启动
    bj = now_utc.astimezone(_BJ)
    expired_decisions: dict = {"skipped": [], "absent_reports": []}
    remedy: list[dict] = []
    try:
        from core import reconcile  # noqa: PLC0415
        expired_decisions = reconcile.expire_decision_tasks(
            state, today=bj.date().isoformat(), now_bj=bj.replace(tzinfo=None),
            actor=actor)
        remedy = reconcile.remedy_startup(
            state, now_bj=now_utc, trading=_is_trading_hours(now_utc), actor=actor)
        if remedy:
            tasks.run_pending(state, task_type="补救启动", actor=actor)
    except Exception:  # noqa: BLE001 - 时效判定失败不阻断 tick
        log.exception("时效任务现实时间判定失败（下个 tick 重试）")
    window = idle_window(state, now=now_utc, capacity=capacity)
    ran = {"claimed": 0, "done": 0, "failed": 0, "skipped": 0, "items": []}
    summary_queued: list[dict] = []
    card_queued: list[dict] = []
    if window["idle"]:
        try:  # spec-02 §4.1：空闲窗口扫描分层摘要缺口并入可延迟队列
            from core import summaries  # noqa: PLC0415
            summary_queued = summaries.enqueue_pending(
                state, today=bj.date(), now_bj=bj.replace(tzinfo=None), actor=actor)
        except Exception:  # noqa: BLE001 - 摘要扫描失败不阻断 tick
            log.exception("分层摘要缺口扫描失败（下个 tick 重试）")
        try:  # spec-02 §3.2：无卡片/flagged 条目补卡入队（高优先级）
            from core import memory_cards  # noqa: PLC0415
            card_queued = memory_cards.enqueue_pending_cards(state, actor=actor)
        except Exception:  # noqa: BLE001 - 补卡扫描失败不阻断 tick
            log.exception("调用卡片缺口扫描失败（下个 tick 重试）")
        ran = tasks.run_deferrable(state, limit=deferrable_limit, actor=actor)
    mgr = None
    try:  # §2.2 第 7 项：按 30 分钟桶入队健康检查（降级期间 light 仍执行以探测恢复）
        from core import manager  # noqa: PLC0415
        manager.ensure_health_task(state, now=now_utc, actor=actor)
        mgr = manager.status(state)
    except Exception:  # noqa: BLE001
        log.exception("管理 Agent 健康判定失败（下个 tick 重试）")
    result = {
        "ts": now_utc.astimezone(_BJ).isoformat(timespec="seconds"),
        "expired_approvals": expired, "interrupt_timed_out": len(timed_out),
        "interrupts": timed_out, "recovered_tasks": len(recovered),
        "recovered": recovered,
        "expired_decisions": expired_decisions["skipped"],
        "absent_reports": expired_decisions["absent_reports"],
        "remedy_startups": remedy, "idle": window["idle"],
        "summary_queued": summary_queued, "card_queued": card_queued,
        "window": {k: v for k, v in window.items() if k != "capacity"},
        "capacity": window["capacity"], "deferrable": ran,
        "manager_mode": (mgr or {}).get("mode", ""),
    }
    with write_txn(state_conn(state)) as c:
        c.execute(
            "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
            " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
            (_now_iso(), actor, "scheduler.tick", "task_schedule", "", "ok",
             f"崩溃恢复 {len(recovered)}，过期审批 {expired}，"
             f"interrupt 超时 {len(timed_out)}，"
             f"过期决策 {len(expired_decisions['skipped'])}，"
             f"补救启动 {len(remedy)}，摘要入队 {len(summary_queued)}，"
             f"补卡入队 {len(card_queued)}，"
             f"空闲={window['idle']}，可延迟执行 {ran['done']}/{ran['claimed']}", ""),
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
    pend_intr = c.execute(
        "SELECT COUNT(*) AS n FROM task_schedule WHERE status='running'"
        " AND interrupt_entered_ts!=''").fetchone()["n"]
    window = idle_window(state, now=now, capacity=capacity)
    mgr_mode = ""
    try:
        from core import manager  # noqa: PLC0415
        mgr_mode = manager.status(state)["mode"]
    except Exception:  # noqa: BLE001
        pass
    return {
        "pending_tasks": pend, "pending_deferrable": pend_def,
        "pending_approvals": pend_appr, "pending_interrupts": pend_intr,
        "running_tasks": window["running"],
        "idle": window["idle"], "idle_reason": window["reason"],
        "capacity": window["capacity"], "manager_mode": mgr_mode,
    }


class SchedulerEngine:
    """常驻 tick 驱动（spec-04 §2.2）：core 进程内空闲巡检 + 过期清扫唯一触发点。

    与 EodSettleTrigger 同构：run_forever 异步循环，操作全幂等；每个 tick 通过
    asyncio.to_thread 执行（sqlite/LLM 调用为阻塞操作，避免卡事件循环）。
    测试默认不启用（CORE_SCHEDULER_AUTO_TICK），run_once 供同步单步验证。
    """

    def __init__(self, state, *, deferrable_limit: int = 10, actor: str = "scheduler"):
        self.state = state
        self.deferrable_limit = deferrable_limit
        self.actor = actor
        self.ticks = 0
        self.last_result: dict | None = None

    def run_once(self, *, now: datetime | None = None,
                 capacity: dict | None = None) -> dict:
        r = tick(self.state, now=now, capacity=capacity,
                 deferrable_limit=self.deferrable_limit, actor=self.actor)
        self.ticks += 1
        self.last_result = r
        return r

    async def run_forever(self, tick_s: int) -> None:
        while True:
            try:
                r = await asyncio.to_thread(self.run_once)
                ran = r["deferrable"]
                if r["expired_approvals"] or ran["claimed"]:
                    log.info("调度 tick：过期审批 %s，空闲=%s，可延迟执行 %s/%s"
                             "（完成 %s / 失败 %s / 跳过 %s）",
                             r["expired_approvals"], r["idle"], ran["done"],
                             ran["claimed"], ran["done"], ran["failed"], ran["skipped"])
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 单次失败不终止常驻循环
                log.exception("调度 tick 异常（下个 tick 重试）")
            await asyncio.sleep(tick_s)

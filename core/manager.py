"""管理 Agent 安全自治模式（spec-04 §9.1，#39）。

管理 Agent 非常驻进程，"心跳"=周期健康检查任务（health_check）。降级判定：
健康检查连续 N 次 failed（默认 3）∨ 审批/interrupt 恢复调用连续失败 N 次 → 进入
安全自治；健康检查连续成功 M 次（默认 2）→ 退出降级。降级/恢复事件落库、审计并
推送管理 Agent 用户会话。

降级行为（§9.1）：无新审批/新能力下发；既定大纲任务照常；确定性职责即时处理，
LLM 职责延迟批处理——由 `is_degraded` 供任务执行层（run_deferrable）跳过 llm-heavy
可延迟任务，待恢复后按可延迟组补跑。全局状态全落库，无进程内存依赖。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from core.db import state_conn, write_txn

log = logging.getLogger(__name__)

MANAGER_ID = "agent-manager"
ROW_ID = "manager"

DEGRADE_AFTER_FAILURES = 3    # 连续失败 N 次 → 降级
RECOVER_AFTER_SUCCESSES = 2   # 连续成功 M 次 → 恢复
MODE_ACTIVE = "active"
MODE_AUTONOMOUS = "autonomous"
MODE_LABEL = {MODE_ACTIVE: "正常", MODE_AUTONOMOUS: "安全自治"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load(state) -> dict:
    c = state_conn(state)
    c.execute(
        "INSERT OR IGNORE INTO manager_state (id, updated_ts) VALUES (?, ?)",
        (ROW_ID, _now_iso()),
    )
    r = c.execute("SELECT * FROM manager_state WHERE id=?", (ROW_ID,)).fetchone()
    return dict(r)


def _audit(state, *, action: str, result: str, detail: str, actor: str) -> None:
    with write_txn(state_conn(state)) as c:
        c.execute(
            "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
            " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
            (_now_iso(), actor, action, "manager_state", ROW_ID, result,
             detail[:400], ""),
        )


def _notify(state, *, recovered: bool, reason: str, mode: str) -> None:
    """降级/恢复事件推送管理 Agent 用户会话（幂等：payload_ref 判重）。"""
    from core import chatstore  # noqa: PLC0415
    kind = "recover" if recovered else "degrade"
    body = "\n".join([
        f"## 管理 Agent {'恢复' if recovered else '降级'} · {'正常' if recovered else '安全自治'}模式",
        f"- 当前模式：{MODE_LABEL.get(mode, mode)}",
        f"- 触发原因：{reason or '—'}",
        "- 处置：" + ("积压 LLM 职责按可延迟组补跑" if recovered
                     else "无新审批/新能力下发；确定性职责照常，LLM 职责延迟批处理"),
    ])
    payload = json.dumps({"kind": kind, "mode": mode, "reason": reason[:200]},
                         ensure_ascii=False, sort_keys=True)
    try:
        conv = chatstore.ensure_user_chat(state, MANAGER_ID)
    except LookupError:
        return
    if state_conn(state).execute(
        "SELECT 1 FROM messages WHERE conv_id=? AND payload_ref=? LIMIT 1",
        (conv["id"], payload),
    ).fetchone():
        return
    try:
        chatstore.insert_message(
            state, conv_id=conv["id"], agent_id=MANAGER_ID, direction="agent",
            msg_type="manager_mode", body=body, payload_ref=payload,
            status="delivered", delivered_via="web")
    except Exception:  # noqa: BLE001
        log.exception("管理 Agent 模式变更推送失败")


def _update(state, fields: dict, *, action: str, result: str, detail: str,
            actor: str, notify: tuple[bool, str, str] | None = None) -> dict:
    sets = ", ".join(f"{k}=?" for k in fields)
    with write_txn(state_conn(state)) as c:
        c.execute(
            f"UPDATE manager_state SET {sets}, updated_ts=? WHERE id=?",
            (*fields.values(), _now_iso(), ROW_ID),
        )
    _audit(state, action=action, result=result, detail=detail, actor=actor)
    if notify is not None:
        _notify(state, recovered=notify[0], reason=notify[1], mode=notify[2])
    return status(state)


def status(state) -> dict:
    """管理 Agent 全局状态快照（含模式标签，供 UI/调度判定）。"""
    r = _load(state)
    return {
        "mode": r["mode"], "mode_label": MODE_LABEL.get(r["mode"], r["mode"]),
        "degraded": r["mode"] == MODE_AUTONOMOUS,
        "consecutive_health_failures": r["consecutive_health_failures"],
        "consecutive_health_successes": r["consecutive_health_successes"],
        "consecutive_resume_failures": r["consecutive_resume_failures"],
        "last_health_ts": r["last_health_ts"],
        "last_health_ok": bool(r["last_health_ok"]),
        "last_reason": r["last_reason"],
        "degraded_since": r["degraded_since"],
        "recovered_ts": r["recovered_ts"],
        "updated_ts": r["updated_ts"],
        "degrade_after": DEGRADE_AFTER_FAILURES,
        "recover_after": RECOVER_AFTER_SUCCESSES,
    }


def is_degraded(state) -> bool:
    return _load(state)["mode"] == MODE_AUTONOMOUS


def record_health(state, ok: bool, *, detail: str = "",
                  actor: str = "scheduler") -> dict:
    """记录一次健康检查结果并按阈值切换模式（§9.1）。返回最新状态。"""
    r = _load(state)
    now = _now_iso()
    fields: dict = {
        "last_health_ts": now, "last_health_ok": 1 if ok else 0,
        "last_reason": detail[:400] or ("健康检查通过" if ok else "健康检查失败"),
    }
    mode = r["mode"]
    notify = None
    action, result = "manager.health", "ok" if ok else "failed"
    if ok:
        succ = r["consecutive_health_successes"] + 1
        fields.update({"consecutive_health_successes": succ,
                       "consecutive_health_failures": 0})
        if mode == MODE_AUTONOMOUS and succ >= RECOVER_AFTER_SUCCESSES:
            fields.update({"mode": MODE_ACTIVE, "consecutive_resume_failures": 0,
                           "recovered_ts": now})
            action, result = "manager.recover", "active"
            notify = (True, detail or "健康检查连续成功", MODE_ACTIVE)
    else:
        fails = r["consecutive_health_failures"] + 1
        fields.update({"consecutive_health_failures": fails,
                       "consecutive_health_successes": 0})
        if mode == MODE_ACTIVE and fails >= DEGRADE_AFTER_FAILURES:
            fields.update({"mode": MODE_AUTONOMOUS, "degraded_since": now})
            action, result = "manager.degrade", "autonomous"
            notify = (False, detail or f"健康检查连续失败 {fails} 次", MODE_AUTONOMOUS)
    return _update(state, fields, action=action, result=result,
                   detail=detail or f"health ok={ok}", actor=actor, notify=notify)


def record_resume_call(state, ok: bool, *, detail: str = "",
                       actor: str = "scheduler") -> dict:
    """记录一次审批/interrupt 恢复调用结果；连续失败 N 次 → 降级（§9.1）。"""
    r = _load(state)
    if ok:
        if r["consecutive_resume_failures"] == 0:
            return status(state)
        return _update(state, {"consecutive_resume_failures": 0},
                       action="manager.resume", result="ok",
                       detail=detail or "恢复调用成功", actor=actor)
    fails = r["consecutive_resume_failures"] + 1
    fields: dict = {"consecutive_resume_failures": fails}
    notify = None
    action, result = "manager.resume", "failed"
    if r["mode"] == MODE_ACTIVE and fails >= DEGRADE_AFTER_FAILURES:
        fields.update({"mode": MODE_AUTONOMOUS, "degraded_since": _now_iso()})
        action, result = "manager.degrade", "autonomous"
        notify = (False, detail or f"恢复调用连续失败 {fails} 次", MODE_AUTONOMOUS)
    return _update(state, fields, action=action, result=result,
                   detail=detail or "恢复调用失败", actor=actor, notify=notify)


def ensure_health_task(state, *, now: datetime | None = None,
                       interval_s: int = 1800, actor: str = "scheduler") -> dict | None:
    """按 interval_s 时间桶入队一次「健康检查」可延迟任务（§9.1，默认 30 分钟）。

    幂等：同一时间桶内重复 tick 只入队一次（dedup_key=桶号）。返回入队结果。
    """
    from core import tasks  # noqa: PLC0415
    now_utc = now or datetime.now(timezone.utc)
    bucket = int(now_utc.timestamp() // max(60, int(interval_s)))
    try:
        return tasks.enqueue(
            state, task_type="健康检查", agent_id=MANAGER_ID,
            dedup_key=f"health-{bucket}", resource_class="light",
            is_deferrable=True, priority=7, actor=actor)
    except ValueError:
        log.exception("健康检查任务入队失败")
        return None


def health_check(state, *, actor: str = "scheduler",
                 timeout_s: float = 10.0) -> dict:
    """周期健康检查任务处理器（§9.1：轻量 LLM ping + 进程内状态自检）。

    进程内自检：DB 可读。LLM ping：适配层连通性测试。任一失败记一次失败；
    异常不外抛（返回结果供任务处理器判定 finish）。
    """
    from core import llm  # noqa: PLC0415
    checks: dict = {}
    try:
        state_conn(state).execute("SELECT 1").fetchone()
        checks["db"] = True
    except Exception:  # noqa: BLE001
        log.exception("健康检查 DB 自检失败")
        checks["db"] = False
    ping = llm.provider_test(state, timeout_s=timeout_s, actor=actor)
    checks["llm"] = bool(ping.get("ok"))
    if not ping.get("configured"):
        checks["llm_error"] = ping.get("error", "LLM 未配置")
    ok = checks["db"] and checks["llm"]
    detail = "db=ok" if checks["db"] else "db=fail"
    if checks["llm"]:
        detail += f"；llm=ok({ping.get('latency_ms')}ms)"
    else:
        detail += f"；llm=fail({checks.get('llm_error', '')})"
    st = record_health(state, ok, detail=detail, actor=actor)
    return {"ok": ok, "checks": checks, "mode": st["mode"], "detail": detail}

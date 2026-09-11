"""补跑与时效分级（spec-04 §2.4 / #28）。

有时效决策任务在启动/盘中对照现实时间判定：
- 过期选股/挂单/复盘/总汇报 → `skipped`（不补，时效已过）；
- 过期日报 → 降级生成**缺勤日报**（spec-04 §5.3，复用 reporting.fill_absent_report）；
- 盘中首次启动且当日尚未设条件单、账户仍有持仓 → 生成「补救启动」任务
  （快速为持仓补保护性条件单与当日简况，防持仓裸奔）。

无时效引擎任务（结算/跟踪/对账）的快进回放归 settle_scheduler.catchup_missed（§2.6 第 4 步）。
"""
from __future__ import annotations

import logging

from core import tasks

log = logging.getLogger(__name__)

# 有时效决策任务类型（过期不补，日报除外→缺勤日报）
TIME_SENSITIVE_TYPES = ("选股", "挂单", "复盘", "总汇报", "日报")


def _audit(state, *, action: str, result: str, object_id: str, actor: str,
           detail: str) -> None:
    from core import tasks as _t  # noqa: PLC0415
    _t._audit(state, action=action, result=result, object_id=object_id,
              actor=actor, detail=detail)


def expire_decision_tasks(state, *, today: str, now_bj, grace_minutes: int = 0,
                          actor: str = "startup") -> dict:
    """时效决策任务现实时间判定（§2.4/§2.6 第 5 步）。

    过期口径：trade_date < today；或同日但 task_slot(HH:MM) + grace 已过 now_bj。
    日报过期 → 生成缺勤日报后标 skipped；其余直接 skipped。幂等（已非 pending 不动）。
    """
    from core import reporting  # noqa: PLC0415
    pending = tasks.list_tasks(state, status="pending", limit=500)
    skipped: list[dict] = []
    absent: list[dict] = []
    for t in pending:
        if t["task_type"] not in TIME_SENSITIVE_TYPES:
            continue
        td = t["trade_date"]
        overdue = bool(td) and td < today
        if not overdue and td == today and t["task_slot"]:
            slot = t["task_slot"]
            if len(slot) == 5 and slot[2] == ":" and slot[:2].isdigit():
                hh, mm = int(slot[:2]), int(slot[3:])
                now_min = now_bj.hour * 60 + now_bj.minute
                if now_min > hh * 60 + mm + max(0, int(grace_minutes)):
                    overdue = True
        if not overdue:
            continue
        reason = f"时效已过（trade_date={td or '-'}，判定于 {today}）不补跑"
        if t["task_type"] == "日报" and td:
            try:
                r = reporting.fill_absent_report(state, t["agent_id"], td, reason)
                if not r.get("skipped"):
                    absent.append({"agent_id": t["agent_id"], "trade_date": td})
            except Exception:  # noqa: BLE001 - 缺勤日报失败不阻断清扫
                log.exception("缺勤日报补生成失败 %s %s", t["agent_id"], td)
        tasks.finish(state, t["id"], ok=False, status="skipped",
                     detail=reason, actor=actor)
        skipped.append({"id": t["id"], "task_type": t["task_type"],
                        "trade_date": td})
    return {"skipped": skipped, "absent_reports": absent}


def _held_accounts(state) -> list[str]:
    from core.db import state_conn  # noqa: PLC0415
    c = state_conn(state)
    return [r["account_id"] for r in c.execute(
        "SELECT h.account_id FROM holdings h"
        " JOIN accounts a ON a.id = h.account_id"
        " WHERE h.quantity > 0 AND a.status IN ('normal', 'paused_buy')"
        " GROUP BY h.account_id").fetchall()]


def _has_active_orders(state, account_id: str) -> bool:
    from core.db import state_conn  # noqa: PLC0415
    row = state_conn(state).execute(
        "SELECT 1 FROM condition_orders WHERE account_id=? AND status='active'"
        " LIMIT 1", (account_id,)).fetchone()
    return row is not None


def remedy_startup(state, *, now_bj, trading: bool | None = None,
                   actor: str = "scheduler") -> list[dict]:
    """盘中补救启动（§2.4）：有持仓、当日无 active 条件单的账户 → 入队「补救启动」。

    trading=None 时按北京时间交易时段判定。幂等：dedup_key=trade_date。
    非交易时段/无持仓账户不产生任务，返还空列表。
    """
    from core import scheduler  # noqa: PLC0415
    if trading is None:
        trading = scheduler._is_trading_hours(now_bj)
    if not trading:
        return []
    today = now_bj.date().isoformat() if hasattr(now_bj, "date") else str(now_bj)
    out: list[dict] = []
    for account_id in _held_accounts(state):
        if _has_active_orders(state, account_id):
            continue
        r = tasks.enqueue(
            state, task_type="补救启动", agent_id=account_id, trade_date=today,
            dedup_key=today, resource_class="light", is_deferrable=False,
            priority=2, payload={"account_id": account_id, "reason": "未设条件单"},
            actor=actor)
        if r.get("created"):
            out.append({"account_id": account_id, "task_id": r["id"]})
    if out:
        _audit(state, action="task.remedy_startup", result="pending",
               object_id=today, actor=actor,
               detail=f"盘中补救启动 {len(out)} 个账户："
                      + "，".join(o["account_id"] for o in out))
    return out


def _run_remedy_startup(state, task: dict) -> str:
    """「补救启动」处理器：产出当日简况并推 Agent 会话（保护性条件单由策略侧补）。"""
    from core import chatstore  # noqa: PLC0415
    from core.db import state_conn  # noqa: PLC0415
    account_id = (task.get("payload") or {}).get("account_id") or task["agent_id"]
    rows = state_conn(state).execute(
        "SELECT symbol, quantity FROM holdings"
        " WHERE account_id=? AND quantity>0 ORDER BY symbol", (account_id,)).fetchall()
    holdings = "，".join(f"{r['symbol']}×{r['quantity']:g}" for r in rows) or "无"
    body = "\n".join([
        "## 补救启动 · 当日简况（spec-04 §2.4）",
        f"- 账户：{account_id}",
        f"- 持仓：{holdings}",
        "- 状态：盘中首次启动且当日未设条件单，请尽快补保护性条件单，防持仓裸奔",
    ])
    try:
        conv = chatstore.ensure_user_chat(state, account_id)
        chatstore.insert_message(
            state, conv_id=conv["id"], agent_id=account_id, direction="agent",
            msg_type="remedy_startup", body=body,
            payload_ref=f"remedy:{account_id}:{task.get('trade_date', '')}",
            status="delivered", delivered_via="web")
    except Exception:  # noqa: BLE001 - 推送失败不影响任务收尾
        log.exception("补救启动简况推送失败 %s", account_id)
    return f"账户 {account_id} 当日简况已生成：持仓 {holdings}"

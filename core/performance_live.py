"""性能监控现状快照（spec-04 §9 / spec-06 §6.1 回执链，P3 占位落地）。

本片不虚构指标曲线：近端时序未持久化 → 面板只给出可自证的"现状快照"：
- tasks：messages 回执链实时态（queued/processing/delivered/...），活跃滞留任务含 age；
- msg_1h：近 1 小时已送达量（含失败），作为吞吐代理且标注口径；
- approvals：待决审批数与最近过期倒计时（expires_ts 现成约束）；
- last_settle：最近一次结算运行的新鲜度（settlement_log.created_at）。
一切数值由 SQL + 时间差推导，缺数据返回零/None，不做插值。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from core.db import state_conn

_LIVE_STATES = ("queued", "processing")


def _ts_to_epoch(ts: str) -> float:
    if not ts:
        return 0.0
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.timestamp()
    except (TypeError, ValueError):
        return 0.0


def _epoch_iso(e: float) -> str:
    return datetime.fromtimestamp(e, timezone.utc).isoformat(timespec="seconds")


def snapshot(state, *, started_ts: float | None = None) -> dict:
    now = time.time()
    c = state_conn(state)

    # 回执链任务实时态（messages.status，非终态视为活跃任务）
    status_rows = c.execute(
        "SELECT status, COUNT(*) n FROM messages GROUP BY status").fetchall()
    status_dist = {str(r["status"]): int(r["n"]) for r in status_rows}

    live_rows = c.execute(
        "SELECT m.id, m.conv_id, m.agent_id, m.status, m.body, m.ts, ag.role"
        " FROM messages m JOIN agents ag ON ag.id = m.agent_id"
        " WHERE m.status IN ('queued','processing')"
        " ORDER BY m.ts ASC LIMIT 50").fetchall()
    stale_active = []
    for r in live_rows:
        age = max(0.0, now - _ts_to_epoch(r["ts"]))
        stale_active.append({
            "id": r["id"], "agent_id": r["agent_id"], "role": r["role"],
            "status": r["status"], "age_s": int(age),
            "body_preview": (r["body"] or "")[:40],
        })

    # 近 1 小时流转（吞吐代理，口径显式标注）
    since_ts = _epoch_iso(now - 3600)
    one_hour = c.execute(
        "SELECT COUNT(*) n FROM messages WHERE status='delivered' AND ts >= ?",
        (since_ts,)).fetchone()["n"]
    user_req = c.execute(
        "SELECT COUNT(*) n FROM messages WHERE direction='user' AND ts >= ?",
        (since_ts,)).fetchone()["n"]
    failed_1h = c.execute(
        "SELECT COUNT(*) n FROM messages WHERE status='failed' AND ts >= ?",
        (since_ts,)).fetchone()["n"]

    # 待决审批（24h expires_ts 现成）——过期由审批域自动置 expired，此处给倒计时
    aprows = c.execute(
        "SELECT status, COUNT(*) n FROM approval_requests GROUP BY status").fetchall()
    ap_dist = {str(r["status"]): int(r["n"]) for r in aprows}
    next_exp = c.execute(
        "SELECT expires_ts FROM approval_requests WHERE status='pending'"
        " ORDER BY expires_ts ASC LIMIT 1").fetchone()

    # 最近结算新鲜度
    last_settle = c.execute(
        "SELECT trade_date, created_at FROM settlement_log"
        " ORDER BY created_at DESC LIMIT 1").fetchone()

    uptime_s = 0
    if started_ts:
        uptime_s = int(max(0.0, now - started_ts))

    return {
        "snapshot_ts": _epoch_iso(now),
        "uptime_s": uptime_s,
        "tasks": {
            "total": sum(status_dist.values()),
            "status": status_dist,
            "live": sum(status_dist.get(s, 0) for s in _LIVE_STATES),
            "stale_active": stale_active[:8],
        },
        "msg_1h": {
            "delivered": int(one_hour),
            "user_requests": int(user_req),
            "failed": int(failed_1h),
            "scope": "messages 现状行近 1h（ts 精度秒，非独立时序埋点）",
        },
        "approvals": {
            "status": ap_dist,
            "pending": int(ap_dist.get("pending", 0)),
            "next_expires_in_s": (
                int(max(0.0, _ts_to_epoch(next_exp["expires_ts"]) - now))
                if next_exp and next_exp["expires_ts"] else None),
        },
        "last_settle": (
            {
                "trade_date": last_settle["trade_date"],
                "fresh_s": int(max(0.0, now - _ts_to_epoch(last_settle["created_at"]))),
            } if last_settle else None
        ),
    }


def pre_dump(o: object) -> str:
    """调试序列化（非接口使用）。"""
    return json.dumps(o, ensure_ascii=False, default=str)

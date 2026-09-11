"""数据源健康度与故障切换（spec-03 §9 / §2.2 分层规则）。

- 每源指标：成功率、延迟 P50/P95、当日降级票数、连续失败数 → 健康度分 A/B/C；
- C 级持续 N 分钟 → 触发降权/切换：**拉取类当日切换、采集类次日生效**（§2.2）；
- 探活成功 → 冷却期后连续 M 次回权（冷却期可配）；
- 健康度供管理 Agent 审批"盘中档申请"与 spec-04 资源闸门消费（`snapshot`）。

按 (source, trade_date) 单日聚合；结果落 `source_health`，切换/回权留审计。
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone

from core.db import state_conn, write_txn

log = logging.getLogger(__name__)

_BJT = timezone(timedelta(hours=8))
GRADES = ("A", "B", "C")


def _now_bj(now: datetime | None = None) -> datetime:
    now = now or datetime.now(_BJT)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_BJT)
    return now.astimezone(_BJT)


def _today(now: datetime | None = None) -> str:
    return _now_bj(now).date().isoformat()


def _settings(state):
    return getattr(state, "settings", None)


def _cfg(state, name: str, default):
    return getattr(_settings(state), name, default)


def _pctl(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    vs = sorted(values)
    idx = max(0, math.ceil(pct / 100.0 * len(vs)) - 1)
    return round(vs[min(idx, len(vs) - 1)], 2)


def _grade(state, success: int, calls: int, p95: float) -> str:
    if calls <= 0:
        return ""
    rate = success / calls * 100.0
    a_pct = _cfg(state, "source_health_a_success_pct", 98)
    b_pct = _cfg(state, "source_health_b_success_pct", 90)
    a_p95 = _cfg(state, "source_health_a_p95_ms", 3000)
    if rate >= a_pct and p95 <= a_p95:
        return "A"
    if rate >= b_pct:
        return "B"
    return "C"


def _audit(state, *, action: str, result: str, object_id: str, actor: str,
           detail: str) -> None:
    with write_txn(state_conn(state)) as c:
        c.execute(
            "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
            " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
            (_now_bj().isoformat(timespec="seconds"), actor, action,
             "source_health", object_id, result, detail[:400], ""))


def _row(state, source: str, trade_date: str):
    return state_conn(state).execute(
        "SELECT * FROM source_health WHERE source=? AND trade_date=?",
        (source, trade_date)).fetchone()


def record_call(state, *, source: str, ok: bool, latency_ms: float = 0.0,
                source_family: str = "", kind: str = "pull",
                degraded: bool = False, trade_date: str = "",
                now: datetime | None = None) -> dict:
    """记录一次源调用（成功/失败+延迟+是否降级），刷新当日健康度分。"""
    td = trade_date or _today(now)
    now_bj = _now_bj(now)
    r = _row(state, source, td)
    if r is None:
        latency: list[float] = []
        agg = {"calls": 0, "success": 0, "fail": 0, "degraded": 0,
               "consec": 0, "c_since": "", "cooled": "", "recover": 0,
               "switched": 0, "family": source_family or "", "kind": kind}
    else:
        latency = list(json.loads(r["latency_samples"] or "[]"))
        agg = {"calls": r["calls"], "success": r["success"], "fail": r["fail"],
               "degraded": r["degraded_count"],
               "consec": r["consecutive_failures"], "c_since": r["c_since_ts"],
               "cooled": r["cooled_until"], "recover": r["recover_successes"],
               "switched": r["switched"],
               "family": source_family or r["source_family"],
               "kind": kind or r["kind"]}
    agg["calls"] += 1
    if ok:
        agg["success"] += 1
        agg["consec"] = 0
    else:
        agg["fail"] += 1
        agg["consec"] += 1
    if degraded:
        agg["degraded"] += 1
    if latency_ms:
        latency.append(float(latency_ms))
    cap = int(_cfg(state, "source_health_latency_sample_max", 200))
    latency = latency[-cap:]
    p50, p95 = _pctl(latency, 50), _pctl(latency, 95)
    score = _grade(state, agg["success"], agg["calls"], p95)
    # C 级起算时间戳：进入 C 记录，离开 C 清除
    c_since = agg["c_since"]
    if score == "C" and not c_since:
        c_since = now_bj.isoformat(timespec="seconds")
    elif score != "C":
        c_since = ""
    with write_txn(state_conn(state)) as c:
        c.execute(
            "INSERT INTO source_health (source, trade_date, source_family, kind,"
            " calls, success, fail, degraded_count, consecutive_failures,"
            " latency_samples, score, c_since_ts, cooled_until,"
            " recover_successes, switched, note, updated_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(source, trade_date) DO UPDATE SET"
            " source_family=excluded.source_family, kind=excluded.kind,"
            " calls=excluded.calls, success=excluded.success, fail=excluded.fail,"
            " degraded_count=excluded.degraded_count,"
            " consecutive_failures=excluded.consecutive_failures,"
            " latency_samples=excluded.latency_samples, score=excluded.score,"
            " c_since_ts=excluded.c_since_ts, updated_ts=excluded.updated_ts",
            (source, td, agg["family"], agg["kind"], agg["calls"], agg["success"],
             agg["fail"], agg["degraded"], agg["consec"], json.dumps(latency),
             score, c_since, agg["cooled"], agg["recover"], agg["switched"],
             r["note"] if r is not None else "", now_bj.isoformat(timespec="seconds")))
    return {"source": source, "trade_date": td, "calls": agg["calls"],
            "success": agg["success"], "fail": agg["fail"],
            "success_rate": round(agg["success"] / agg["calls"], 4),
            "degraded_count": agg["degraded"], "consecutive_failures": agg["consec"],
            "p50_ms": p50, "p95_ms": p95, "score": score}


def get_health(state, source: str, trade_date: str = "") -> dict | None:
    r = _row(state, source, trade_date or _today())
    if r is None:
        return None
    lat = list(json.loads(r["latency_samples"] or "[]"))
    return {**dict(r),
            "latency_samples": len(lat),
            "p50_ms": _pctl(lat, 50), "p95_ms": _pctl(lat, 95),
            "success_rate": round(r["success"] / r["calls"], 4) if r["calls"] else 0.0}


def snapshot(state, trade_date: str = "") -> list[dict]:
    """当日各源健康度快照（管理 Agent 审批与资源闸门输入）。"""
    td = trade_date or _today()
    rows = state_conn(state).execute(
        "SELECT * FROM source_health WHERE trade_date=? ORDER BY source",
        (td,)).fetchall()
    out = []
    for r in rows:
        lat = list(json.loads(r["latency_samples"] or "[]"))
        out.append({
            "source": r["source"], "source_family": r["source_family"],
            "kind": r["kind"], "calls": r["calls"], "success": r["success"],
            "fail": r["fail"],
            "success_rate": round(r["success"] / r["calls"], 4) if r["calls"] else 0.0,
            "degraded_count": r["degraded_count"],
            "consecutive_failures": r["consecutive_failures"],
            "p50_ms": _pctl(lat, 50), "p95_ms": _pctl(lat, 95),
            "score": r["score"], "switched": bool(r["switched"]),
            "cooled_until": r["cooled_until"],
            "c_since_ts": r["c_since_ts"],
        })
    return out


def evaluate_switch(state, *, source: str, kind: str = "", trade_date: str = "",
                    now: datetime | None = None) -> dict:
    """C 级持续 N 分钟 → 降权/切换（拉取类当日、采集类次日生效，§2.2）。"""
    td = trade_date or _today(now)
    now_bj = _now_bj(now)
    r = _row(state, source, td)
    if r is None or r["score"] != "C":
        return {"source": source, "action": "none", "score": r["score"] if r else ""}
    if r["switched"]:
        return {"source": source, "action": "already_switched",
                "effective": "today" if (kind or r["kind"]) == "pull" else "next_day",
                "score": "C"}
    try:
        since = datetime.fromisoformat(r["c_since_ts"])
        if since.tzinfo is None:
            since = since.replace(tzinfo=_BJT)
    except ValueError:
        since = now_bj
    elapsed = (now_bj - since).total_seconds() / 60.0
    threshold = int(_cfg(state, "source_health_c_minutes", 10))
    if elapsed < threshold:
        return {"source": source, "action": "retreat", "score": "C",
                "elapsed_min": round(elapsed, 1), "threshold_min": threshold}
    effective = "today" if (kind or r["kind"]) == "pull" else "next_day"
    cooldown = int(_cfg(state, "source_health_recover_cooldown_min", 30))
    cooled_until = (now_bj + timedelta(minutes=cooldown)).isoformat(timespec="seconds")
    with write_txn(state_conn(state)) as c:
        c.execute(
            "UPDATE source_health SET switched=1, cooled_until=?, updated_ts=?,"
            " note=? WHERE source=? AND trade_date=?",
            (cooled_until, now_bj.isoformat(timespec="seconds"),
             f"C 级持续 {round(elapsed, 1)} 分钟，{effective} 生效切换", source, td))
    action = "switch" if effective == "today" else "switch_next_day"
    _audit(state, action="source.switch", result="ok",
           object_id=f"{source}:{td}", actor="scheduler",
           detail=f"score=C elapsed={round(elapsed,1)}min effective={effective}")
    return {"source": source, "action": action, "effective": effective,
            "score": "C", "elapsed_min": round(elapsed, 1),
            "cooled_until": cooled_until}


def probe_recover(state, *, source: str, ok: bool, trade_date: str = "",
                  now: datetime | None = None) -> dict:
    """恢复探活：冷却期后连续 M 次成功 → 回权（清除切换状态）。"""
    td = trade_date or _today(now)
    now_bj = _now_bj(now)
    r = _row(state, source, td)
    if r is None:
        return {"source": source, "recovered": False, "reason": "no_record"}
    recover = r["recover_successes"]
    needs = int(_cfg(state, "source_health_recover_successes", 2))
    waiting = False
    if r["cooled_until"]:
        try:
            until = datetime.fromisoformat(r["cooled_until"])
            if until.tzinfo is None:
                until = until.replace(tzinfo=_BJT)
            waiting = now_bj < until
        except ValueError:
            waiting = False
    if not ok:
        recover = 0
    elif not waiting:
        recover += 1
    recovered = bool(ok and not waiting and recover >= needs)
    with write_txn(state_conn(state)) as c:
        if recovered:
            c.execute(
                "UPDATE source_health SET recover_successes=0, switched=0,"
                " c_since_ts='', cooled_until='', updated_ts=?, note=? WHERE"
                " source=? AND trade_date=?",
                (now_bj.isoformat(timespec="seconds"),
                 f"探活 {recover} 次成功回权", source, td))
        else:
            c.execute(
                "UPDATE source_health SET recover_successes=?, updated_ts=?"
                " WHERE source=? AND trade_date=?",
                (recover, now_bj.isoformat(timespec="seconds"), source, td))
    if recovered:
        _audit(state, action="source.recover", result="ok",
               object_id=f"{source}:{td}", actor="scheduler",
               detail=f"连续 {recover} 次探活成功回权")
    return {"source": source, "recovered": recovered, "recover_successes": recover,
            "waiting_cooldown": waiting, "reason": "cooldown" if waiting else ""}

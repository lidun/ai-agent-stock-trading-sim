"""市场观察滚动删除与级联清理（spec-02 §4.3 / §12）。

前置条件（缺一不可）：该期分层摘要已生成 **且** 管理 Agent 抽检通过（`memory_inspections`
passing 凭证）。满足后一次事务内级联清理：删卡片 → 删原文 → 审计留痕；向量删除通过
可注入钩子（chroma 未接入时为 None，不造假删除）。原文删除后，摘要 record_ids 对应条目
降级为"历史索引"——溯源到删除审计记录。

**抽检**：本片以结构性抽检（抽样条目是否有卡片、是否 flagged）实现，可后续替换为
LLM 卡片-原文一致性比对；抽样比例取 config `memory_inspection_sample_ratio`（默认 0.1）。
"""
from __future__ import annotations

import logging
import math
import secrets
from datetime import date, datetime, timezone

from core import summaries
from core.db import state_conn, write_txn

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _settings(state):
    return getattr(state, "settings", None)


def _audit(state, *, action: str, result: str, object_id: str, actor: str,
           detail: str) -> None:
    with write_txn(state_conn(state)) as c:
        c.execute(
            "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
            " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
            (_now_iso(), actor, action, "memory_rollout", object_id, result,
             detail[:400], ""))


def _sample_ids(state, agent_id: str, period: str, period_key: str) -> list[str]:
    inputs = summaries._load_inputs(state, agent_id, period, period_key)
    if period == "month":
        ids: list[str] = []
        for w in inputs:
            ids.extend(w.get("record_ids") or [])
    else:
        ids = [str(i["id"]) for i in inputs]
    return sorted(set(ids))


def inspect_period(state, agent_id: str, period: str, period_key: str, *,
                   sample_ratio: float | None = None,
                   actor: str = "manager") -> dict:
    """管理 Agent 抽检：抽样条目须有卡片且未 flagged。落 `memory_inspections` 凭证。"""
    ids = _sample_ids(state, agent_id, period, period_key)
    ratio = (sample_ratio if sample_ratio is not None else
             getattr(_settings(state), "memory_inspection_sample_ratio", 0.1))
    k = max(1, math.ceil(len(ids) * float(ratio or 0))) if ids else 0
    sample = ids[:k]
    c = state_conn(state)
    missing = flagged = 0
    for mid in sample:
        if c.execute("SELECT 1 FROM memory_cards WHERE memory_id=?",
                     (mid,)).fetchone() is None:
            missing += 1
        q = c.execute("SELECT quality FROM memory_entries WHERE id=?",
                      (mid,)).fetchone()
        if q is not None and q["quality"] == "flagged":
            flagged += 1
    passed = bool(ids) and missing == 0 and flagged == 0
    iid = "mi" + secrets.token_hex(10)
    note = f"抽样 {len(sample)}/{len(ids)}，缺卡 {missing}，flagged {flagged}"
    with write_txn(c) as cw:
        cw.execute(
            "INSERT INTO memory_inspections (id, agent_id, period, period_key,"
            " checked, flagged, missing_cards, passed, note, created_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (iid, agent_id, period, period_key, len(sample), flagged, missing,
             1 if passed else 0, note, _now_iso()))
    _audit(state, action="memory.inspection", result="passed" if passed else "failed",
           object_id=f"{agent_id}:{period}:{period_key}", actor=actor, detail=note)
    return {"id": iid, "agent_id": agent_id, "period": period,
            "period_key": period_key, "total": len(ids), "checked": len(sample),
            "missing_cards": missing, "flagged": flagged, "passed": passed,
            "note": note}


def list_inspections(state, agent_id: str, *, limit: int = 100) -> list[dict]:
    rows = state_conn(state).execute(
        "SELECT * FROM memory_inspections WHERE agent_id=? ORDER BY created_ts DESC"
        " LIMIT ?", (agent_id, int(limit))).fetchall()
    return [dict(r) for r in rows]


def _has_pass(state, agent_id: str, period: str, period_key: str) -> bool:
    return state_conn(state).execute(
        "SELECT 1 FROM memory_inspections WHERE agent_id=? AND period=? AND"
        " period_key=? AND passed=1 LIMIT 1",
        (agent_id, period, period_key)).fetchone() is not None


def roll_market_notes(state, *, agent_id: str, before: str,
                      vector_delete=None, actor: str = "scheduler",
                      limit: int = 1000) -> dict:
    """滚动删除 `before`（BJ 日期，不含）之前的市场观察原文（级联卡片+审计）。

    仅处理"日摘要已生成 且 抽检通过"的条目；其余保持不动（前置不满足）。
    `vector_delete(state, memory_ids)` 为可注入向量删除钩子（未接入传 None）。
    """
    before_date = date.fromisoformat(before)
    c = state_conn(state)
    rows = c.execute(
        "SELECT id, ts FROM memory_entries WHERE agent_id=? AND mem_type='market_note'"
        " ORDER BY ts ASC", (agent_id,)).fetchall()
    eligible: list[tuple[str, str]] = []
    skipped = 0
    for r in rows:
        d = summaries._bj_date(r["ts"])
        if d is None or d >= before_date:
            continue
        key = d.isoformat()
        if summaries.get_summary(state, agent_id, "day", key) is None \
                or not _has_pass(state, agent_id, "day", key):
            skipped += 1
            continue
        eligible.append((r["id"], key))
    selected = eligible[:limit]
    ids = [x[0] for x in selected]
    periods = sorted({x[1] for x in selected})
    if ids:
        if vector_delete is not None:
            try:
                vector_delete(state, ids)
            except Exception:  # noqa: BLE001 - 向量删除失败不阻塞级联（留痕续查）
                log.exception("向量删除钩子失败（继续清理关系库并留痕）")
        marks = ",".join("?" for _ in ids)
        with write_txn(c) as cw:
            cw.execute(
                f"DELETE FROM memory_cards WHERE memory_id IN ({marks})", ids)
            cw.execute(
                f"DELETE FROM memory_entries WHERE id IN ({marks})", ids)
        _audit(state, action="memory.rollout", result="ok",
               object_id=f"{agent_id}:<{before}",
               actor=actor,
               detail=f"删除 {len(ids)} 条市场观察原文（卡片级联），"
                      f"period_keys={periods}，抽检凭证=memory.inspections.passed")
    else:
        _audit(state, action="memory.rollout", result="noop",
               object_id=f"{agent_id}:<{before}", actor=actor,
               detail=f"无满足前置条件的条目（跳过 {skipped}）")
    return {"agent_id": agent_id, "before": before, "deleted": len(ids),
            "skipped": skipped, "memory_ids": ids, "period_keys": periods}

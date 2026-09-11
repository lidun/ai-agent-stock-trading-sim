"""调用卡片（spec-02 §3.1/§3.2）：原文一对一精炼，离线可延迟任务生成。

- `memory_cards` 主键 memory_id 一对一；已存在且未 flagged 则跳过；重生成原地 UPSERT
  （card_version+1、model/updated_ts 刷新，created_ts 不变）。
- 触发：①无卡片（原文落库后的常规补卡）；②`memory_entries.quality='flagged'`
  （抽检发现卡片与原文不符）→ 重生成后清 flag（原文 body 不变，符合 append-only）。
- 卡片仅用于导航：token 预算 200-500，事实引用须 get_full 回原文核对（§3.2）。
- 扫描与执行归 spec-04 空闲窗口：`enqueue_pending_cards` 入队「卡片生成」，处理器调用
  `generate_card` 幂等落库。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from time import monotonic

from core.db import state_conn, write_txn

log = logging.getLogger(__name__)

_BJT = timezone(timedelta(hours=8))
CARD_PRIORITY = 6  # §3.2 补卡属可延迟组高优先级


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _audit(state, *, action: str, result: str, object_id: str, actor: str,
           detail: str) -> None:
    with write_txn(state_conn(state)) as c:
        c.execute(
            "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
            " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
            (_now_iso(), actor, action, "memory_cards", object_id, result,
             detail[:400], ""),
        )


def _entry(state, memory_id: str) -> dict | None:
    row = state_conn(state).execute(
        "SELECT id, agent_id, mem_type, ts, body, revision, quality"
        " FROM memory_entries WHERE id=?", (memory_id,)).fetchone()
    return dict(row) if row else None


def get_card(state, memory_id: str) -> dict | None:
    row = state_conn(state).execute(
        "SELECT * FROM memory_cards WHERE memory_id=?", (memory_id,)).fetchone()
    return dict(row) if row else None


def list_cards(state, agent_id: str, *, limit: int = 200) -> list[dict]:
    rows = state_conn(state).execute(
        "SELECT * FROM memory_cards WHERE agent_id=? ORDER BY updated_ts DESC LIMIT ?",
        (agent_id, int(limit))).fetchall()
    return [dict(r) for r in rows]


def list_pending_cards(state, *, agent_id: str = "", limit: int = 200) -> list[dict]:
    """待补卡：无卡片 或 原文 quality=flagged（旧→新）。"""
    sql = ("SELECT me.id, me.agent_id, me.mem_type, me.ts, me.quality,"
           " mc.memory_id AS card_id FROM memory_entries me"
           " LEFT JOIN memory_cards mc ON mc.memory_id = me.id"
           " WHERE (mc.memory_id IS NULL OR me.quality='flagged')")
    params: list = []
    if agent_id:
        sql += " AND me.agent_id=?"
        params.append(agent_id)
    rows = state_conn(state).execute(
        sql + " ORDER BY me.ts ASC LIMIT ?", params + [int(limit)]).fetchall()
    return [{"memory_id": r["id"], "agent_id": r["agent_id"],
             "mem_type": r["mem_type"], "ts": r["ts"], "quality": r["quality"],
             "reason": "flagged" if r["quality"] == "flagged" else "no_card"}
            for r in rows]


def _fallback_card(entry: dict) -> str:
    body = " ".join((entry.get("body") or "").split())
    return (f"- 类型：{entry.get('mem_type', '')}\n"
            f"- 时间：{entry.get('ts', '')}\n"
            f"- 要点：{body[:400]}")


def _llm_card(state, entry: dict, timeout_s: float) -> tuple[str, str]:
    """返回 (card, model)；未配置/失败 → 空串走确定性降级。"""
    from core import llm  # noqa: PLC0415
    started = monotonic()
    try:
        out = llm.chat(state, [
            {"role": "system", "content":
             "你是量化团队的记忆卡片生成器；只依据原文做结构化精炼（200-500 token），"
             "保留关键数字/结论/依据，不虚构、不引入原文没有的信息。"},
            {"role": "user", "content":
             f"记忆类型={entry.get('mem_type', '')}\n原文：\n"
             f"{(entry.get('body') or '')[:4000]}\n\n"
             "请输出该条记忆的调用卡片（要点式，可含关键数字）。"},
        ], timeout_s=timeout_s)
        card = (out.get("content") or "").strip()
        model = out.get("model", "")
        try:
            from core import perf_records  # noqa: PLC0415
            perf_records.record_chat_usage(
                state, agent_id=entry["agent_id"],
                task_id=f"memory_card:{entry['id']}", task_type="memory_card",
                provider=out.get("provider", ""), model=model,
                usage=out.get("usage"), ok=bool(card), started_mono=started,
                detail=f"card {entry['id']}")
        except Exception:  # noqa: BLE001 - 记账失败不影响卡片
            log.exception("卡片记账失败")
        return card, model
    except llm.LLMNotConfigured:
        return "", ""
    except llm.LLMProviderError as exc:
        log.warning("卡片 LLM 失败（确定性降级）：%s", exc)
        return "", ""


def generate_card(state, memory_id: str, *, use_llm: bool = True, force: bool = False,
                  timeout_s: float = 60.0, actor: str = "scheduler") -> dict:
    """生成/重生成单条调用卡片（幂等 UPSERT）。已存在且未 flagged 则跳过。"""
    entry = _entry(state, memory_id)
    if entry is None:
        raise LookupError(f"记忆条目不存在：{memory_id}")
    existing = get_card(state, memory_id)
    flagged = entry.get("quality") == "flagged"
    if existing and not flagged and not force:
        return {"skipped": True, "reason": "卡片已存在且未 flagged",
                "memory_id": memory_id, "card_version": existing["card_version"]}
    card, model = ("", "")
    status = "fallback"
    if use_llm:
        card, model = _llm_card(state, entry, timeout_s)
        if card:
            status = "generated"
    if not card:
        card = _fallback_card(entry)
    version = (int(existing["card_version"]) + 1) if existing else 1
    ts = _now_iso()
    with write_txn(state_conn(state)) as c:
        c.execute(
            "INSERT INTO memory_cards (memory_id, agent_id, card, card_version,"
            " model, created_ts, updated_ts) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(memory_id) DO UPDATE SET card=excluded.card,"
            " card_version=excluded.card_version, model=excluded.model,"
            " updated_ts=excluded.updated_ts",
            (memory_id, entry["agent_id"], card, version, model,
             existing["created_ts"] if existing else ts, ts),
        )
        if flagged:  # 重生成即消费 flag（原文 body 不变，仅清元数据）
            c.execute("UPDATE memory_entries SET quality='normal' WHERE id=?",
                      (memory_id,))
    _audit(state, action="memory.card", result=("recomputed" if existing else status),
           object_id=memory_id, actor=actor,
           detail=f"v{version} provider={model or '-'} reason="
                  f"{'flagged' if flagged else 'force' if force else 'missing'}")
    return {"skipped": False, "memory_id": memory_id, "card_version": version,
            "status": status, "recomputed": bool(existing), "model": model}


def enqueue_pending_cards(state, *, agent_id: str = "", limit: int = 50,
                          actor: str = "scheduler") -> list[dict]:
    """扫描待补卡并幂等入队「卡片生成」任务（可延迟组高优先级）。"""
    from core import tasks  # noqa: PLC0415
    created: list[dict] = []
    for p in list_pending_cards(state, agent_id=agent_id, limit=limit):
        r = tasks.enqueue(
            state, task_type="卡片生成", agent_id=p["agent_id"],
            dedup_key=p["memory_id"], resource_class="llm-heavy",
            is_deferrable=True, priority=CARD_PRIORITY,
            payload={"memory_id": p["memory_id"], "reason": p["reason"]}, actor=actor)
        if r.get("created"):
            created.append({**p, "task_id": r["id"]})
    return created


def _run_card_task(state, task: dict) -> str:
    memory_id = (task.get("payload") or {}).get("memory_id", "")
    if not memory_id:
        raise ValueError("卡片生成任务缺少 payload.memory_id")
    r = generate_card(state, memory_id, actor="scheduler")
    if r.get("skipped"):
        return f"卡片已存在，跳过（{memory_id}）"
    return f"卡片 v{r['card_version']} {r['status']}（{memory_id}）"

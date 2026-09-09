"""任务粒度性能与费用留痕（spec-02 §11，provider 单价配置化不写死）。

- provider_pricing：按 (provider, model) 配置 input/output/cache_read 每千 token 单价
  （元）；cache_read_per_1k 无缓存机制的 provider 置 NULL，按 input 价计（v0.3 C1）。
- performance_records：每次 LLM 调用（本切片粒度=1 次 chat）落 1 行，费用按 spec-02
  公式分缓存命中/未命中折算：cost_yuan=(tokens_in−cached)×input + cached×cache_read
  + tokens_out×output（÷1000）；未配置单价 → cost_yuan=NULL 保留，展示层标"未计价"。
- usage.cached_tokens 由 llm 适配层透传（无该字段记 0，v0.3 要求）；每日/Agent/任务
  类型聚合供容量成本审查与月度《策略体检报告》④。
"""
from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timedelta, timezone

from core.db import state_conn, write_txn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pricing_get(state, provider: str, model: str, conn=None) -> dict | None:
    c = conn or state_conn(state)
    row = c.execute(
        "SELECT provider, model, input_per_1k, output_per_1k, cache_read_per_1k"
        " FROM provider_pricing WHERE provider=? AND model=?",
        (provider, model),
    ).fetchone()
    if row is None:
        return None
    return {"provider": row["provider"], "model": row["model"],
            "input_per_1k": row["input_per_1k"] or 0.0,
            "output_per_1k": row["output_per_1k"] or 0.0,
            "cache_read_per_1k": row["cache_read_per_1k"]}


def pricing_list(state, provider: str = "", model: str = "") -> list[dict]:
    sql = ("SELECT provider, model, input_per_1k, output_per_1k,"
           " cache_read_per_1k, updated_ts, updated_by FROM provider_pricing"
           " WHERE 1=1")
    params: list = []
    if provider:
        sql += " AND provider=?"
        params.append(provider)
    if model:
        sql += " AND model=?"
        params.append(model)
    sql += " ORDER BY provider, model"
    rows = state_conn(state).execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def pricing_upsert(state, *, actor: str, provider: str, model: str,
                   input_per_1k: float, output_per_1k: float,
                   cache_read_per_1k: float | None = None) -> dict:
    """配置单价（spec-02 §11：价格变动仅改表，不写死）。值域校验后 upsert + 审计。"""
    provider = (provider or "").strip()
    model = (model or "").strip()
    if not provider or not model:
        raise ValueError("provider 与 model 不能为空")
    if input_per_1k < 0 or output_per_1k < 0:
        raise ValueError("单价不能为负")
    if cache_read_per_1k is not None and cache_read_per_1k < 0:
        raise ValueError("缓存命中单价不能为负")
    now = _now_iso()
    conn = state_conn(state)
    with write_txn(conn) as c:
        c.execute(
            "INSERT INTO provider_pricing (provider, model, input_per_1k,"
            " output_per_1k, cache_read_per_1k, updated_ts, updated_by)"
            " VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(provider, model) DO UPDATE SET"
            " input_per_1k=excluded.input_per_1k,"
            " output_per_1k=excluded.output_per_1k,"
            " cache_read_per_1k=excluded.cache_read_per_1k,"
            " updated_ts=excluded.updated_ts, updated_by=excluded.updated_by",
            (provider, model, input_per_1k, output_per_1k,
             cache_read_per_1k, now, actor),
        )
        c.execute(
            "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
            " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
            (now, actor, "settings.pricing.upsert", "provider_pricing",
             f"{provider}:{model}", "updated",
             json.dumps(
                 {"input_per_1k": input_per_1k, "output_per_1k": output_per_1k,
                  "cache_read_per_1k": cache_read_per_1k}, ensure_ascii=False), ""),
        )
    return pricing_get(state, provider, model)


def _cost(tokens_in: int, cached: int, tokens_out: int, price: dict | None):
    if price is None:
        return None
    miss = max(0, tokens_in - cached)
    cache_rate = (price["cache_read_per_1k"]
                  if price["cache_read_per_1k"] is not None
                  else price["input_per_1k"])
    return (miss * price["input_per_1k"]
            + cached * cache_rate
            + tokens_out * price["output_per_1k"]) / 1000.0


def record_chat_usage(state, *, agent_id: str, task_id: str, task_type: str,
                      provider: str, model: str, usage: dict | None,
                      ok: bool, started_mono: float | None = None,
                      detail: str = "") -> dict:
    """一次 LLM chat 的记账（本切片任务粒度=1 次调用）：落 performance_records。

    返回 {id, cost_yuan(None=未计价), priced: bool, tokens...}。task_id 重复重跑会
    追加新行（每次调用独立留痕，对应 spec 任务粒度可对账），不覆盖历史。
    """
    usage = usage or {}
    try:
        tokens_in = int(usage.get("prompt_tokens") or 0)
    except (TypeError, ValueError):
        tokens_in = 0
    try:
        tokens_out = int(usage.get("completion_tokens") or 0)
    except (TypeError, ValueError):
        tokens_out = 0
    try:
        cached = int(usage.get("cached_tokens") or 0)
    except (TypeError, ValueError):
        cached = 0
    price = pricing_get(state, provider or "", model or "")
    cost = _cost(tokens_in, cached, tokens_out, price)
    duration_ms = None
    if started_mono is not None:
        duration_ms = int((time.monotonic() - started_mono) * 1000)
    ended = _now_iso()
    started = ended
    if duration_ms:
        dt_end = datetime.now(timezone.utc)
        started = (dt_end - timedelta(milliseconds=duration_ms)
                   ).isoformat(timespec="seconds")
    rid = "pr" + secrets.token_hex(10)
    conn = state_conn(state)
    with write_txn(conn) as c:
        c.execute(
            "INSERT INTO performance_records (id, task_id, agent_id, task_type,"
            " started_ts, ended_ts, duration_ms, llm_calls, tokens_in,"
            " cached_tokens, tokens_out, cost_yuan, result, provider, model,"
            " detail, created_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, task_id, agent_id, task_type, started, ended, duration_ms,
             1, tokens_in, cached, tokens_out, cost,
             "ok" if ok else "failed", provider or "", model or "",
             (detail or "")[:1000], ended),
        )
    return {"id": rid, "ok": ok, "task_id": task_id, "agent_id": agent_id,
            "task_type": task_type, "provider": provider, "model": model,
            "tokens_in": tokens_in, "cached_tokens": cached,
            "tokens_out": tokens_out, "cost_yuan": cost,
            "priced": cost is not None, "duration_ms": duration_ms}


def ledger(state, *, limit: int = 50, agent_id: str = "", task_type: str = "") -> list[dict]:
    """最近费用台账（新→旧），供监控页与对账抽样。"""
    sql = ("SELECT id, task_id, agent_id, task_type, tokens_in, cached_tokens,"
           " tokens_out, cost_yuan, result, provider, model, detail, created_ts"
           " FROM performance_records WHERE 1=1")
    params: list = []
    if agent_id:
        sql += " AND agent_id=?"
        params.append(agent_id)
    if task_type:
        sql += " AND task_type=?"
        params.append(task_type)
    sql += " ORDER BY created_ts DESC, id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 200)))
    return [dict(r) for r in state_conn(state).execute(sql, params).fetchall()]


def daily_summary(state, *, days: int = 14) -> list[dict]:
    """近 N 自然日（UTC 日，展示层再转 BJT）按日聚合：调用数/tokens/费用。"""
    rows = state_conn(state).execute(
        "SELECT created_ts, tokens_in, cached_tokens, tokens_out, cost_yuan"
        " FROM performance_records ORDER BY created_ts DESC LIMIT ?",
        (max(1, int(days) * 500),),
    ).fetchall()
    buckets: dict[str, dict] = {}
    for r in rows:
        day = (r["created_ts"] or "")[:10]
        if not day:
            continue
        b = buckets.setdefault(day, {"date": day, "llm_calls": 0,
                                     "tokens_in": 0, "cached_tokens": 0,
                                     "tokens_out": 0, "cost_yuan": None})
        b["llm_calls"] += 1
        b["tokens_in"] += int(r["tokens_in"] or 0)
        b["cached_tokens"] += int(r["cached_tokens"] or 0)
        b["tokens_out"] += int(r["tokens_out"] or 0)
        if r["cost_yuan"] is not None:
            b["cost_yuan"] = round((b["cost_yuan"] or 0.0)
                                   + float(r["cost_yuan"]), 4)
    return [buckets[d] for d in sorted(buckets, reverse=True)]


def agent_task_summary(state, *, days: int = 14) -> list[dict]:
    """近 N 天按 Agent×任务类型聚合费用归属（元/Agent/任务类型三维）。"""
    limit = max(1, int(days)) * 500
    rows = state_conn(state).execute(
        "SELECT agent_id, task_type, tokens_in, cached_tokens, tokens_out,"
        " cost_yuan FROM performance_records ORDER BY created_ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    agg: dict[tuple, dict] = {}
    for r in rows:
        key = (r["agent_id"], r["task_type"])
        b = agg.setdefault(key, {"agent_id": r["agent_id"],
                                 "task_type": r["task_type"], "llm_calls": 0,
                                 "tokens_in": 0, "cached_tokens": 0,
                                 "tokens_out": 0, "cost_yuan": None})
        b["llm_calls"] += 1
        b["tokens_in"] += int(r["tokens_in"] or 0)
        b["cached_tokens"] += int(r["cached_tokens"] or 0)
        b["tokens_out"] += int(r["tokens_out"] or 0)
        if r["cost_yuan"] is not None:
            b["cost_yuan"] = round((b["cost_yuan"] or 0.0)
                                   + float(r["cost_yuan"]), 4)
    return [agg[k] for k in sorted(agg)]

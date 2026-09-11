"""分层摘要索引（spec-02 §4.1/§4.2，#50 可延迟无时效任务）。

- 周期口径按北京时间自然/交易周月：日 `YYYY-MM-DD`、周 ISO `YYYY-Www`、月 `YYYY-MM`；
- 生成：日=当日沉淀条目，周=本周**原文**重算（防链式误差），月=本月**周摘要**合成
  （继承周摘要 record_ids 全集，可回溯原文）；
- 谓词定义归本 spec，扫描与执行归 spec-04：`scan_pending` 在空闲窗口发现缺口并入队
  「摘要补跑」，处理器调用 `generate` 幂等 UPSERT（UNIQUE(agent_id, period, period_key)）。
- 无本地节假日日历，日/周/月边界取自然口径 + 收盘后判定（与调度器 `_is_trading_hours`
  的"无日历"折衷一致）；真正交易日历接入后仅需替换边界判定。
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import date, datetime, timedelta, timezone
from time import monotonic

from core.db import state_conn, write_txn

log = logging.getLogger(__name__)

_BJT = timezone(timedelta(hours=8))
PERIODS = ("day", "week", "month")
CLOSE_HOUR = 15  # A 股收盘 15:00（北京时间）
_MAX_ENTRIES = 5000


def day_key(d: date) -> str:
    return d.isoformat()


def week_key(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def week_bounds(key: str) -> tuple[date, date]:
    year, ww = key.split("-W")
    monday = date.fromisocalendar(int(year), int(ww), 1)
    return monday, monday + timedelta(days=6)


def month_bounds(key: str) -> tuple[date, date]:
    year, mm = (int(x) for x in key.split("-"))
    first = date(year, mm, 1)
    last = date(year + (mm // 12), (mm % 12) + 1, 1) - timedelta(days=1)
    return first, last


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _bj_date(ts: str) -> date | None:
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_BJT).date()


def _audit(state, *, action: str, result: str, object_id: str, actor: str,
           detail: str) -> None:
    with write_txn(state_conn(state)) as c:
        c.execute(
            "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
            " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
            (_now_iso(), actor, action, "memory_summaries", object_id, result,
             detail[:400], ""),
        )


def _entries_in(state, agent_id: str, start: date, end: date) -> list[dict]:
    lo = datetime(start.year, start.month, start.day, tzinfo=_BJT)
    hi = datetime(end.year, end.month, end.day, tzinfo=_BJT) + timedelta(days=1)
    rows = state_conn(state).execute(
        "SELECT id, mem_type, ts, body FROM memory_entries"
        " WHERE agent_id=? AND ts>=? AND ts<? ORDER BY ts, id LIMIT ?",
        (agent_id, lo.astimezone(timezone.utc).isoformat(timespec="seconds"),
         hi.astimezone(timezone.utc).isoformat(timespec="seconds"), _MAX_ENTRIES),
    ).fetchall()
    return [dict(r) for r in rows]


def _entry_dates(state, agent_id: str = "") -> dict[str, list[str]]:
    """按 (agent_id, BJ 日期) 归集 memory_entries 的 id。"""
    sql = "SELECT id, agent_id, ts FROM memory_entries"
    params: list = []
    if agent_id:
        sql += " WHERE agent_id=?"
        params.append(agent_id)
    out: dict[str, list[str]] = {}
    for r in state_conn(state).execute(sql, params).fetchall():
        d = _bj_date(r["ts"])
        if d is None:
            continue
        out.setdefault(f"{r['agent_id']}|{d.isoformat()}", []).append(r["id"])
    return out


def list_summaries(state, agent_id: str, *, period: str = "",
                   limit: int = 200) -> list[dict]:
    sql = "SELECT * FROM memory_summaries WHERE agent_id=?"
    params: list = [agent_id]
    if period:
        sql += " AND period=?"
        params.append(period)
    rows = state_conn(state).execute(
        sql + " ORDER BY period_key DESC LIMIT ?", params + [int(limit)]).fetchall()
    return [_row_out(r) for r in rows]


def get_summary(state, agent_id: str, period: str, period_key: str) -> dict | None:
    row = state_conn(state).execute(
        "SELECT * FROM memory_summaries WHERE agent_id=? AND period=? AND period_key=?",
        (agent_id, period, period_key)).fetchone()
    return _row_out(row) if row else None


def _row_out(r) -> dict:
    try:
        refs = json.loads(r["record_ids"] or "[]")
    except (TypeError, ValueError):
        refs = []
    return {"id": r["id"], "agent_id": r["agent_id"], "period": r["period"],
            "period_key": r["period_key"], "body": r["body"],
            "record_ids": refs if isinstance(refs, list) else [],
            "source_count": int(r["source_count"] or 0), "model": r["model"],
            "created_ts": r["created_ts"], "updated_ts": r["updated_ts"]}


def scan_pending(state, *, today: date, now_bj: datetime,
                 agent_id: str = "") -> list[dict]:
    """缺口检测（§4.1 触发谓词）→ 待入队摘要任务清单 {agent_id, period, period_key}。

    - 日：有沉淀条目的交易日已收盘（<今日，或==今日且 now_bj 已过 15:00）且无日摘要；
    - 周：有原文的上一交易周（周日 < 今日）已结束且无周摘要；
    - 月：自然月已结束、月内相关周摘要齐备且无月摘要（缺周摘要先补周再合成月）。
    """
    today_d = today
    closed_today = now_bj.hour * 60 + now_bj.minute >= CLOSE_HOUR * 60
    dates = _entry_dates(state, agent_id)
    agents = sorted({k.split("|", 1)[0] for k in dates})
    out: list[dict] = []
    for ag in agents:
        ag_dates = sorted(k.split("|", 1)[1] for k in dates
                          if k.split("|", 1)[0] == ag)
        have = {(_s["period"], _s["period_key"]) for _s in list_summaries(state, ag)}
        # 日
        for dstr in ag_dates:
            d = date.fromisoformat(dstr)
            if d > today_d or (d == today_d and not closed_today):
                continue
            if ("day", dstr) not in have:
                out.append({"agent_id": ag, "period": "day", "period_key": dstr})
        # 周（从原文推导，含更早周）
        weeks: set[str] = set()
        for dstr in ag_dates:
            weeks.add(week_key(date.fromisoformat(dstr)))
        for wk in sorted(weeks):
            _, sun = week_bounds(wk)
            if sun >= today_d:
                continue
            if ("week", wk) not in have:
                out.append({"agent_id": ag, "period": "week", "period_key": wk})
        # 月（月内相关周摘要齐备才合成）
        months: set[str] = set()
        for dstr in ag_dates:
            months.add(month_key(date.fromisoformat(dstr)))
        for mk in sorted(months):
            _, last = month_bounds(mk)
            if last >= today_d:
                continue
            mweeks = {week_key(date.fromisoformat(d))
                      for d in ag_dates if month_key(date.fromisoformat(d)) == mk}
            if mweeks and all(("week", w) in have for w in mweeks) \
                    and ("month", mk) not in have:
                out.append({"agent_id": ag, "period": "month", "period_key": mk})
    return out


def enqueue_pending(state, *, today: date, now_bj: datetime,
                    actor: str = "scheduler") -> list[dict]:
    """扫描并幂等入队「摘要补跑」任务（可延迟组），返回新建条目。"""
    from core import tasks  # noqa: PLC0415
    created: list[dict] = []
    for p in scan_pending(state, today=today, now_bj=now_bj):
        r = tasks.enqueue(
            state, task_type="摘要补跑", agent_id=p["agent_id"],
            trade_date=p["period_key"], dedup_key=f"{p['period']}:{p['period_key']}",
            resource_class="llm-heavy", is_deferrable=True, priority=4,
            payload=p, actor=actor)
        if r.get("created"):
            created.append({**p, "task_id": r["id"]})
    return created


def _load_inputs(state, agent_id: str, period: str, period_key: str) -> list[dict]:
    if period == "day":
        d = date.fromisoformat(period_key)
        return _entries_in(state, agent_id, d, d)
    if period == "week":
        s, e = week_bounds(period_key)
        return _entries_in(state, agent_id, s, e)
    if period == "month":
        s, e = month_bounds(period_key)
        return [s for s in list_summaries(state, agent_id, period="week")
                if s["period_key"] and month_key(week_bounds(s["period_key"])[0]) == period_key]
    raise ValueError(f"未知 period：{period}")


def _digest(period: str, period_key: str, inputs: list[dict]) -> str:
    lines = [f"## {period} 摘要 {period_key}",
             f"- 输入条数：{len(inputs)}"]
    for it in inputs[:50]:
        body = (it.get("body") or "").strip().replace("\n", " ")
        lines.append(f"- [{it.get('mem_type', it.get('period_key', ''))}] {body[:160]}")
    return "\n".join(lines)


def _llm_narrative(state, agent_id: str, period: str, period_key: str,
                   inputs: list[dict], timeout_s: float) -> tuple[str, str]:
    """返回 (narrative, model)；未配置/失败 → 空串走确定性降级。"""
    from core import llm  # noqa: PLC0415
    packed = "\n".join(
        f"<{i.get('mem_type', i.get('period_key', ''))}>"
        f"{(i.get('body') or '').strip()[:1200]}</{i.get('mem_type', '')}>"
        for i in inputs[:100])
    started = monotonic()
    try:
        out = llm.chat(state, [
            {"role": "system", "content":
             "你是量化团队的记忆管理员；只依据给定记录做分层摘要，"
             "保留关键数字与结论，不虚构内容，输出简洁 Markdown。"},
            {"role": "user", "content":
             f"周期={period} 键={period_key}\n记录：\n{packed}\n\n"
             "请输出该周期的分层摘要（要点式）。"},
        ], timeout_s=timeout_s)
        text = (out.get("content") or "").strip()
        model = out.get("model", "")
        try:
            from core import perf_records  # noqa: PLC0415
            perf_records.record_chat_usage(
                state, agent_id=agent_id,
                task_id=f"summary:{period}:{period_key}",
                task_type="memory_summary", provider=out.get("provider", ""),
                model=model, usage=out.get("usage"), ok=bool(text),
                started_mono=started, detail=f"summary {period}:{period_key}")
        except Exception:  # noqa: BLE001 - 记账失败不影响摘要
            log.exception("摘要记账失败")
        return text, model
    except llm.LLMNotConfigured:
        return "", ""
    except llm.LLMProviderError as exc:
        log.warning("摘要 LLM 失败（确定性降级）：%s", exc)
        return "", ""


def generate(state, agent_id: str, period: str, period_key: str, *,
             use_llm: bool = True, timeout_s: float = 90.0,
             actor: str = "scheduler") -> dict:
    """生成/重算单个分层摘要（幂等 UPSERT）。无输入 → {skipped: True}。"""
    if period not in PERIODS:
        raise ValueError(f"未知 period：{period}")
    inputs = _load_inputs(state, agent_id, period, period_key)
    if not inputs:
        return {"skipped": True, "reason": "无输入记录", "period": period,
                "period_key": period_key}
    if period == "month":  # 继承周摘要 record_ids 全集，抽检可回溯任一记录到原文
        record_ids: list[str] = []
        for w in inputs:
            for rid in (w.get("record_ids") or []):
                if rid not in record_ids:
                    record_ids.append(rid)
    else:
        record_ids = [str(i.get("id") or i.get("period_key")) for i in inputs]
    narrative, model = ("", "")
    if use_llm:
        narrative, model = _llm_narrative(
            state, agent_id, period, period_key, inputs, timeout_s)
    body = narrative or _digest(period, period_key, inputs)
    status = "generated" if narrative else "fallback"
    ts = _now_iso()
    c = state_conn(state)
    existing = get_summary(state, agent_id, period, period_key)
    sid = existing["id"] if existing else "ms" + secrets.token_hex(10)
    with write_txn(c) as cw:
        cw.execute(
            "INSERT INTO memory_summaries (id, agent_id, period, period_key, body,"
            " record_ids, source_count, model, created_ts, updated_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(agent_id, period, period_key) DO UPDATE SET"
            " body=excluded.body, record_ids=excluded.record_ids,"
            " source_count=excluded.source_count, model=excluded.model,"
            " updated_ts=excluded.updated_ts",
            (sid, agent_id, period, period_key, body, json.dumps(record_ids,
             ensure_ascii=False), len(inputs), model,
             existing["created_ts"] if existing else ts, ts),
        )
    result = "recomputed" if existing else status
    _audit(state, action="memory.summary", result=result,
           object_id=f"{agent_id}:{period}:{period_key}", actor=actor,
           detail=f"输入 {len(inputs)} 条，model={model or '-'}，{status}")
    return {"skipped": False, "period": period, "period_key": period_key,
            "id": sid, "source_count": len(inputs), "status": status,
            "recomputed": bool(existing), "model": model}


def _run_summary_task(state, task: dict) -> str:
    payload = task.get("payload") or {}
    period = payload.get("period") or ""
    period_key = payload.get("period_key") or task.get("trade_date") or ""
    agent_id = payload.get("agent_id") or task.get("agent_id") or ""
    if not (period and period_key and agent_id):
        raise ValueError("摘要补跑任务缺少 payload(period/period_key/agent_id)")
    r = generate(state, agent_id, period, period_key, actor="scheduler")
    if r.get("skipped"):
        return f"{period} {period_key} 无输入，跳过"
    return (f"{period} {period_key} {r['status']}，输入 {r['source_count']} 条"
            f"（重算={r['recomputed']}）")

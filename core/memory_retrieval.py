"""记忆检索协议（spec-02 §3.2：#23 防"全量历史"进上下文）。

```
retrieve(agent_id, query, intent, top_k=5) → list[{card|raw, memory_id, score, mem_type, from_card, ts}]
get_full(agent_id, memory_id) → 原文全文
```

- 命中卡片而非原文（`from_card=true`），卡片仅用于导航；决策引用须 get_full 回原文核对；
- **无卡片空窗兜底**：近 `recent_fallback_days` 内无卡片原文追加结构化检索，标
  `from_card=false`；
- 向量层（§8）未接入前以关键词重叠打分，接口与字段保持稳定，接入后仅替换打分实现；
- 隔离：所有查询强制注入 `agent_id`，get_full 校验归属（越权返回 LookupError）。

意图路由：intent → mem_type 加权（`_INTENT_BONUS`），非过滤——保证召回不因意图丢失。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from core.db import state_conn

log = logging.getLogger(__name__)

_BJT = timezone(timedelta(hours=8))
_CJK = re.compile(r"[\u4e00-\u9fff]+")
_WORD = re.compile(r"[A-Za-z0-9_]+")
_INTENT_BONUS = {"strategy": "strategy", "user_requirement": "user_requirement",
                 "trade_decision": "trade_decision", "market_note": "market_note"}
_INTENT_BONUS_VALUE = 0.15
_MAX_SCAN = 2000


def _settings(state):
    return getattr(state, "settings", None)


def _tokens(text: str) -> set[str]:
    toks: set[str] = set()
    for w in _WORD.findall((text or "").lower()):
        toks.add(w)
    for run in _CJK.findall(text or ""):
        if len(run) == 1:
            toks.add(run)
        else:
            for i in range(len(run) - 1):
                toks.add(run[i:i + 2])
    return toks


def _score(query_tokens: set[str], text: str) -> float:
    if not query_tokens:
        return 0.0
    hit = len(query_tokens & _tokens(text))
    return hit / len(query_tokens)


def _cutoff(days: int, now: datetime | None) -> str:
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=max(0, days))).isoformat(timespec="seconds")


def retrieve(state, agent_id: str, query: str, *, intent: str = "", top_k: int = 5,
             now: datetime | None = None) -> dict:
    """检索候选（卡片优先 + 近期无卡原文兜底）；降序按 score，tie 按 ts 降序。"""
    if not agent_id:
        raise ValueError("agent_id 不可为空")
    top_k = max(1, min(int(top_k), 50))
    q = (query or "").strip()
    qtok = _tokens(q)
    c = state_conn(state)
    bonus_type = _INTENT_BONUS.get(intent, "")
    items: list[dict] = []

    # 1) 卡片候选
    cards = c.execute(
        "SELECT mc.memory_id, mc.card AS text, mc.updated_ts AS ts, me.mem_type,"
        " me.ts AS entry_ts FROM memory_cards mc"
        " JOIN memory_entries me ON me.id = mc.memory_id"
        " WHERE mc.agent_id=? ORDER BY mc.updated_ts DESC LIMIT ?",
        (agent_id, _MAX_SCAN)).fetchall()
    for r in cards:
        s = _score(qtok, r["text"])
        if bonus_type and r["mem_type"] == bonus_type:
            s += _INTENT_BONUS_VALUE
        if qtok and s <= 0:
            continue
        items.append({"memory_id": r["memory_id"], "text": r["text"],
                      "mem_type": r["mem_type"], "score": round(min(s, 1.0), 4),
                      "from_card": True, "ts": r["entry_ts"]})

    # 2) 近窗无卡片原文兜底（结构化：agent + 时间窗 + 关键词）
    days = int(getattr(_settings(state), "recent_fallback_days", 5) or 5)
    for r in c.execute(
        "SELECT me.id, me.body AS text, me.mem_type, me.ts FROM memory_entries me"
        " LEFT JOIN memory_cards mc ON mc.memory_id = me.id"
        " WHERE me.agent_id=? AND mc.memory_id IS NULL AND me.ts>=?"
        " ORDER BY me.ts DESC LIMIT ?",
        (agent_id, _cutoff(days, now), _MAX_SCAN)).fetchall():
        s = _score(qtok, r["text"])
        if bonus_type and r["mem_type"] == bonus_type:
            s += _INTENT_BONUS_VALUE
        if qtok and s <= 0:
            continue
        items.append({"memory_id": r["id"], "text": r["text"],
                      "mem_type": r["mem_type"], "score": round(min(s, 1.0), 4),
                      "from_card": False, "ts": r["ts"]})

    items.sort(key=lambda x: (x["score"], x["ts"] or ""), reverse=True)
    out = items[:top_k]
    return {"agent_id": agent_id, "query": q, "intent": intent, "top_k": top_k,
            "count": len(out), "items": out}


def get_full(state, agent_id: str, memory_id: str) -> dict:
    """取原文全文；越权/不存在 → LookupError（隔离断言）。"""
    row = state_conn(state).execute(
        "SELECT * FROM memory_entries WHERE id=?", (memory_id,)).fetchone()
    if row is None or row["agent_id"] != agent_id:
        raise LookupError(f"记忆条目不存在或不属于该 Agent：{memory_id}")
    import json  # noqa: PLC0415
    try:
        refs = json.loads(row["ref_ids"] or "[]")
    except (TypeError, ValueError):
        refs = []
    return {"memory_id": row["id"], "agent_id": row["agent_id"],
            "mem_type": row["mem_type"], "version_no": row["version_no"],
            "ts": row["ts"], "body": row["body"],
            "ref_ids": refs if isinstance(refs, list) else [],
            "revision": int(row["revision"] or 0), "source": row["source"],
            "quality": row["quality"]}


def card_backlog(state, *, agent_id: str = "", now: datetime | None = None,
                limit: int = 200) -> list[dict]:
    """无卡片滞留超阈值（§3.2 超时补卡）：旧→新，供高优先级即时补卡。"""
    hours = int(getattr(_settings(state), "card_backlog_hours", 24) or 24)
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=max(0, hours))).isoformat(timespec="seconds")
    sql = ("SELECT me.id, me.agent_id, me.mem_type, me.ts FROM memory_entries me"
           " LEFT JOIN memory_cards mc ON mc.memory_id = me.id"
           " WHERE mc.memory_id IS NULL AND me.ts<=?")
    params: list = [cutoff]
    if agent_id:
        sql += " AND me.agent_id=?"
        params.append(agent_id)
    rows = state_conn(state).execute(
        sql + " ORDER BY me.ts ASC LIMIT ?", params + [int(limit)]).fetchall()
    return [{"memory_id": r["id"], "agent_id": r["agent_id"],
             "mem_type": r["mem_type"], "ts": r["ts"]} for r in rows]

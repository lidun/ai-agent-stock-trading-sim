"""演进记忆只读（spec-06 §6.4 策略演进卡内嵌；spec-02 §3.1 + spec-05 §4.1/§4.2）。

载体 memory_entries（迁移 v21，type=strategy）：每次优化记录（修改前/后/依据/
预期/结果，body 全文）逐条挂 version_no，append-only + UNIQUE(agent_id, dedup_key)
幂等。本片只读展示（方案：只读+壳）——公开写入方为引擎 EVOQUANT 优化流
（spec-05 §4.1），卡片/分层摘要/向量与 market_note 滚动策略属 spec-02 后续切片。
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone

from core.db import state_conn

MEM_TYPE = "strategy"
_MAX_LIMIT = 200


def _now_ts_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _require_agent(state, agent_id: str) -> None:
    from core.accountstore import accounts_for_agent  # noqa: PLC0415
    if not accounts_for_agent(state, agent_id):
        raise LookupError(f"Agent {agent_id} 不存在或无账户")


def _row_out(r) -> dict:
    try:
        refs = json.loads(r["ref_ids"]) if r["ref_ids"] else []
    except (TypeError, ValueError):
        refs = []
    return {
        "id": r["id"],
        "agent_id": r["agent_id"],
        "mem_type": r["mem_type"],
        "version_no": r["version_no"],
        "ts": r["ts"],
        "body": r["body"],
        "ref_ids": refs if isinstance(refs, list) else [],
        "revision": int(r["revision"] or 0),
        "source": r["source"],
        "dedup_key": r["dedup_key"],
        "quality": r["quality"],
    }


def list_strategy_memory(state, agent_id: str, *, version_no: str = "",
                         limit: int = 100) -> dict:
    """type=strategy 演进记忆（ts 降序）。"""
    _require_agent(state, agent_id)
    limit = max(1, min(int(limit), _MAX_LIMIT))
    c = state_conn(state)
    sql = ("SELECT * FROM memory_entries WHERE agent_id=? AND mem_type=?"
           " AND (? = '' OR version_no = ?)")
    params: list = [agent_id, MEM_TYPE, version_no, version_no]
    rows = c.execute(sql + " ORDER BY ts DESC, id DESC LIMIT ?",
                     params + [limit]).fetchall()
    items = [_row_out(r) for r in rows]
    versions = sorted({i["version_no"] for i in items if i["version_no"]})
    return {
        "agent_id": agent_id,
        "mem_type": MEM_TYPE,
        "version_no": version_no,
        "total": len(items),
        "versions": versions,
        "items": items,
    }


def _append(state, agent_id: str, *, version_no: str, body: str,
            ref_ids: list | None = None, source: str = "", dedup_key: str = "",
            quality: str = "normal", ts: str | None = None) -> dict:
    """内部受控写入（测试/壳用；公开写方=引擎优化流，本片不开放路由）。"""
    _require_agent(state, agent_id)
    dedup = dedup_key or f"strategy:{version_no}:{hashlib.sha1(body.encode()).hexdigest()[:16]}"
    rid = f"{agent_id}:mem:{version_no}:{int(datetime.now().timestamp() * 1000)}:{secrets.token_hex(3)}"
    from core.db import write_txn  # noqa: PLC0415
    c = state_conn(state)
    with write_txn(c) as cw:
        cw.execute(
            "INSERT OR IGNORE INTO memory_entries"
            " (id, agent_id, mem_type, version_no, ts, body, ref_ids, revision,"
            "  source, dedup_key, quality)"
            " VALUES (?,?,?,?,?,?,?,0,?,?,?)",
            (rid, agent_id, MEM_TYPE, version_no, ts or _now_ts_iso(), body,
             json.dumps(ref_ids or [], ensure_ascii=False), source, dedup, quality),
        )
        row = cw.execute(
            "SELECT * FROM memory_entries WHERE agent_id=? AND dedup_key=?",
            (agent_id, dedup),
        ).fetchone()
    return _row_out(row)

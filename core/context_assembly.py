"""上下文装配（spec-02 §7）：模板/意图路由 + assemble 装配即预算。

- §7.0 `context_templates`：route_table 的 key 即合法 intent（选股/复盘/日报/审批/优化
  + fallback）；未建模板用内置默认路由表；演进=新版本插入 + active 切换，旧版本标
  superseded_ts 保留，全审计。
- §7.1 `assemble` 产出 prefix_static / charter_summary / memory_block / data_digest / budget；
  检索块复用 memory_retrieval（卡片+月/周摘要），数据块由调用方工具聚合先行（数据不进 prompt）。
- §7.2 静态前缀携带 charter_version 戳（缓存键=agent_id+charter_version+模型）。
- §7.3 装配即预算：估算 token 与预算对照，超限标"高耗任务"（写 performance_records）。
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone

from core.db import state_conn, write_txn

log = logging.getLogger(__name__)

DEFAULT_ROUTE_TABLE: dict[str, list[str]] = {
    "选股": ["市场观察卡片", "近期决策"],
    "复盘": ["卖出跟踪", "演进记录", "失败修改"],
    "日报": ["昨日计划", "当日引擎数据段"],
    "审批": ["申请历史"],
    "优化": ["演进史", "版本链", "信号统计摘要"],
    "fallback": ["近期卡片", "月摘要", "周摘要"],
}
DEFAULT_BLOCK_ORDER = ["prefix_static", "charter_summary", "memory_block", "data_digest"]
_EST_CHARS_PER_TOKEN = 2  # 中英混合粗略估算


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _settings(state):
    return getattr(state, "settings", None)


def _est_tokens(text: str) -> int:
    return max(0, (len(text or "") + _EST_CHARS_PER_TOKEN - 1) // _EST_CHARS_PER_TOKEN)


def _audit(state, *, action: str, result: str, object_id: str, actor: str,
           detail: str) -> None:
    with write_txn(state_conn(state)) as c:
        c.execute(
            "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
            " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
            (_now_iso(), actor, action, "context_templates", object_id, result,
             detail[:400], ""),
        )


def _row_out(r) -> dict:
    def _load(s, default):
        try:
            return json.loads(s or default)
        except (TypeError, ValueError):
            return json.loads(default)
    return {"id": r["id"], "agent_id": r["agent_id"],
            "template_version": int(r["template_version"]),
            "route_table": _load(r["route_table"], "{}"),
            "block_order": _load(r["block_order"], "[]"),
            "active": bool(r["active"]), "created_by": r["created_by"],
            "created_ts": r["created_ts"], "superseded_ts": r["superseded_ts"]}


def active_template(state, agent_id: str) -> dict | None:
    row = state_conn(state).execute(
        "SELECT * FROM context_templates WHERE agent_id=? AND active=1"
        " ORDER BY template_version DESC LIMIT 1", (agent_id,)).fetchone()
    return _row_out(row) if row else None


def route_table(state, agent_id: str) -> dict:
    tpl = active_template(state, agent_id)
    return tpl["route_table"] if tpl and tpl["route_table"] else DEFAULT_ROUTE_TABLE


def effective_intent(rt: dict, intent: str) -> str:
    """intent ∈ 路由表则用之，否则 fallback（§7.0）。"""
    return intent if intent in rt else "fallback"


def create_template(state, agent_id: str, *, route_table_: dict | None = None,
                    block_order: list | None = None, created_by: str = "manager",
                    actor: str = "manager") -> dict:
    """插入新模板版本并切换 active（旧版标 superseded_ts）。幂等键=版本号。"""
    c = state_conn(state)
    if c.execute("SELECT 1 FROM agents WHERE id=?", (agent_id,)).fetchone() is None:
        raise LookupError(f"Agent 不存在：{agent_id}")
    rt = route_table_ if route_table_ is not None else DEFAULT_ROUTE_TABLE
    order = block_order if block_order is not None else DEFAULT_BLOCK_ORDER
    ts = _now_iso()
    with write_txn(c) as cw:
        prev = cw.execute(
            "SELECT MAX(template_version) AS v FROM context_templates WHERE agent_id=?",
            (agent_id,)).fetchone()["v"]
        version = int(prev or 0) + 1
        cw.execute(
            "UPDATE context_templates SET active=0, superseded_ts=?"
            " WHERE agent_id=? AND active=1", (ts, agent_id))
        tid = "ct" + secrets.token_hex(10)
        cw.execute(
            "INSERT INTO context_templates (id, agent_id, template_version,"
            " route_table, block_order, active, created_by, created_ts, superseded_ts)"
            " VALUES (?,?,?,?,?,1,?,?,'')",
            (tid, agent_id, version, json.dumps(rt, ensure_ascii=False),
             json.dumps(order, ensure_ascii=False), created_by, ts))
    _audit(state, action="context.template", result="active",
           object_id=f"{agent_id}:v{version}", actor=actor,
           detail=f"新装配模板 v{version}，intents={sorted(rt.keys())}")
    return _row_out(state_conn(state).execute(
        "SELECT * FROM context_templates WHERE id=?", (tid,)).fetchone())


def _prefix_static(state, agent_id: str) -> tuple[str, str, str]:
    """返回 (prefix, charter_version, charter_summary)。"""
    from core import strategy_profile  # noqa: PLC0415
    try:
        prof = strategy_profile.profile(state, agent_id)
    except Exception:  # noqa: BLE001 - 无账户/无章程时退化为最小前缀
        return ("你是量化交易子 Agent。", "", "")
    active = prof.get("active") or {}
    version = active.get("version_no", "")
    lines = ["你是量化交易子 Agent。", "## 策略理念"]
    if active.get("core_belief"):
        lines.append(str(active["core_belief"]))
    layers = active.get("layers") or []
    for ly in layers if isinstance(layers, list) else []:
        if isinstance(ly, dict) and ly.get("text"):
            lines.append(f"- {ly.get('name', '')}：{ly['text']}")
    if version:
        lines.append(f"（charter_version={version}）")
    return "\n".join(lines), version, (active.get("note") or "")


def _memory_block(state, agent_id: str, intent: str, query: str,
                  *, top_k: int, now=None) -> dict:
    from core import memory_retrieval, summaries  # noqa: PLC0415
    r = memory_retrieval.retrieve(state, agent_id, query, intent=intent,
                                  top_k=top_k, now=now)
    summs = summaries.list_summaries(state, agent_id)
    monthly = next((s for s in summs if s["period"] == "month"), None)
    weekly = next((s for s in summs if s["period"] == "week"), None)
    return {"intent": r["intent"], "query": r["query"], "items": r["items"],
            "month_summary": monthly["body"] if monthly else "",
            "week_summary": weekly["body"] if weekly else ""}


def _mark_high_cost(state, agent_id: str, task_id: str, estimate: int,
                    budget: int, intent: str) -> None:
    if not task_id:
        return
    try:
        with write_txn(state_conn(state)) as c:
            c.execute(
                "INSERT INTO performance_records (id, task_id, agent_id, task_type,"
                " result, high_cost_flag, detail, created_ts)"
                " VALUES (?,?,?,?,'high_cost',1,?,?)",
                ("pr" + secrets.token_hex(10), task_id, agent_id,
                 f"context.{intent}", f"装配估算 {estimate} > 预算 {budget}", _now_iso()))
    except Exception:  # noqa: BLE001 - 标记失败不影响装配
        log.exception("高耗任务标记失败")


def assemble(state, agent_id: str, task_intent: str,
             task_payload: dict | None = None, *, top_k: int = 5,
             now=None) -> dict:
    """装配一次任务上下文（§7.1）；返回 prefix_static/charter_summary/memory_block/
    data_digest/budget。"""
    payload = task_payload or {}
    rt = route_table(state, agent_id)
    intent = effective_intent(rt, task_intent)
    prefix, charter_version, charter_summary = _prefix_static(state, agent_id)
    query = str(payload.get("query") or payload.get("topic") or task_intent or "")
    block = _memory_block(state, agent_id, intent, query, top_k=top_k, now=now)
    data_digest = payload.get("data_digest") or {}
    blocks = {"prefix_static": prefix, "charter_summary": charter_summary,
              "memory_block": json.dumps(block, ensure_ascii=False, default=str),
              "data_digest": json.dumps(data_digest, ensure_ascii=False, default=str)}
    tokens = {k: _est_tokens(v) for k, v in blocks.items()}
    estimate = sum(tokens.values())
    budget = int(getattr(_settings(state), "context_budget_tokens", 8000) or 8000)
    high_cost = estimate > budget
    if high_cost:
        _mark_high_cost(state, agent_id, str(payload.get("task_id", "")),
                        estimate, budget, intent)
    tpl = active_template(state, agent_id)
    return {
        "agent_id": agent_id, "task_intent": task_intent, "intent": intent,
        "template_version": (tpl or {}).get("template_version", 0),
        "route": rt.get(intent, []),
        "block_order": (tpl or {}).get("block_order", DEFAULT_BLOCK_ORDER),
        "prefix_static": prefix, "charter_summary": charter_summary,
        "charter_version": charter_version, "memory_block": block,
        "data_digest": data_digest,
        "budget": {"estimated_tokens": estimate, "budget_tokens": budget,
                   "high_cost": high_cost, "blocks": tokens},
    }

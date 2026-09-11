"""归档经验提取（spec-05 §5：Agent 归档/退休终局归因，防样本沉底与系统遗忘）。

流程：
1. 终局统计（确定性，零 token）：该 Agent 全部已结清信号按 (concept_tag × env_bucket)
   分桶，concept_tag 按 kb_tag_aliases 归并到规范名（§3.11），统计口径同 kb_stats（§3.3）；
2. LLM 终局归因：结合终局统计产出「什么有效/什么无效/为什么」（未配置/失败确定性降级）；
3. 管理 Agent 评审：用户/管理 Agent 在报告上确认（review_ref 留痕）；
4. 回填知识库：证实有效 → 归并到现有条目（写别名）或新建 observing；确认无效 → 条目失效；
5. 全程审计。

归档 Agent 历史信号样本继续参与跨 Agent 统计（授权路径，本模块只读不改写信号行）。
"""
from __future__ import annotations

import json
import secrets
import time as _tm
from datetime import datetime, timezone

from core import kb, llm, perf_records
from core.chatstore import get_agent
from core.db import state_conn, write_txn

RETRO_STATUS = ("pending_review", "confirmed")

_PROMPT = """你是量化团队管理 Agent，正在为即将归档/退休的策略子 Agent「{agent_name}」（{agent_id}）撰写终局归因报告。

只允许使用下方【终局统计】：该 Agent 全部已结清信号按（概念 × 环境桶）归并，含样本/胜率/期望值（含费）；trial 样本已排除；concept_tag 已按月度归并映射到规范名。

请输出 Markdown（不加代码围栏，≤800 字），覆盖：
- 哪些概念在该 Agent 被证实有效（期望值为正、样本充足），哪些无效或证据不足；
- 可能原因（结合环境桶差异与样本量，不得编造具体行情/新闻）；
- 对知识库的处置建议：有效→合并进现有条目或新建（观察态）；无效→失效证据追加；
- 若统计不足，明确写「样本不足，暂不建议处置」。

【终局统计】
{stats}
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve_tags(conn) -> tuple[dict, dict]:
    alias = {r["alias"]: r["canonical_kb_id"] for r in conn.execute(
        "SELECT alias, canonical_kb_id FROM kb_tag_aliases").fetchall()}
    idname = {r["id"]: r["name"] for r in conn.execute(
        "SELECT id, name FROM kb_entries").fetchall()}
    return alias, idname


def final_stats(state, agent_id: str) -> dict:
    """§5 步骤1 终局统计（确定性，零 token；concept_tag 按 kb_tag_aliases 归并）。"""
    conn = state_conn(state)
    alias, idname = _resolve_tags(conn)
    rows = conn.execute(
        "SELECT sr.concept_tag, sr.env_bucket, sr.fwd_return_pct, sr.quality,"
        " sr.exception FROM signal_registry sr"
        " JOIN accounts a ON a.id = sr.account_id"
        " WHERE a.agent_id=? AND sr.trial_flag=0"
        " AND sr.sig_type IN ('candidate','buy','sell')", (agent_id,),
    ).fetchall()
    grouped: dict[tuple, dict] = {}
    for r in rows:
        tag = r["concept_tag"] or ""
        cid = alias.get(tag)
        name = idname.get(cid, tag) if cid else tag
        bucket = (r["env_bucket"] or "all").strip() or "all"
        g = grouped.setdefault((name, bucket), {"rows": [], "raw": set()})
        g["rows"].append(r)
        if tag and tag != name:
            g["raw"].add(tag)
    buckets: list[dict] = []
    for (name, bucket) in sorted(grouped):
        g = grouped[(name, bucket)]
        agg = kb._aggregate(g["rows"])
        if agg["sample_n"] and (agg["expectancy"] or 0.0) > 0:
            verdict = "effective"
        elif agg["sample_n"] and (agg["expectancy"] or 0.0) < 0:
            verdict = "ineffective"
        else:
            verdict = "neutral"
        buckets.append({
            "concept_tag": name, "raw_tags": sorted(g["raw"]),
            "env_bucket": bucket, "verdict": verdict, **agg,
        })
    settled = sum(b["sample_n"] for b in buckets)
    return {
        "agent_id": agent_id, "signal_total": len(rows), "settled_n": settled,
        "effective": sum(1 for b in buckets if b["verdict"] == "effective"),
        "ineffective": sum(1 for b in buckets if b["verdict"] == "ineffective"),
        "buckets": buckets,
    }


def _audit(state, *, action: str, result: str, object_id: str, detail: str,
           actor: str) -> None:
    with write_txn(state_conn(state)) as c:
        c.execute(
            "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
            " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
            (_now_iso(), actor, action, "retro_reports", object_id, result,
             detail[:400], ""),
        )


def build_report(state, agent_id: str, *, timeout_s: float = 90.0,
                 actor: str = "manager") -> dict:
    """生成归档经验提取报告（终局统计 + LLM 终局归因，落 pending_review）。"""
    ag = get_agent(state, agent_id)
    if ag is None:
        raise LookupError(f"Agent 不存在：{agent_id}")
    stats = final_stats(state, agent_id)
    report_id = "rr" + secrets.token_hex(10)
    started = _tm.monotonic()
    task_id = f"retro.extraction:{report_id}"
    attribution = ""
    attr_status = "not_configured"
    try:
        out = llm.chat(state, [
            {"role": "system", "content": "你是严谨的量化团队管理 Agent；只依据给定终局统计做归因，不虚构数据。"},
            {"role": "user", "content": _PROMPT.format(
                agent_name=ag.get("name", agent_id), agent_id=agent_id,
                stats=json.dumps(stats, ensure_ascii=False))},
        ], timeout_s=timeout_s)
        attribution = (out.get("content") or "").strip()
        attr_status = "generated" if attribution else "empty_output"
        perf_records.record_chat_usage(
            state, agent_id=agent_id, task_id=task_id,
            task_type="retro_extraction", provider=out.get("provider", ""),
            model=out.get("model", ""), usage=out.get("usage"),
            ok=attr_status == "generated", started_mono=started,
            detail=f"retro attribution {attr_status}")
    except llm.LLMNotConfigured:
        attr_status = "not_configured"
    except llm.LLMProviderError as exc:
        attr_status = "llm_failed"
        perf_records.record_chat_usage(
            state, agent_id=agent_id, task_id=task_id,
            task_type="retro_extraction", provider="", model="", usage=None,
            ok=False, started_mono=started, detail=str(exc)[:400])
    ts = _now_iso()
    with write_txn(state_conn(state)) as c:
        c.execute(
            "INSERT INTO retro_reports (id, agent_id, status, stats, attribution,"
            " attribution_status, review_ref, created_ts, updated_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (report_id, agent_id, "pending_review",
             json.dumps(stats, ensure_ascii=False), attribution, attr_status,
             "{}", ts, ts))
    _audit(state, action="kb.retro.extract", result=attr_status,
           object_id=report_id, actor=actor,
           detail=f"agent={agent_id} 有效桶={stats['effective']} 无效桶="
                  f"{stats['ineffective']} 归因={attr_status}")
    return get_report(state, report_id)


def _row(r) -> dict:
    def _loads(s):
        try:
            return json.loads(s or "{}")
        except ValueError:
            return {}
    return {
        "id": r["id"], "agent_id": r["agent_id"], "status": r["status"],
        "stats": _loads(r["stats"]), "attribution": r["attribution"],
        "attribution_status": r["attribution_status"],
        "review_ref": _loads(r["review_ref"]),
        "created_ts": r["created_ts"], "updated_ts": r["updated_ts"],
    }


def get_report(state, report_id: str) -> dict | None:
    row = state_conn(state).execute(
        "SELECT * FROM retro_reports WHERE id=?", (report_id,)).fetchone()
    return _row(row) if row else None


def list_reports(state, agent_id: str = "", *, limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM retro_reports"
    params: list = []
    if agent_id:
        sql += " WHERE agent_id=?"
        params.append(agent_id)
    sql += " ORDER BY created_ts DESC LIMIT ?"
    params.append(max(1, min(int(limit), 200)))
    return [_row(r) for r in state_conn(state).execute(sql, params).fetchall()]


def confirm_report(state, report_id: str, *, actor: str = "user",
                   note: str = "", decisions: list | None = None) -> dict:
    """管理 Agent 评审确认 + 回填知识库（§5 步骤3/4）。

    decisions=[{concept_tag, action: merge|create|invalidate, kb_id?, type?, note?}]：
    - merge：写别名（自由标签→规范条目，append-only，统计按规范名归并）；
    - create：以该概念名新建 observing 条目（source=retrospective）；
    - invalidate：目标条目失效（invalid_reason 强化）。
    报告状态 pending_review→confirmed，review_ref 记录决策与应用结果。
    """
    rpt = get_report(state, report_id)
    if rpt is None:
        raise LookupError(f"报告不存在：{report_id}")
    if rpt["status"] == "confirmed":
        raise ValueError("报告已确认，不可重复确认")
    applied: list[dict] = []
    for d in decisions or []:
        tag = (d.get("concept_tag") or "").strip()
        action = d.get("action")
        try:
            if action == "merge":
                res = kb.merge_tag(state, alias=tag,
                                   canonical_kb_id=d.get("kb_id", ""),
                                   actor=actor, reason=d.get("note", "") or "归档经验提取回填")
                applied.append({"concept_tag": tag, "action": "merge",
                                "kb_id": res["canonical_kb_id"],
                                "created": res["created"]})
            elif action == "create":
                ent = kb.create_entry(
                    state, name=tag, type_=d.get("type", "positive"),
                    description=d.get("note", "") or "归档经验提取回填（观察态）",
                    source="retrospective", origin_agent=rpt["agent_id"],
                    created_by=actor)
                applied.append({"concept_tag": tag, "action": "create",
                                "kb_id": ent["id"]})
            elif action == "invalidate":
                ent = kb.transition_kb(state, d.get("kb_id", ""),
                                       action="invalidate",
                                       note=d.get("note", "") or "归档经验提取：确认无效",
                                       actor=actor)
                applied.append({"concept_tag": tag, "action": "invalidate",
                                "kb_id": ent["id"]})
            else:
                applied.append({"concept_tag": tag, "action": action,
                                "error": "未知动作"})
        except (ValueError, LookupError) as exc:
            applied.append({"concept_tag": tag, "action": action,
                            "error": str(exc)})
    ts = _now_iso()
    review = {"decided_by": actor, "note": note, "ts": ts, "applied": applied}
    with write_txn(state_conn(state)) as c:
        c.execute(
            "UPDATE retro_reports SET status='confirmed', review_ref=?,"
            " updated_ts=? WHERE id=?",
            (json.dumps(review, ensure_ascii=False), ts, report_id))
    _audit(state, action="kb.retro.confirm", result="confirmed",
           object_id=report_id, actor=actor,
           detail=f"agent={rpt['agent_id']} 回填 {len(applied)} 项"
                  + (f"（{note}）" if note else ""))
    return get_report(state, report_id)

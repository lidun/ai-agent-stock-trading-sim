"""日报 LLM 叙述段生产者（spec-04 §5.2/§5.4 六段结构：一二三=引擎数据段、
四~六=LLM 叙述段；§8 统一适配层；§9.1 未配置/失败确定性降级）。

- 前置依赖（spec-04 §2.5 B3）：当日结算 done 才允许生成正常日报叙述段。
- 上下文只取已落库确定性事实（摘要/持仓/成交/降级标注/活跃版本配置/上日叙述），
  不引外部行情资讯；市场观察段明确标"主观"，禁止虚构当日具体行情数据。
- provider 未配置/调用失败：不阻塞日报，narrative 保持空并落审计
  （report.narrative_generate result=failed/not_configured），可稍后重跑。
"""
from __future__ import annotations

import json

from core import llm, reporting
from core.db import state_conn, write_txn

_PROMPT_NARRATIVE = """你是策略子 Agent「{agent_name}」（id {agent_id}）的日报叙述撰写者，正在撰写 {trade_date} 的收盘日报叙述段（spec-04 §5.2 六段结构中的四~六段）。

只允许使用下面【确定性事实】中给出的数据；不得编造当日具体行情涨跌、指数点位或个股新闻。市场观察若超出事实范围，须标注"（主观推演，未接入实时资讯）"。

【确定性事实】
- 账户：{agent_name}（{agent_id}），现役版本 {version}；活跃版本风控约束：{risk_summary}
- 当日摘要：现金 {cash}，NAV {nav}，当日盈亏 {today_pnl}，累计盈亏 {total_pnl}
- 持仓：{positions}
- 当日成交：{trades}
- 数据降级/备注：{annotations}
- 上一叙述段（供语境衔接，引用时勿重复整段）：{prev_narrative}

请按以下三节输出 Markdown（不加代码围栏，总长 ≤1500 字；语句简洁、可证伪、不喊口号）：
【四、市场观察（主观）】
基于模型既有知识的当日市场环境推演，仅限定性判断，不得虚构具体数字/新闻事件。
【五、判断与明日方向】
结合持仓与当日成交给出操作回顾（引用事实）、明日方向（候选动作与触发条件），所有建议须满足上方风控约束与交易红线。
【六、申报叙述】
如有能力/知识/策略优化申报动议在此提出并简述理由与收益度量；无则写"本日无新增申报动议。" """


def _fmt_num(v):
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v or "")


def _load_active_version(state, account_id: str) -> dict:
    c = state_conn(state)
    agent = c.execute(
        "SELECT name, role FROM agents WHERE id=?", (account_id,)).fetchone()
    agent_name = agent["name"] if agent else account_id
    ver = c.execute(
        "SELECT version_no, config FROM strategy_versions"
        " WHERE agent_id=? AND status='active' ORDER BY created_ts DESC LIMIT 1",
        (account_id,)).fetchone()
    version = ver["version_no"] if ver else ""
    risk = ""
    if ver:
        try:
            cfg = json.loads(ver["config"] or "{}")
            risk = json.dumps(cfg.get("risk", {}), ensure_ascii=False)
        except (ValueError, AttributeError):
            risk = ""
    return {"agent_name": agent_name, "version": version, "risk": risk}


def _positions_txt(items) -> str:
    if not items:
        return "（空仓）"
    out = []
    for p in items[:12]:
        try:
            mv = f"{float(p.get('market_value', 0)):,.0f}"
        except (TypeError, ValueError):
            mv = str(p.get("market_value", ""))
        out.append(f"{p.get('symbol')} 数量{p.get('quantity')} 成本{p.get('avg_cost')} "
                   f"收{p.get('close')} 市值{mv}")
    return "；".join(out) if out else "（空仓）"


def _trades_txt(trades) -> str:
    if not trades:
        return "（无成交）"
    out = []
    for t in trades[:12]:
        side = {"buy": "买入", "sell": "卖出"}.get(t.get("side"), t.get("side"))
        out.append(f"{t.get('symbol')} {side} {t.get('qty')}股@{t.get('price')} "
                   f"金额{t.get('amount')} 费用{t.get('fee_total')} 依据:{t.get('reason') or '—'}")
    return "；".join(out) if out else "（无成交）"


def _prev_narrative(state, account_id: str, trade_date: str) -> str:
    rows = reporting.list_engine_reports(state, account_id)
    for r in rows:
        if r["trade_date"] < trade_date and r["narrative"]:
            return r["narrative"][:800]
    return "（首篇）"


def _report(state, account_id: str, trade_date: str) -> dict | None:
    rows = reporting.list_engine_reports(state, account_id, trade_date)
    return rows[0] if rows else None


def generate_narrative(state, account_id: str, trade_date: str, *,
                       force: bool = False, timeout_s: float = 90.0,
                       actor: str = "strategy_agent") -> dict:
    """为指定账户/交易日补写 LLM 叙述段；返回确定性状态码。"""
    report = _report(state, account_id, trade_date)
    if report is None:
        return {"ok": False, "code": "no_report", "detail": "该日无日报记录"}
    ds = report["data_section"] or {}
    if not (ds.get("settlement") or {}).get("done"):
        return {"ok": False, "code": "not_settled",
                "detail": "当日结算未 done（spec-04 B3：结算 done 才生成正常日报叙述段）"}
    if report["narrative"] and not force:
        return {"ok": True, "code": "skipped", "detail": "已存在叙述段（force 可重写）",
                "version": report["version"], "narrative_len": len(report["narrative"])}

    meta = _load_active_version(state, account_id)
    summary = ds.get("summary", {}) or {}
    positions = ds.get("positions", {}) or {}
    trades = (ds.get("operations", {}) or {}).get("trades", []) or []
    annotations = (ds.get("annotations", {}) or {})
    notes = annotations.get("notes") or []
    ann = []
    if notes:
        ann.append(f"备注:{'；'.join(notes)}")
    if annotations.get("unsettled"):
        ann.append("未结算角标")
    if annotations.get("degraded"):
        ann.append(f"降级:{','.join(annotations['degraded'])}")

    user = _PROMPT_NARRATIVE.format(
        agent_name=meta["agent_name"], agent_id=account_id, trade_date=trade_date,
        version=meta["version"] or "（未登记）", risk_summary=meta["risk"] or "（未登记，默认红线）",
        cash=_fmt_num(summary.get("cash")), nav=_fmt_num(summary.get("nav")),
        today_pnl=_fmt_num(summary.get("today_pnl")),
        total_pnl=_fmt_num(summary.get("total_pnl")),
        positions=_positions_txt(positions.get("items") or []),
        trades=_trades_txt(trades),
        annotations="；".join(ann) or "无",
        prev_narrative=_prev_narrative(state, account_id, trade_date),
    )

    try:
        out = llm.chat(state, [
            {"role": "system", "content": "你是严谨的量化策略子 Agent 日报叙述员；只陈述可支撑的判断，明确区分事实、推演与拟议，不虚构数据。"},
            {"role": "user", "content": user},
        ], timeout_s=timeout_s)
    except llm.LLMNotConfigured as exc:
        _audit(state, report, "not_configured", str(exc), usage=None)
        return {"ok": False, "code": "not_configured", "detail": str(exc)}
    except llm.LLMProviderError as exc:
        _audit(state, report, "failed", str(exc), usage=None)
        return {"ok": False, "code": "llm_failed", "detail": str(exc)}

    narrative = (out.get("content") or "").strip()
    if not narrative:
        _audit(state, report, "failed", "模型返回空叙述", usage=out.get("usage"))
        return {"ok": False, "code": "empty_output", "detail": "模型返回空叙述"}
    try:
        reporting.update_narrative(
            state, account_id, trade_date, report["version"], narrative, actor=actor)
    except ValueError as exc:
        return {"ok": False, "code": "invalid", "detail": str(exc)}
    _audit(state, report, "ok", f"narrative_len={len(narrative)}",
           usage=out.get("usage"), model=out.get("model"))
    return {"ok": True, "code": "generated", "version": report["version"],
            "narrative_len": len(narrative), "model": out.get("model"),
            "usage": out.get("usage")}


def _audit(state, report: dict, result: str, detail: str,
           usage: dict | None, model: str = "") -> None:
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    meta = {"detail": detail}
    if usage:
        meta["usage"] = {k: v for k, v in usage.items() if v is not None}
    if model:
        meta["model"] = model
    with write_txn(state_conn(state)) as tx:
        tx.execute(
            "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
            " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
            (ts, "strategy_agent", "report.narrative_generate", "daily_reports",
             report["id"], result, json.dumps(meta, ensure_ascii=False), ""),
        )

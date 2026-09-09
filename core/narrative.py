"""日报 LLM 叙述段生产者（spec-04 §5.2/§5.4 六段结构：一二三=引擎数据段、
四~六=LLM 叙述段；§8 统一适配层；§9.1 未配置/失败确定性降级）。

- 前置依赖（spec-04 §2.5 B3）：当日结算 done 才允许生成正常日报叙述段。
- 上下文只取已落库确定性事实（摘要/持仓/成交/降级标注/活跃版本配置/上日叙述），
  不引外部行情资讯；市场观察段明确标"主观"，禁止虚构当日具体行情数据。
- provider 未配置/调用失败：不阻塞日报，narrative 保持空并落审计
   （report.narrative_generate result=failed/not_configured），可稍后重跑。
- AutoNarrativeSweep：spec-04 §5.3 18:31 正常日报叙述段自动任务（调度器 tick 收敛）——
  只收主账户结算 done 的 normal 日报；未配置模型服务聚合留痕不刷审计，配置就绪自动
  续跑；瞬时失败指数退避重试，耗尽后聚合留痕可 UI/force 重跑。
"""
from __future__ import annotations

import json
import time as _tm
from datetime import date as _date
from datetime import datetime as _datetime
from datetime import time as _time
from datetime import timedelta as _td

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


_NARR_START = _time(18, 31)
_NARR_LOOKBACK_DAYS = 3
_NARR_MAX_ATTEMPTS = 3
_NARR_BASE_BACKOFF_S = 300.0


class AutoNarrativeSweep:
    """spec-04 §5.3 18:31 叙述段自动任务（调度器每分钟 tick 内收敛，幂等）。

    - 时点：北京时间 >= start_at（默认 18:31）才动作；早于时点零动作零网络。
    - 候选：role='main' 账户、lookback 日历窗内（含引擎 catchup 补结算的近期日）、
      结算 done 的 normal 日报最新版且叙述为空。trial/验证窗账户不入列，不给回放
      账户烧 LLM token。
    - provider 未配置：不调用 chat、不刷逐条审计；同一 trade_date 聚合留 1 条
      skipped 审计，随后每 tick 廉价重检，配置就绪自动续跑（晚间晚配置也能补上）。
    - 瞬时失败（llm_failed/empty_output）：指数退避重试至多 max_attempts 次；耗尽后
      聚合留痕并停止（当日叙述保持为空，日报不受影响，可经 UI/force 重跑）。
    - 进程重启即清零退避表：已落库叙述被候选过滤自然排除，不重写；未配置日的审计
      聚合同样由 set 去重，不重复刷屏。
    """

    def __init__(self, state, *, start_at: _time = _NARR_START,
                 lookback_days: int = _NARR_LOOKBACK_DAYS,
                 max_attempts: int = _NARR_MAX_ATTEMPTS,
                 base_backoff_s: float = _NARR_BASE_BACKOFF_S,
                 mono=None):
        self.state = state
        self.start_at = start_at
        self.lookback_days = max(1, int(lookback_days))
        self.max_attempts = max(1, int(max_attempts))
        self.base_backoff_s = max(0.0, float(base_backoff_s))
        self._mono = mono or _tm.monotonic
        self._st: dict[tuple[str, str], dict] = {}
        self._noprov_audited: set[str] = set()

    def _candidate_dates(self, today: _date) -> list[str]:
        return [(today - _td(days=k)).isoformat()
                for k in range(self.lookback_days)]

    def _pending(self, dates: list[str]) -> list[dict]:
        """主账户当日结算 done 且叙述为空的 normal 日报最新版（确定性候选）。"""
        ph = ",".join("?" * len(dates))
        rows = state_conn(self.state).execute(
            f"""
            SELECT dr.agent_id, dr.trade_date, dr.data_section
              FROM daily_reports dr
              JOIN accounts a ON a.id = dr.agent_id AND a.role = 'main'
             WHERE dr.trade_date IN ({ph})
               AND dr.status = 'normal'
               AND (dr.narrative IS NULL OR trim(dr.narrative) = '')
               AND NOT EXISTS (
                    SELECT 1 FROM daily_reports dr2
                     WHERE dr2.agent_id = dr.agent_id
                       AND dr2.trade_date = dr.trade_date
                       AND dr2.version > dr.version)
             ORDER BY dr.trade_date, dr.agent_id
            """,
            dates,
        ).fetchall()
        out = []
        for r in rows:
            try:
                ds = json.loads(r["data_section"] or "{}")
            except ValueError:
                continue
            if not (ds.get("settlement") or {}).get("done"):
                continue
            out.append({"account_id": r["agent_id"],
                        "trade_date": r["trade_date"]})
        return out

    def _write_audit(self, action: str, result: str, detail: str) -> None:
        from datetime import timezone
        ts = _datetime.now(timezone.utc).isoformat(timespec="seconds")
        with write_txn(state_conn(self.state)) as tx:
            tx.execute(
                "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
                " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
                (ts, "scheduler", action, "daily_reports", "",
                 result, json.dumps({"detail": detail}, ensure_ascii=False), ""),
            )

    def sweep(self, now: _datetime) -> dict:
        """单次收敛（调度器每 tick 调用；返回轻量状态机结果）。"""
        today = now.date()
        stats = {"phase": "narrative_auto", "date": today.isoformat(),
                 "candidates": 0, "generated": 0, "failed": 0, "retrying": 0,
                 "noprov": 0}
        if now.time() < self.start_at:
            stats["status"] = "outside_window"
            stats["start_at"] = self.start_at.isoformat()
            return stats
        dates = self._candidate_dates(today)
        pending = self._pending(dates)
        stats["candidates"] = len(pending)
        configured = llm.provider_configured(self.state)
        noprov_cnt: dict[str, int] = {}
        for cand in pending:
            key = (cand["account_id"], cand["trade_date"])
            rec = self._st.get(key) or {"attempts": 0, "next": 0.0}
            if rec.get("final"):
                continue
            if self._mono() < rec.get("next", 0.0):
                stats["retrying"] += 1
                continue
            if not configured:
                rec.update(kind="noprov", final=None)
                self._st[key] = rec
                noprov_cnt[cand["trade_date"]] = noprov_cnt.get(
                    cand["trade_date"], 0) + 1
                stats["noprov"] += 1
                continue
            res = generate_narrative(self.state, cand["account_id"],
                                     cand["trade_date"])
            code = res.get("code", "")
            if code in ("generated", "skipped", "no_report", "not_settled"):
                self._st[key] = {"attempts": 0, "next": 0.0, "final": code}
                if code == "generated":
                    stats["generated"] += 1
                continue
            if code == "not_configured":
                self._st[key] = {"attempts": 0, "next": 0.0, "kind": "noprov"}
                noprov_cnt[cand["trade_date"]] = noprov_cnt.get(
                    cand["trade_date"], 0) + 1
                stats["noprov"] += 1
                continue
            attempts = rec.get("attempts", 0) + 1
            if attempts >= self.max_attempts:
                self._st[key] = {"attempts": attempts, "next": 0.0,
                                 "final": code}
                stats["failed"] += 1
                self._write_audit(
                    "report.narrative_auto", "failed",
                    f"{cand['account_id']}@{cand['trade_date']} 18:31 自动叙述"
                    f"重试 {attempts} 次仍 {code}，当日叙述保持为空"
                    f"（可经 UI/force 重跑）")
            else:
                self._st[key] = {"attempts": attempts, "kind": "retry",
                                 "next": self._mono()
                                 + self.base_backoff_s * attempts}
                stats["retrying"] += 1
        for d, n in sorted(noprov_cnt.items()):
            if d in self._noprov_audited:
                continue
            self._noprov_audited.add(d)
            self._write_audit(
                "report.narrative_auto", "skipped",
                f"{d} {n} 份主账户日报待叙述，未配置模型服务已跳过"
                f"（spec-04 §9.1：配置后可自动补跑，不伪造叙述）")
        stats["status"] = "ok"
        return stats

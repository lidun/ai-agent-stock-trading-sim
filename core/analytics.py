"""策略分析聚合（spec-06 §6.4 P2 数据源：资金曲线/指标摘要卡/策略演进）。

数据口径（复用既有落库态，不新建表、不改引擎）：
- 净值序列：daily_reports 每 (account_id=日报 agent_id, trade_date) 最新版本的
  data_section.summary.nav——与 reporting.build_engine_data_section 同源，份额法
  NAV（spec-01 §6.1，shares 固定 10 万 → nav−1 即累计收益率口径）。trial 账户同样
  有自己的日报（试运行回放逐日结算），故各角色账户均可独立出曲线。
- 沪深300 基准：index_quotes 表本版未建（spec-03 §2），经 quotes_tencent.day_rows
  现拉 sh000300（引擎 EXIT_BENCH_SYMBOL 同码），按各曲线交易日期对齐归一为
  累计收益 %。源不可用 → benchmark.available=false，曲线降级为仅净值。
- 信号胜率：signal_registry 本版未建（spec-01 §8 注释明示），真实承载=exit_trackings
  （卖出成交自动登记、settle_exits 推进/结清给出 fwd/excess/conclusion，spec-01 §8.1），
  以"卖出决策"维度聚合替代，UI 口径标注。累计收益/最大回撤全程由主账户净值算。
- 策略演进：账户角色账本（main 现役 / trial·validation 验证）+ trial_archives 验收
  结论（launch/reject 快照，accountstore.finish_trial 一次写入）——spec-02 §9 多代
  版本链引擎侧尚未落地，UI 如实展示已有账户维度。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Callable

from core.db import state_conn

EXIT_BENCH_SYMBOL = "sh000300"  # 与 eodengine.EXIT_BENCH_SYMBOL 同码
_ROLE_LABEL = {"main": "现役主账户", "trial": "试运行验证", "validation": "独立验证"}


def _now_ts_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _to_f(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _round2(v: float | None) -> float | None:
    return round(v, 2) if v is not None else None


def _nav_series(state, account_id: str, start: str = "", end: str = "") -> list[dict]:
    """账户净值日序（daily_reports 每日最新版本 summary.nav，升序）。"""
    c = state_conn(state)
    sql = ("SELECT trade_date, version FROM daily_reports"
           " WHERE agent_id=? AND trade_date BETWEEN ? AND ?"
           " ORDER BY trade_date DESC, version DESC")
    rows = c.execute(sql, (account_id, start or "0000-01-01", end or "9999-12-31")
                     ).fetchall()
    best: dict[str, int] = {}
    for r in rows:
        best.setdefault(r["trade_date"], r["version"])
    out: list[dict] = []
    for d in sorted(best):
        row = c.execute(
            "SELECT data_section FROM daily_reports WHERE agent_id=? AND trade_date=?"
            " AND version=? LIMIT 1", (account_id, d, best[d])).fetchone()
        if row is None:
            continue
        try:
            ds = json.loads(row["data_section"])
        except ValueError:
            continue
        nav = _to_f((ds.get("summary") or {}).get("nav"))
        if nav is None or nav <= 0:
            continue
        out.append({"trade_date": d, "nav": round(nav, 6),
                    "return_pct": round((nav - 1.0) * 100, 2)})
    return out


def _latest_report_date(state, account_id: str) -> str:
    c = state_conn(state)
    row = c.execute("SELECT MAX(trade_date) AS d FROM daily_reports WHERE agent_id=?",
                    (account_id,)).fetchone()
    return row["d"] or ""


def _cut_range(points: list[dict], span: str, latest: str = "") -> list[dict]:
    """区间裁剪：1m/3m/all（按各序列自身最后日期，或显式 latest）。"""
    if not points:
        return []
    if span in ("1m", "3m"):
        base = latest or points[-1]["trade_date"]
        months = 1 if span == "1m" else 3
        d = date.fromisoformat(base)
        month = d.month - months
        year = d.year
        while month <= 0:
            month += 12
            year -= 1
        try:
            start = date(year, month, d.day).isoformat()
        except ValueError:
            start = date(year, month, 1).isoformat()
        return [p for p in points if p["trade_date"] >= start]
    return points


def _bench_series(state, dates: list[str],
                  fetch: Callable[[str, str, str], list[dict]] | None = None) -> dict:
    """沪深300 收盘序列（spec-03 §2 index_quotes 本版未建 → 现拉 sh000300）。

    拉取窗口=账户日序区间前探 7 天，按账户日序前向填充（缺行情交易日/停牌不入点），
    基准收益以首个账户日 close 为锚归一（与净值曲线同起点）。源缺口显式降级不阻断。"""
    ordered = sorted(set(d for d in dates if d))
    if not ordered:
        return {"available": False, "reason": "无交易区间", "points": []}
    if fetch is None:
        try:
            from core import quotes_tencent as qt  # noqa: PLC0415
            fetch = qt.day_rows
        except Exception:  # noqa: BLE001
            return {"available": False, "reason": "行情源不可用", "points": []}
    lo = date.fromisoformat(ordered[0])
    start = (lo - timedelta(days=7)).isoformat()
    try:
        rows = fetch(EXIT_BENCH_SYMBOL, start, ordered[-1])
    except Exception as exc:  # noqa: BLE001 网络/源缺口 → 降级不阻断曲线
        return {"available": False, "reason": f"基准拉取失败：{exc}", "points": []}
    closes: dict[str, float] = {}
    for r in rows:
        v = _to_f(r["close"])
        if v and v > 0:
            closes[str(r["date"])] = v
    aligned: list[tuple[str, float]] = []
    last: float | None = None
    for d in ordered:
        if d in closes:
            last = closes[d]
        if last is not None:
            aligned.append((d, last))
    if not aligned:
        return {"available": False, "reason": "基准区间无行情", "points": []}
    base = aligned[0][1]
    points = [{"trade_date": d, "close": round(c, 4),
               "return_pct": round((c / base - 1.0) * 100, 2)}
              for d, c in aligned]
    return {"available": True, "reason": "", "points": points}


def equity_curve(state, agent_id: str, span: str = "all") -> dict:
    """各角色账户净值曲线 + 沪深300 基准（spec-06 §6.4 B4）。"""
    from core.accountstore import accounts_for_agent  # noqa: PLC0415
    accounts = accounts_for_agent(state, agent_id)
    if not accounts:
        raise LookupError(f"Agent {agent_id} 不存在或无账户")
    series: list[dict] = []
    all_dates: list[str] = []
    for acct in accounts:
        full = _nav_series(state, acct["id"])
        pts = _cut_range(full, span)
        role = acct["role"]
        label = _ROLE_LABEL.get(role, role)
        if acct["status"] == "archived":
            label += "（已归档）"
        series.append({
            "account_id": acct["id"],
            "role": role,
            "label": label,
            "active_version_no": acct["active_version_no"],
            "created_ts": acct["created_ts"],
            "status": acct["status"],
            "points": pts,
            "last_return_pct": _round2(pts[-1]["return_pct"]) if pts else None,
        })
        all_dates += [p["trade_date"] for p in pts]
    bench = _bench_series(state, sorted(set(all_dates)))
    return {"agent_id": agent_id, "range": span, "series": series, "benchmark": bench}


def _drawdown(navs: list[float]) -> float | None:
    """全历史逐日最大回撤 %（峰值→谷值，pec-06 §6.4 指标卡）。"""
    if len(navs) < 2:
        return None
    peak = navs[0]
    mdd = 0.0
    for n in navs:
        if n > peak:
            peak = n
        elif peak > 0:
            dd = (peak - n) / peak * 100
            if dd > mdd:
                mdd = dd
    return round(mdd, 2) if mdd > 0 else 0.0


def metrics(state, agent_id: str) -> dict:
    """指标摘要卡：累计收益率/最大回撤（主账户净值）/卖出信号胜率（exit_trackings）。"""
    from core.accountstore import accounts_for_agent  # noqa: PLC0415
    accounts = accounts_for_agent(state, agent_id)
    if not accounts:
        raise LookupError(f"Agent {agent_id} 不存在或无账户")
    main = next((a for a in accounts if a["role"] == "main"), None)
    pts: list[dict] = []
    as_of = ""
    if main:
        pts = _nav_series(state, main["id"])
        as_of = _latest_report_date(state, main["id"])
    navs = [p["nav"] for p in pts]
    cum = None
    if navs:
        cum = round((navs[-1] - 1.0) * 100, 2)
    signal: dict = {
        "n": 0, "done": 0, "win_n": 0, "tie_n": 0, "early_n": 0,
        "win_rate_pct": None, "avg_fwd_return_pct": None, "avg_excess_pct": None,
        "note": "口径：exit_trackings 卖出决策客观统计（signal_registry 本版未建，"
                "spec-01 §8.1）——卖对/卖平/卖早结论由 N 日前瞻收益对沪深300超额判定",
    }
    if main:
        c = state_conn(state)
        rows = c.execute(
            "SELECT conclusion, fwd_return_pct, excess_pct, status FROM exit_trackings"
            " WHERE account_id=? AND status='done'", (main["id"],)).fetchall()
        signal["n"] = len(rows)
        done = [r for r in rows if r["conclusion"]]
        signal["done"] = len(done)
        counts = {"卖对": 0, "卖平": 0, "卖早": 0}
        for r in done:
            counts[r["conclusion"]] = counts.get(r["conclusion"], 0) + 1
        signal["win_n"] = counts["卖对"]
        signal["tie_n"] = counts["卖平"]
        signal["early_n"] = counts["卖早"]
        decided = counts["卖对"] + counts["卖早"]
        if decided > 0:
            signal["win_rate_pct"] = round(counts["卖对"] / decided * 100, 2)
        if done:
            signal["avg_fwd_return_pct"] = round(
                sum(r["fwd_return_pct"] for r in done) / len(done), 2)
            signal["avg_excess_pct"] = round(
                sum(r["excess_pct"] for r in done) / len(done), 2)
    return {
        "agent_id": agent_id,
        "as_of": as_of,
        "cum_return_pct": _round2(cum),
        "max_drawdown_pct": _drawdown(navs),
        "settle_days": len(pts),
        "nav_last": round(navs[-1], 6) if navs else None,
        "signal": signal,
    }


def evolution(state, agent_id: str) -> dict:
    """策略演进账本：账户角色账本 + 试运行验收结论（spec-05 §6.2 快照）。"""
    from core.accountstore import accounts_for_agent  # noqa: PLC0415
    accounts = accounts_for_agent(state, agent_id)
    if not accounts:
        raise LookupError(f"Agent {agent_id} 不存在或无账户")
    c = state_conn(state)
    replay = c.execute(
        "SELECT window_days, status, trial_account_id FROM trial_replays"
        " WHERE agent_id=?", (agent_id,)).fetchone()
    archive_row = c.execute(
        "SELECT decision, verdict, account_id, snapshot, archived_ts"
        " FROM trial_archives WHERE agent_id=? ORDER BY archived_ts DESC LIMIT 1",
        (agent_id,)).fetchone()
    ledgers: list[dict] = []
    for acct in accounts:
        pts = _nav_series(state, acct["id"])
        trial = None
        if replay and replay["trial_account_id"] == acct["id"]:
            trial = {"window_days": replay["window_days"],
                     "replay_status": replay["status"]}
        role = acct["role"]
        label = _ROLE_LABEL.get(role, role)
        if acct["status"] == "archived":
            label += "（已归档留证）"
        ledgers.append({
            "account_id": acct["id"],
            "role": role,
            "label": label,
            "status": acct["status"],
            "active_version_no": acct["active_version_no"],
            "created_ts": acct["created_ts"],
            "report_days": len(pts),
            "first_report_date": pts[0]["trade_date"] if pts else "",
            "last_report_date": pts[-1]["trade_date"] if pts else "",
            "first_nav": pts[0]["nav"] if pts else None,
            "nav_last": pts[-1]["nav"] if pts else None,
            "return_pct": _round2(pts[-1]["return_pct"]) if pts else None,
            "trial": trial,
        })
    archive = None
    if archive_row:
        snap = {}
        try:
            snap = json.loads(archive_row["snapshot"] or "{}")
        except ValueError:
            snap = {}
        acct = (snap.get("account") or {})
        repl = (snap.get("replay") or {})
        counts = (snap.get("counts") or {})
        archive = {
            "account_id": archive_row["account_id"],
            "decision": archive_row["decision"],
            "verdict": archive_row["verdict"],
            "archived_ts": archive_row["archived_ts"],
            "end_nav": _to_f(acct.get("nav")),
            "end_total_pnl": _to_f(acct.get("total_pnl")),
            "replay_window_days": repl.get("window_days"),
            "replay_sessions": repl.get("sessions"),
            "settle_days": counts.get("settle_days"),
            "orders": counts.get("orders"),
            "trades": counts.get("trades"),
            "holdings": counts.get("holdings"),
        }
    return {
        "agent_id": agent_id,
        "note": "spec-02 §9 多代版本链引擎侧未落地；此处按账户角色账本呈现——"
                "main=现役、trial/validation=验证期独立业绩（spec-05 §4.2/§6.2）。",
        "ledgers": ledgers,
        "archive": archive,
        "generated_ts": _now_ts_iso(),
    }

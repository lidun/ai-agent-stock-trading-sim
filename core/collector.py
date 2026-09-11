"""盘中 3 秒采集器（spec-03 §3，L0 供给）。

- 采集范围：每日采集清单（当日有效条件单 ∪ 持仓 ∪ 观察集合，§3.1）——首次 9:25 后
  计算、13:00 前重读一次（午间子 Agent 可能新增条件单）；清单落 collector_watchlist。
- 频率固定 3 秒批量快照（单批覆盖全清单）；失败单次跳过、连续失败 ≥3 次指数退避
  （5/10/30/60s 封顶），恢复后回 3 秒（§3.3）。
- 单源同日不变式（§3.3）：当日绑定源记录于 source_binding，采集器整日只退避不切源
  （换源次日生效）；批量粒度绑定=当日主源全票同源。
- 午休（11:30-13:00）不采；15:00-15:02 延续采样落 is_extended=1（§3.2/§4.1 仅作
  close_candidates，不进判定序列）；盘中 status=suspended 样本剔除（不落判定序列，
  仅审计），停牌秒数从覆盖率分母剔除（§3.4 A5）。
- 覆盖率：分母=该票当日实际可交易秒数/3；中途启动分母按全天应有、分子按实际采集
  （禁止“启动过就算完整”，§4.1）；15:02:30 后按票收敛 coverage 与降级标记。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal
from time import monotonic

from core import l0store, source_chain, source_health
from core.db import state_conn

log = logging.getLogger(__name__)

_BJT = timezone(timedelta(hours=8))
MORNING_START = dtime(9, 30)
MORNING_END = dtime(11, 30)
AFTERNOON_START = dtime(13, 0)
AFTERNOON_END = dtime(15, 0)
EXTENDED_END = dtime(15, 2, 30)
WATCHLIST_AM = dtime(9, 25)

BACKOFF_STEPS = (5, 10, 30, 60)
COVERAGE_MIN = Decimal("0.90")


def bjt_now() -> datetime:
    return datetime.now(_BJT).replace(tzinfo=None)


def session_of(now: datetime) -> str:
    """返回当前采集会话档位：preopen|morning|lunch|afternoon|extended|closed。"""
    t = now.time()
    if t < WATCHLIST_AM:
        return "preopen"
    if MORNING_START <= t < MORNING_END:
        return "morning"
    if MORNING_END <= t < AFTERNOON_START:
        return "lunch"
    if AFTERNOON_START <= t < AFTERNOON_END:
        return "afternoon"
    if AFTERNOON_END <= t < EXTENDED_END:
        return "extended"
    return "closed"


def _reason_rows(rows_by_tag: dict[str, set[str]]) -> list[dict]:
    tags = sorted(rows_by_tag)
    by_symbol: dict[str, list[str]] = {}
    for tag in tags:
        for sym in rows_by_tag[tag]:
            by_symbol.setdefault(sym, []).append(tag)
    return [{"symbol": sym, "reason": ",".join(sorted(tags_))}
            for sym, tags_ in sorted(by_symbol.items())]


def derive_watchlist(state, trade_date: str) -> list[dict]:
    """计算当日采集清单（spec-03 §3.1：当日有效条件单 ∪ 持仓 ∪ 观察集合）。

    有效条件单口径与结算判定一致（当日新建 active 单 + 跨日 resting 的 long/until 单，
    spec-01 §3.3）；试运行/退役 Agent 账户冻结不纳入采集（与结算冻结口径一致）。
    """
    conn = state_conn(state)
    rows = {"order": set(), "holding": set()}
    for r in conn.execute(
        """
        SELECT DISTINCT co.symbol FROM condition_orders co
         JOIN accounts a ON a.id = co.account_id
         JOIN agents ag ON ag.id = a.agent_id
         WHERE a.granularity='eod_replay'
           AND a.status NOT IN ('halted','archived','paused_buy')
           AND ag.status NOT IN ('trial','archived')
           AND co.status='active'
           AND (co.created_at LIKE ?
                OR (co.created_at < ? AND co.validity IN ('long','until')))
        """,
        (trade_date + "%", trade_date),
    ).fetchall():
        rows["order"].add(r["symbol"])
    for r in conn.execute(
        """
        SELECT DISTINCT h.symbol FROM holdings h
         JOIN accounts a ON a.id = h.account_id
         JOIN agents ag ON ag.id = a.agent_id
         WHERE a.granularity='eod_replay'
           AND a.status NOT IN ('halted','archived','paused_buy')
           AND ag.status NOT IN ('trial','archived')
           AND h.quantity > 0
        """,
    ).fetchall():
        rows["holding"].add(r["symbol"])
    rows["observation"] = {o["symbol"] for o in l0store.observation_list(state)}
    return _reason_rows(rows)


def _sample(symbol: str, payload: dict, now: datetime, trade_date: str,
            source: str, is_extended: bool) -> dict:
    """快照 → L0 样本行（status=suspended 语义：无有效价即停牌/无报价中间态）。"""
    price = Decimal(str(payload.get("price") or "0"))
    prev_close = Decimal(str(payload.get("prev_close") or "0"))
    name = str(payload.get("name") or "")
    suspended = price <= 0 or "停牌" in name
    pct = Decimal(0)
    if not suspended and prev_close > 0:
        pct = (price - prev_close) * 100 / prev_close
    return {
        "symbol": symbol, "trade_date": trade_date,
        "ts": now.isoformat(timespec="seconds"),
        "price": price, "prev_close": prev_close, "pct_chg": pct,
        "cum_turnover": payload.get("cum_turnover") or "0",
        "status": "suspended" if suspended else "normal",
        "is_extended": 1 if (is_extended and not suspended) else 0,
        "source": source,
    }


class MarketCollector:
    """3 秒批量采集器（单进程常驻；测试/手动按 cycle(now) 驱动，run_forever 挂 app）。"""

    def __init__(self, state, *, configured: list[str] | None = None,
                 providers: dict | None = None,
                 fail_to_quarantine: int = 3,
                 coverage_min: Decimal = COVERAGE_MIN):
        self.state = state
        self.configured = list(configured) if configured is not None else (
            source_chain.configured_sources(
                getattr(state.settings, "market_sources", "") or "tencent"))
        self.role_configured = [c for c in self.configured if "l0" in source_chain.roles_of(c)]
        self.providers = dict(providers or {})
        self.coverage_min = coverage_min
        self.router = source_chain.SourceRouter(self.role_configured, "l0",
                                                fail_to_quarantine=fail_to_quarantine)
        self._day: date | None = None
        self._am_done = False
        self._pm_done = False
        self._finalized = False
        self._fails = 0
        self._backoff_until = 0.0
        self._last_outcome: dict = {}
        self.running = False

    # ------------------------------------------------------------ 状态与供给

    def _provider(self, name: str):
        if name in self.providers:
            return self.providers[name]
        return source_chain.adapter_for(name)

    def status(self) -> dict:
        return {
            "configured": list(self.configured),
            "l0_candidates": list(self.role_configured),
            "running": self.running,
            "day": self._day.isoformat() if self._day else None,
            "session": session_of(bjt_now()),
            "am_watchlist_done": self._am_done,
            "pm_watchlist_done": self._pm_done,
            "finalized": self._finalized,
            "consecutive_fails": self._fails,
            "backoff": bool(self._backoff_until > monotonic()),
            "last": self._last_outcome,
        }

    # ------------------------------------------------------------ 采集主循环

    def cycle(self, now: datetime | None = None) -> dict:
        """单个 3 秒采集周期（幂等、可并发安全地重入；交易日会话外零网络）。"""
        now = now or bjt_now()
        day = now.date()
        if day != self._day:
            self._reset_day(day)
        sess = session_of(now)
        self._maybe_watchlist(now)
        if sess in ("preopen", "lunch", "closed"):
            self._maybe_finalize(now)
            return {"status": "idle", "date": day.isoformat(), "session": sess}
        if monotonic() < self._backoff_until:
            return {"status": "backoff", "date": day.isoformat(), "session": sess,
                    "wait_s": round(self._backoff_until - monotonic(), 1)}
        watchlist = l0store.watchlist_for(self.state, day.isoformat())
        if not watchlist:
            return {"status": "empty_watchlist", "date": day.isoformat(),
                    "session": sess}
        symbols = [w["symbol"] for w in watchlist]
        trade_date = day.isoformat()
        source = self._resolve_source(trade_date)
        if source is None:
            return {"status": "no_source", "date": trade_date, "session": sess,
                    "configured": self.role_configured}
        try:
            t0 = monotonic()
            snap = self._provider(source).realtime_batch(symbols)
        except Exception as exc:  # noqa: BLE001 源级失败 → 退避重试（当日不切源）
            self._record_health(source, False, 0.0, trade_date)
            return self._on_source_failure(trade_date, sess, source, exc)
        rows = [self._sample_row(s, snap.get(s), now, trade_date, source, sess)
                for s in symbols]
        rows = [r for r in rows if r is not None]
        if rows:
            l0store.upsert_l0_ticks(self.state, rows)
            self._record_source(trade_date, source, snap)
        self._fails = 0
        self._backoff_until = 0.0
        self.router.record_ok(source)
        self._record_health(source, True, (monotonic() - t0) * 1000.0, trade_date)
        self._maybe_finalize(now)
        outcome = {"status": "collected", "date": trade_date, "session": sess,
                   "source": source, "samples": len(rows),
                   "symbols": sorted({r["symbol"] for r in rows}),
                   "ts": now.isoformat(timespec="seconds")}
        self._last_outcome = outcome
        return outcome

    def _sample_row(self, symbol, payload, now, trade_date, source, sess):
        if not payload:
            return None
        return _sample(symbol, payload, now, trade_date, source,
                       is_extended=(sess == "extended"))

    def _reset_day(self, day: date) -> None:
        self._day = day
        self._am_done = False
        self._pm_done = False
        self._finalized = False
        self._fails = 0
        self._backoff_until = 0.0
        self.router.note_day(day)

    def _maybe_watchlist(self, now: datetime) -> None:
        if self._am_done and self._pm_done:
            return
        trade_date = now.date().isoformat()
        if not self._am_done:
            if now.time() >= WATCHLIST_AM:
                l0store.replace_watchlist(
                    self.state, trade_date, derive_watchlist(self.state, trade_date))
                self._am_done = True
            return
        if not self._pm_done and now.time() >= AFTERNOON_START:
            l0store.replace_watchlist(
                self.state, trade_date, derive_watchlist(self.state, trade_date))
            self._pm_done = True

    def _resolve_source(self, trade_date: str) -> str | None:
        bound = l0store.day_binding_sources(self.state, trade_date)
        if bound:
            chosen = bound[0]
            if chosen not in self.role_configured:
                log.warning("当日绑定源 %s 不在已配置候选，仍按绑定续采（不切源）", chosen)
            return chosen
        return self.router.pick()

    def _record_source(self, trade_date: str, source: str, snap: dict) -> None:
        for sym in snap:
            l0store.set_source_binding(self.state, sym, trade_date, source)

    def _record_health(self, source: str, ok: bool, latency_ms: float,
                       trade_date: str) -> None:
        """记录源健康度并评估切换（§9）；失败不影响采集主链路。"""
        try:
            now = bjt_now()
            source_health.record_call(
                self.state, source=source, ok=ok, latency_ms=latency_ms,
                source_family=source_chain.family_of(source), kind="collect",
                trade_date=trade_date, now=now)
            source_health.evaluate_switch(
                self.state, source=source, kind="collect",
                trade_date=trade_date, now=now)
        except Exception:  # noqa: BLE001
            log.exception("源健康度记录失败（不影响采集）")

    def _on_source_failure(self, trade_date, sess, source, exc) -> dict:
        self._fails += 1
        if self._fails >= 3:
            step = BACKOFF_STEPS[min(self._fails - 3, len(BACKOFF_STEPS) - 1)]
            self._backoff_until = monotonic() + step
            log.warning("采集源 %s 连续失败 %s 次，退避 %ss：%s",
                        source, self._fails, step, exc)
        self._last_outcome = {"status": "source_error", "date": trade_date,
                              "session": sess, "source": source,
                              "consecutive_fails": self._fails,
                              "error": str(exc)[:500]}
        return self._last_outcome

    def _maybe_finalize(self, now: datetime) -> None:
        if self._finalized or now.time() < EXTENDED_END:
            return
        day = now.date().isoformat()
        rows = l0store.watchlist_for(self.state, day)
        if not rows:
            self._finalized = True
            return
        for w in rows:
            self._finalize_symbol(w["symbol"], day)
        self._finalized = True

    def _finalize_symbol(self, symbol: str, trade_date: str) -> None:
        stats = l0store.refresh_coverage(self.state, symbol, trade_date)
        normal = stats["actual"]
        rate = stats["coverage_rate"]
        quality = "degraded"
        reason = "coverage_gap"
        notes = f"有效样本 {normal}/{stats['expected']}"
        if normal == 0:
            reason = "source_fault" if self._fails else "coverage_gap"
            notes = "当日无有效样本（源故障或全时段不可采）"
        elif rate >= self.coverage_min:
            quality = "ok"
            reason = ""
            notes = f"覆盖率 {format(rate, '.4f')}"
        l0store.update_coverage(
            self.state, symbol, trade_date, quality=quality,
            degraded_reason=reason, notes=notes)

    # ------------------------------------------------------------ 常驻循环

    async def run_forever(self, interval_s: int = 3) -> None:
        self.running = True
        try:
            while True:
                try:
                    self.cycle()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 采集器单周期异常不中断常驻
                    log.exception("采集周期异常（下个周期重试）")
                await asyncio.sleep(max(1, interval_s))
        finally:
            self.running = False

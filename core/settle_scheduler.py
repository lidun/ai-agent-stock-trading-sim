"""EOD 结算自动触发（spec-04 §2.2 第 2 项最小实现，P1）。

调度器只判定“该不该触发”（北京时间窗口、当日是否实盘会话、有无待结算工作、
数据是否就绪）；结算逻辑本体在 settle_day/eodengine（幂等 settle_key，
spec-01 §3.8，重复触发零重复）。

- 交易日探测：腾讯快照 ts 日期 == 当前北京时间日期——周末/节假日/当日未开盘
  自动跳过，无需本地日历表；探测结果短时缓存，避免每分钟网络风暴。
- 空日拦截：进入行情探测前先查库（settle_day.pending_any），当日无任何待结算
  工作即直接跳过，零网络。
- 数据缺口（行情/引擎 gap 以账户级 error 上报）留在窗口内每分钟重试；超窗后
  不再触发（结算未完成按 account 记入日志/审计留痕，不虚构收盘价）。
- `_done_dates` 仅为进程内节流护栏：某交易日结算全部落定后当日不再重复探测；
  跨进程/崩溃恢复的幂等由 DB settle_key 兜底（spec-01 §3.8 三层幂等之一）。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from time import monotonic

from core import accountstore, eodengine, quotes_tencent, reporting, settle_day
from core.auth import audit
from core.db import state_conn

log = logging.getLogger(__name__)

DEFAULT_FEED = quotes_tencent
PROBE_TTL_S = 300
_TRIAL_LOOKBACK_DAYS = 180
_BJT = timezone(timedelta(hours=8))


def bjt_now() -> datetime:
    return datetime.now(_BJT).replace(tzinfo=None)


def _snap_date(feed) -> date | None:
    snap = feed.realtime_batch(["600000"])
    ts = (snap.get("600000") or {}).get("ts", "")
    if len(ts) >= 8 and ts[:8].isdigit():
        return date(int(ts[:4]), int(ts[4:6]), int(ts[6:8]))
    return None


class EodSettleTrigger:
    """按 spec-01 §3.1 窗口触发当日 EOD 结算；settle_once 可同步调用（供测试）。"""

    def __init__(self, state, *, feed=DEFAULT_FEED,
                 earliest: time = time(15, 35),
                 retry_until: time = time(16, 35),
                 probe_ttl_s: int = PROBE_TTL_S,
                 corp_events: dict[str, dict] | None = None,
                 corp_provider=None):
        self.state = state
        self.feed = feed
        self.earliest = earliest
        self.retry_until = retry_until
        self.probe_ttl_s = probe_ttl_s
        self.corp_events = corp_events or {}
        self.corp_provider = corp_provider
        self._done_dates: set[str] = set()
        self._gap_accounts: dict[str, list[dict] | None] = {}
        self._probe_cache: dict = {"at": 0.0, "day": None}

    def _corp_events_for(self, trade_date: str) -> dict:
        """按交易日解析当日公司行动/退市事件（spec-01 §6.6 数据/参考侧按 ex_date 供给）。

        corp_provider(trade_date) -> dict（同 eodengine.settle_account corp_events 结构，
        供单日会话与跨日快进/回放统一注入，保证 delist 等事件落在正确 ex_date 而非被
        截断）；未配置 provider 时回退静态 corp_events（单日运行用）。provider 拉取异常
        → 记录告警审计并按当日无公司行动处理（当日数据缺口以审计留痕，下轮由 settle_key
        幂等补齐，不虚构事件）。
        """
        if self.corp_provider is None:
            return self.corp_events
        try:
            ev = self.corp_provider(trade_date) or {}
        except Exception:  # noqa: BLE001
            log.exception("公司行动/退市事件拉取失败 %s（按当日无公司行动处理）", trade_date)
            self._audit("trade.corp_provider_gap", "error",
                        f"{trade_date} 公司行动事件源异常 → 当日按无事件结算，待数据侧补齐")
            return {}
        return ev

    def _probe_trade_date(self) -> date | None:
        now = monotonic()
        if now - self._probe_cache["at"] >= self.probe_ttl_s:
            try:
                self._probe_cache = {"at": now, "day": _snap_date(self.feed)}
            except Exception:  # noqa: BLE001
                log.exception("行情快照交易日探测失败")
                self._probe_cache = {"at": now, "day": None}
        return self._probe_cache["day"]

    def _audit(self, action: str, result: str, detail: str) -> None:
        try:
            audit(self.state, "scheduler", action, result=result,
                  object_type="settle_day", object_id="", detail=detail)
        except Exception:  # noqa: BLE001
            log.exception("结算触发审计写入失败")

    def _tracking_state(self, trade_date: str) -> tuple[list[str], list[str]]:
        """当日有到期/推进义务的跟踪：返回 (accounts, symbols)。"""
        from core.db import state_conn  # noqa: PLC0415
        conn = state_conn(self.state)
        accounts = [r["account_id"] for r in conn.execute(
            "SELECT DISTINCT account_id FROM exit_trackings"
            " WHERE status='tracking' AND sell_date < ?", (trade_date,)
        ).fetchall()]
        symbols = [r["symbol"] for r in conn.execute(
            "SELECT DISTINCT symbol FROM exit_trackings"
            " WHERE status='tracking' AND sell_date < ?", (trade_date,)
        ).fetchall()]
        return accounts, symbols

    def _day_ohlc(self, symbol: str, trade_date: str) -> dict | None:
        """当日官方日线 OHLC（无当日行/源缺口 → None，缺价不虚构，由引擎 stale 语义处理）。"""
        from datetime import timedelta  # noqa: PLC0415
        start = (date.fromisoformat(trade_date) - timedelta(days=12)).isoformat()
        try:
            rows = self.feed.day_rows(symbol, start, trade_date)
        except Exception:  # noqa: BLE001
            log.warning("跟踪行情拉取失败 %s %s", symbol, trade_date, exc_info=True)
            return None
        on = [r for r in rows if str(r.get("date")) == trade_date]
        if not on:
            return None
        r = on[-1]
        try:
            return {"close": float(r["close"]), "high": float(r["high"]),
                    "low": float(r["low"])}
        except Exception:  # noqa: BLE001
            return None

    def _advance_exits(self, trade_date: str) -> dict:
        """当日跟踪推进（spec-04 §2.2 第 3 项 / spec-01 §8.1，幂等）。

        为各跟踪账户拉取其在跟踪中的个股官方日线收盘/OHLC 与沪深300基准，调用引擎
        settle_exits（缺价票不进 market → 引擎按最近可得价 stale 语义处理，不虚构价）。
        """
        accounts, symbols = self._tracking_state(trade_date)
        if not accounts:
            return {"tracked_accounts": 0, "closed": 0, "updated": 0}
        market: dict[str, dict] = {}
        bench = self._day_ohlc(eodengine.EXIT_BENCH_SYMBOL, trade_date)
        if bench:
            market[eodengine.EXIT_BENCH_SYMBOL] = bench
        for sym in symbols:
            ohlc = self._day_ohlc(sym, trade_date)
            if ohlc:
                market[sym] = ohlc
        updated = closed = 0
        for acct in accounts:
            r = eodengine.settle_exits(self.state, acct, trade_date, market=market)
            updated += r.get("updated", 0)
            closed += r.get("closed", 0)
        gaps = len(symbols) - sum(1 for s in symbols if s in market)
        self._audit("trade.eod_exit_settle_auto", "ok",
                    f"{trade_date} 跟踪账户 {len(accounts)} 个，推进 {updated} 行，结清 {closed} 行，"
                    f"行情缺口 {gaps} 票")
        return {"tracked_accounts": len(accounts), "closed": closed, "updated": updated}

    def settle_once(self, now: datetime | None = None) -> dict:
        """单次判定+触发（含当日缺口账户记账，供 close_day_gaps 窗口关闭后补缺勤）。"""
        outcome = self._settle_once(now or bjt_now())
        self._record_gap_outcome(outcome)
        return outcome

    def _settle_once(self, now: datetime) -> dict:
        """单次判定+触发（窗口探测/结算本体，无记账副作用）。返回轻量状态机结果。"""
        day = now.date()
        dstr = day.isoformat()
        clock = now.time()
        if dstr in self._done_dates:
            return {"date": dstr, "status": "done"}
        if clock < self.earliest or clock >= self.retry_until:
            return {"date": dstr, "status": "outside_window",
                    "earliest": self.earliest.isoformat(),
                    "retry_until": self.retry_until.isoformat()}
        has_pending = settle_day.pending_any(self.state, dstr)
        if has_pending:
            return self._settle_trading_day(dstr)
        accounts, _ = self._tracking_state(dstr)
        if not accounts:
            # 空日快路径：仅一次本地 SQL，不发起任何行情网络请求
            return {"date": dstr, "status": "no_pending"}
        snap_day = self._probe_trade_date()
        if snap_day is None or snap_day != day:
            return {"date": dstr, "status": "not_trading_session",
                    "snapshot_date": snap_day.isoformat() if snap_day else None}
        exits = self._advance_exits(dstr)
        self._done_dates.add(dstr)
        return {"date": dstr, "status": "no_pending", "exits": exits}

    def _settle_trading_day(self, dstr: str) -> dict:
        """实盘会话当天：探测交易日 → 推进卖出跟踪 → 跑当日结算。"""
        snap_day = self._probe_trade_date()
        if snap_day is None or snap_day != date.fromisoformat(dstr):
            return {"date": dstr, "status": "not_trading_session",
                    "snapshot_date": snap_day.isoformat() if snap_day else None}

        exits = {}
        accounts, _ = self._tracking_state(dstr)
        if accounts:
            exits = self._advance_exits(dstr)
        report = settle_day.run_day(self.state, dstr, feed=self.feed,
                                    corp_events=self._corp_events_for(dstr))
        accounts_r = report.get("accounts", [])
        errors = [a for a in accounts_r if a.get("error")]
        if not errors:
            self._done_dates.add(dstr)
            self._audit("trade.eod_settle_auto", "ok",
                        f"{dstr} 结算完成，账户 {len(accounts_r)} 个")
            return {"date": dstr, "status": "settled", "accounts": accounts_r,
                    "exits": exits}
        self._audit("trade.eod_settle_auto", "partial",
                    f"{dstr} 存在缺口账户 {len(errors)} 个，窗口内续试")
        return {"date": dstr, "status": "retry_gap", "errors": errors,
                "accounts": accounts_r, "exits": exits}

    def catchup_missed(self, *, now: datetime | None = None,
                       start: date | None = None,
                       lookback_days: int = 40,
                       account_ids: list[str] | None = None) -> dict:
        """spec-04 §2.4/§2.6 快进回放：启动时按交易日顺序补齐确定性引擎工作。

        以 600000 日线轴为交易日本身（历史完整含收盘），范围 [start, today) 逐日推进：
        先跑在途卖出跟踪（settle_exits），再结算当日各账户工作（settle_day.run_day）。
        “今天”不在此列——当天会话由 settle_once 的实时探测/窗口处理，避免用不完整分钟档
        提前结算。settle_key 与 exit 行内 last_seen 双重幂等，重复执行零重复（补跑留痕）。
        """
        now = now or bjt_now()
        until = now.date()
        start = start or (until - timedelta(days=lookback_days))
        base = {"start": start.isoformat(), "until": until.isoformat()}
        if until <= start:
            return {**base, "trading_dates": [], "dates": []}
        try:
            rows = self.feed.day_rows("600000", start.isoformat(), until.isoformat())
        except Exception:  # noqa: BLE001
            log.exception("快进回放取交易日轴失败（跳过本轮）")
            return {**base, "trading_dates": [], "dates": []}
        trading = sorted({str(r.get("date")) for r in rows
                          if start.isoformat() <= str(r.get("date")) < until.isoformat()})
        if not trading:
            return {**base, "trading_dates": [], "dates": []}
        dates: list[dict] = []
        errs: list[dict] = []
        for d in trading:
            exits = {}
            if self._tracking_state(d)[0]:
                exits = self._advance_exits(d)
            report = settle_day.run_day(self.state, d, feed=self.feed,
                                        account_ids=account_ids,
                                        corp_events=self._corp_events_for(d))
            accts = report.get("accounts", [])
            acct_errs = [a for a in accts if a.get("error")]
            dates.append({"date": d, "exits": exits, "error": bool(acct_errs),
                          "accounts": accts})
            if acct_errs:
                errs.append({"date": d, "errors": acct_errs})
                self._fill_catchup_absent(d, acct_errs)
            self._push_deliveries(d)
            self._done_dates.add(d)
        self._audit("trade.eod_catchup_auto", "ok" if not errs else "partial",
                    f"快进回放 {len(trading)} 个会话日（{trading[0]}..{trading[-1]}），"
                    f"缺口会话 {len(errs)} 个")
        return {**base, "trading_dates": trading, "dates": dates, "errors": errs}

    def _record_gap_outcome(self, outcome: dict) -> None:
        """跟踪当日缺口账户（进程内）：结算落定/无错误 → 清标记；缺口 → 记原因。"""
        dstr = outcome.get("date")
        if not dstr:
            return
        errors = outcome.get("errors") or []
        if not errors:
            self._gap_accounts.pop(dstr, None)
            return
        self._gap_accounts[dstr] = [{
            "account_id": a.get("account_id"),
            "reason": (a.get("reason") or "结算缺口（行情/引擎数据供给拒绝）")[:12000],
        } for a in errors if a.get("account_id")]

    def close_day_gaps(self, now: datetime | None = None) -> dict:
        """当日结算缺口在触发窗口结束后补生成缺勤日报（spec-04 §5.3，P1 最小实现）。

        进程内记账的缺口账户（settle_once retry_gap 回报的 error 账户），于两种情况
        视为窗口关闭：昨日及更早日期（settle_once 只处理“今天”，历史缺口由本方法收敛），
        或今日已过 retry_until。届时账户仍无 settlement_log/daily_reports → 落
        status=absent 日报（数据段=可得部分，叙述=缺口原因）；已有日报则幂等跳过。
        跨进程恢复由 catchup_missed 兜底：启动重跑失败缺口日在同函数内直接补 absent。
        """
        now = now or bjt_now()
        today = now.date().isoformat()
        closed: list[dict] = []
        for dstr, gaps in list(self._gap_accounts.items()):
            if gaps is None:
                continue
            if dstr == today and now.time() < self.retry_until:
                continue                     # 窗口仍开：留给窗口内续试，不提前缺勤
            for g in gaps:
                try:
                    r = reporting.fill_absent_report(
                        self.state, g["account_id"], dstr,
                        f"结算任务未完成（{dstr}）：{g['reason']}")
                except Exception:  # noqa: BLE001
                    log.exception("缺勤日报补生成失败 %s %s", dstr, g["account_id"])
                    continue
                if not r["skipped"]:
                    closed.append({"account_id": g["account_id"],
                                   "trade_date": dstr,
                                   "report": r["report"]})
            self._gap_accounts[dstr] = None  # 该日缺口已收敛（成功/缺勤/幂等均视为已处理）
        if closed:
            self._audit("report.absent_auto", "ok",
                        f"窗口关闭补缺勤日报 {len(closed)} 份："
                        + "；".join(f"{c['account_id']}@{c['trade_date']}"
                                   for c in closed))
        return {"date": today, "absent": closed}

    def _fill_catchup_absent(self, trade_date: str, acct_errs: list[dict]) -> list[dict]:
        """快进回放某日存在缺口账户 → 补缺勤日报（幂等：已存在则跳过，原因以当日为准）。"""
        made: list[dict] = []
        for a in acct_errs:
            aid = a.get("account_id")
            if not aid:
                continue
            try:
                r = reporting.fill_absent_report(
                    self.state, aid, trade_date,
                    f"结算任务未完成（{trade_date}）：{a.get('reason') or '数据供给缺口'}")
            except Exception:  # noqa: BLE001
                log.exception("缺勤日报补生成失败 %s %s", trade_date, aid)
                continue
            if not r["skipped"]:
                made.append({"account_id": aid, "report": r["report"]})
        return made

    def run_trial_backfill(self, *, now: datetime | None = None,
                           start: date | None = None) -> dict:
        """spec-05 §6.2 试运行历史回放：为窗口未满的 trial 账户补最近 N 个交易日。

        只取交易日轴（600000 日线）上严格早于“今天”的会话——今天留给实时探测，防用
        不完整档提前结算。逐日推进该 trial 账户（settle_day.run_day mode='replay'，
        空日也计一个回放会话），add_trial_session 记账（agent+trade_date 唯一，幂等），
        满窗口自动转 done（验收证据就绪，等 finish_trial）。主账户全程零参与。
        """
        now = now or bjt_now()
        today = now.date()
        conn = state_conn(self.state)
        replays = conn.execute(
            "SELECT tr.agent_id, tr.trial_account_id, tr.window_days"
            " FROM trial_replays tr JOIN agents ag ON ag.id = tr.agent_id"
            " WHERE tr.status='in_progress' AND ag.status='trial'"
            " ORDER BY tr.created_ts, tr.agent_id"
        ).fetchall()
        if not replays:
            return {"replayed": []}
        processed = {r["agent_id"]: {d[0] for d in conn.execute(
            "SELECT trade_date FROM replay_sessions WHERE agent_id=?",
            (r["agent_id"],)).fetchall()} for r in replays}
        need = {r["agent_id"]: r["window_days"] - len(processed[r["agent_id"]])
                for r in replays}
        need = {a: n for a, n in need.items() if n > 0}
        if not need:
            return {"replayed": []}
        start = start or (today - timedelta(days=_TRIAL_LOOKBACK_DAYS))
        try:
            rows = self.feed.day_rows("600000", start.isoformat(), today.isoformat())
        except Exception:  # noqa: BLE001
            log.exception("试运行回放取交易日轴失败（跳过本轮）")
            return {"replayed": []}
        trading = sorted({str(r.get("date")) for r in rows
                          if str(r.get("date")) < today.isoformat()})
        if not trading:
            return {"replayed": []}
        by_agent = {r["agent_id"]: r for r in replays}
        out: list[dict] = []
        for agent_id in sorted(need):
            rec = by_agent[agent_id]
            avail = [d for d in trading if d not in processed[agent_id]]
            picks = avail[-need[agent_id]:]
            days: list[dict] = []
            for d in picks:
                exits = {}
                if self._tracking_state(d)[0]:
                    exits = self._advance_exits(d)
                report = settle_day.run_day(
                    self.state, d, feed=self.feed,
                    account_ids=[rec["trial_account_id"]], mode="replay",
                    corp_events=self._corp_events_for(d))
                accts = report.get("accounts", [])
                errors = [a for a in accts if a.get("error")]
                if errors:
                    # 对账不平/数据缺口：当日不记回放会话（窗口保持 in_progress 下轮重试，
                    # 等价实盘“窗口内续试”；规则级异常日不允许计入合格回放）
                    self._audit("trade.trial_replay_gap", "error",
                                f"{agent_id} {d} 试运行回放结算异常："
                                f"{errors[0].get('reason') or errors[0]}")
                    days.append({"date": d, "exits": exits, "error": True,
                                 "blocked": True, "accounts": accts})
                    continue
                accountstore.add_trial_session(self.state, agent_id, d)
                days.append({"date": d, "exits": exits, "error": False,
                             "accounts": accts})
            replay = accountstore.trial_replay(self.state, agent_id)
            if replay and replay["status"] == "done":
                self._audit("trade.trial_replay_complete", "ok",
                            f"{agent_id} 试运行回放满 {replay['window_days']} 个交易日"
                            f"（{replay['sessions'][0]}..{replay['sessions'][-1]}），"
                            f"验收证据就绪")
            else:
                self._audit("trade.trial_replay_session", "ok",
                            f"{agent_id} 回放 {len(days)} 个历史会话"
                            f"（累计 {replay['sessions_done']}/{replay['window_days']}）")
            out.append({"agent_id": agent_id,
                        "trial_account_id": rec["trial_account_id"],
                        "days": days,
                        "replay": replay})
        return {"replayed": out}

    def _push_deliveries(self, trade_date: str) -> list[dict]:
        """日报直达站内推送（spec-04 §6）：将 trade_date 首版结算/缺勤日报送入用户会话。
        幂等（payload 判重），试运行/退役 Agent 在 reporting 内被过滤。返回新消息供广播。"""
        try:
            return reporting.push_pending_report_deliveries(self.state, trade_date)
        except Exception:  # noqa: BLE001
            log.exception("日报直达推送失败 %s（已落库的日报不受影响）", trade_date)
            return []

    async def _notify_report_pushes(self, pushed: list[dict]) -> None:
        """WS 广播新推送的日报消息（有在线连接才发送，发送失败仅摘除该连接）。"""
        if not pushed:
            return
        from core.ws import broadcast  # noqa: PLC0415
        for m in pushed:
            try:
                await broadcast(self.state, {
                    "type": "conv", "event": "message_new",
                    "conv_id": m["conv_id"], "message": m,
                })
            except Exception:  # noqa: BLE001
                log.debug("日报消息 WS 广播失败：%s", m.get("id"))

    async def _evening_summaries(self, now: datetime) -> None:
        """§5.4/§5.5 夜间汇报（BJ 20:00 后每个 tick 收敛一次，幂等）：当日每日总汇报
        （确定性拼接版）＋上月月度《策略体检报告》（与总汇报同批推送语义的简化落地）。"""
        if now.time() < time(20, 0):
            return
        prev = (now.date().replace(day=1) - timedelta(days=1))
        pushed: list[dict] = []
        try:
            ds = reporting.push_daily_summary(self.state, now.date().isoformat())
            if ds:
                pushed.append(ds)
            mr = reporting.push_monthly_report(self.state, prev.year, prev.month)
            if mr:
                pushed.append(mr)
        except Exception:  # noqa: BLE001
            log.exception("夜间汇报生成失败（幂等，下个 tick 重试）")
        await self._notify_report_pushes(pushed)

    async def run_forever(self, tick_s: int) -> None:
        """每分钟 tick 循环（core 常驻内唯一结算触发点；操作全幂等）。"""
        while True:
            try:
                outcome = self.settle_once()
                dates: set[str] = set()
                if outcome.get("status") in ("settled", "retry_gap"):
                    dates.add(outcome["date"])
                closed = self.close_day_gaps()
                dates.update(x["trade_date"] for x in closed.get("absent", []))
                pushed: list[dict] = []
                for d in sorted(dates):
                    pushed += self._push_deliveries(d)
                log.info("EOD 结算触发：%s %s（日报直达推送 %s 条）",
                         outcome["date"], outcome["status"], len(pushed))
                self.run_trial_backfill()
                await self._notify_report_pushes(pushed)
                await self._evening_summaries(bjt_now())
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("EOD 结算触发异常（下个 tick 重试）")
            await asyncio.sleep(tick_s)

"""数据服务消费接口（spec-03 §4/§6，L0/L1/L2 按票供给 + 本地缓存）。

- is_ready / get_replay_series 对齐 spec-01 引擎消费语义（票级选档 D4/v0.17.2）：
  L0（本地 3 秒序列）→ L1（分钟线，本地缓存优先）→ L2（日线区间 + 官方收盘价）。
- 官方收盘价来自日线权威源并按日绑定（official_close_source，换源次日生效，
  spec-03 §4.1 B5）；L0 延续样本（close_candidates）与官方收盘价偏差 >0.3% →
  该票当日 quality=degraded（§4.2 #59，标记传播给结算/日报）。
- 分钟/日线拉取落本地 SQLite 缓存（增量更新，spec-03 §6）；拉取源按 spec-03 §2.2
  默认链从“已配置源”解析（运行期配置腾讯，见 config.market_sources）。
- 结算侧（settle_day/eodengine）沿用其现有适配器直供路径，本节不接管、不改行为。
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from core import l0store, source_chain
from core.quotes_tencent import QuoteGapError, QuoteSourceError
from core.db import state_conn

L0_READY_MIN = Decimal("0.90")
L1_EXPECTED_MINUTES = 240
L1_MISSING_TOLERANCE = Decimal("0.05")
CLOSE_DEV_TOL = Decimal("0.003")
_DAY_RANGE_DAYS = 20


def _minute_key(ts: str) -> str:
    return ts[11:16] if len(ts) >= 16 else ts


def _resolve_feed(state, role: str):
    configured = source_chain.configured_sources(
        getattr(state.settings, "market_sources", "") or "tencent")
    name = source_chain.resolve(configured, role)
    if name is None:
        raise QuoteSourceError(f"{role} 无可用数据源（已配置: {configured or '空'}）")
    return name, source_chain.adapter_for(name)


def _pair(state, feed, source: str, role: str) -> tuple[str, object]:
    """返回 (源名, 适配器)：测试可注入 feed 对象；否则按已配置源解析角色适配器。"""
    if feed is not None:
        return (source or getattr(feed, "SOURCE", "")), feed
    name, adapter = _resolve_feed(state, role)
    return name, adapter


def pull_minute(state, symbol: str, trade_date: str, *, feed=None,
                source: str = "") -> dict | None:
    """L1 分钟线供给：本地缓存优先；未命中则按源拉取并写缓存。

    拉取沿用适配器 replay_day 口径（仅当源可提供该日完整会话且会话末价与官方收盘
    一致才供给；否则抛 QuoteGapError，由调用方降 L2）。返回 {cached, source,
    minutes, official_close, prev_close, bars}。
    """
    cached = l0store.minute_cache_get(state, symbol, trade_date)
    if cached:
        return {"cached": True, "source": cached[0]["source"],
                "minutes": len(cached), "bars": [(r["minute"], Decimal(r["close"]))
                                                 for r in cached],
                "official_close": None, "prev_close": None}
    src, adapter = _pair(state, feed, source, "l1")
    fd = adapter.replay_day(symbol, trade_date)
    if fd.get("level") != "l1" or not fd.get("bars"):
        raise QuoteGapError(f"{symbol} {trade_date} L1 档不可供给")
    bars = [(b[0][11:16], Decimal(b[1])) for b in fd["bars"]]
    l0store.minute_cache_put(state, symbol, trade_date, bars, src)
    return {"cached": False, "source": src, "minutes": len(bars),
            "bars": bars, "official_close": Decimal(fd["official_close"]),
            "prev_close": Decimal(fd["prev_close"])}


def _fetch_day_rows(state, symbol: str, start: str, end: str, *, feed=None,
                    source: str = "") -> list[dict]:
    src, adapter = _pair(state, feed, source, "l2")
    rows = adapter.day_rows(symbol, start, end)
    for r in rows:
        l0store.daily_cache_put(state, symbol, r["date"], r, src)
    return rows


def official_close(state, symbol: str, trade_date: str, *, feed=None,
                   source: str = "") -> dict | None:
    """当日官方收盘价（日线权威源）+ 源当日绑定；本地缓存优先。"""
    cached = l0store.daily_cache_get(state, symbol, trade_date)
    if cached:
        return {"official_close": Decimal(cached["close"]),
                "source": cached["source"], "cached": True}
    start = (date.fromisoformat(trade_date) - timedelta(days=_DAY_RANGE_DAYS)
             ).isoformat()
    rows = _fetch_day_rows(state, symbol, start, trade_date, feed=feed,
                           source=source)
    on = [r for r in rows if str(r["date"]) == trade_date]
    if not on:
        return None
    src, _ = _pair(state, feed, source, "l2")
    row = on[-1]
    l0store.daily_cache_put(state, symbol, trade_date, row, src)
    l0store.set_official_close_source(state, trade_date, src)
    return {"official_close": Decimal(row["close"]), "source": src, "cached": False}


def is_ready(state, symbol: str, trade_date: str, *, l0_min: Decimal = L0_READY_MIN,
             l1_expected: int = L1_EXPECTED_MINUTES,
             l1_tolerance: Decimal = L1_MISSING_TOLERANCE) -> dict:
    """按票就绪判定（spec-03 §4.1；只读本地，不触发拉取——网络由显式拉取函数负责）。

    l1_ready：None=尚未拉取/无缓存，True=本地分钟根数通过（≥ 应有×(1−缺失容差)）。
    official_close：仅当本地日线缓存已有当日行才返回（无 → None，不虚构）。
    """
    cov = l0store.get_coverage(state, symbol, trade_date)
    rate = Decimal(cov["coverage_rate"]) if cov and cov["coverage_rate"] else Decimal(0)
    l0_ready = bool(cov) and rate >= l0_min
    minutes = l0store.minute_cache_get(state, symbol, trade_date)
    l1_need = int(Decimal(l1_expected) * (1 - l1_tolerance))
    l1_ready = None
    if minutes:
        l1_ready = len(minutes) >= l1_need
    day = l0store.daily_cache_get(state, symbol, trade_date)
    return {
        "symbol": symbol, "trade_date": trade_date,
        "l0_ready": l0_ready,
        "l0_coverage_rate": str(rate) if cov else None,
        "l0_quality": cov["quality"] if cov else None,
        "l1_ready": l1_ready, "l1_minutes": len(minutes),
        "official_close": str(day["close"]) if day else None,
        "official_close_source": day["source"] if day else None,
    }


def daily_pair(state, symbol: str, trade_date: str, *, feed=None,
               source: str = "") -> dict | None:
    """当日与前一交易日官方收盘（估值/前收用；spec-03 §4.1 prev_close 口径）。

    本地日线缓存优先；未命中按 20 日历日窗口拉取并写缓存。当日无日线 → None；
    有当日行但窗口内无前收 → None（prev_close_map 可缺省，不虚构）。"""
    start = (date.fromisoformat(trade_date) - timedelta(days=_DAY_RANGE_DAYS)
             ).isoformat()
    cached = l0store.daily_cache_range(state, symbol, start, trade_date)
    if cached:
        on = [r for r in cached if r["trade_date"] == trade_date]
        prev = [r for r in cached if r["trade_date"] < trade_date]
        if on and prev:
            return {"official_close": Decimal(on[-1]["close"]),
                    "prev_close": Decimal(prev[-1]["close"]),
                    "source": on[-1]["source"] or prev[-1]["source"],
                    "cached": True}
    rows = _fetch_day_rows(state, symbol, start, trade_date, feed=feed, source=source)
    on = [r for r in rows if str(r["date"]) == trade_date]
    if not on:
        return None
    prev = [r for r in rows if str(r["date"]) < trade_date]
    if not prev:
        return None
    src, _ = _pair(state, feed, source, "l2")
    row = on[-1]
    l0store.daily_cache_put(state, symbol, trade_date, row, src)
    l0store.set_official_close_source(state, trade_date, src)
    return {"official_close": Decimal(row["close"]),
            "prev_close": Decimal(prev[-1]["close"]),
            "source": src, "cached": False}


def settle_input(state, symbol: str, trade_date: str, *, feed=None,
                 source: str = "") -> dict:
    """单票 EOD 回放输入（spec-03 §4.1 供 settle_day 票级选档接线）。

    L0（覆盖率 ≥90% 的本地 3 秒序列）→ L1（分钟，缓存优先/源拉取，末价须与官方
    收盘对齐）→ L2（日线区间）；官方收盘价任一档缺失 → QuoteGapError（不虚构）。
    prev_close 在数据服务可给时补充（L0 档前收来自日线前一日，防估值/基准缺列）。
    """
    r = get_replay_series(state, symbol, trade_date, feed=feed, source=source)
    if r.get("prev_close") is None:
        pair = daily_pair(state, symbol, trade_date, feed=feed, source=source)
        if pair is not None:
            r["prev_close"] = pair["prev_close"]
    return r


def _l0_series(state, symbol: str, trade_date: str) -> list[tuple[str, Decimal]]:
    return [(r["ts"], Decimal(r["price"]))
            for r in l0store.get_l0_ticks(state, symbol, trade_date)]


def _close_candidates(state, symbol: str, trade_date: str) -> list[tuple[str, Decimal]]:
    rows = state_conn(state).execute(
        "SELECT ts, price FROM l0_ticks WHERE symbol=? AND trade_date=?"
        " AND status='normal' AND is_extended=1 ORDER BY ts",
        (symbol, trade_date)).fetchall()
    return [(r["ts"], Decimal(r["price"])) for r in rows]


def _mark_close_deviation(state, symbol: str, trade_date: str,
                          candidate: Decimal, official: Decimal) -> None:
    """偏差检测命中 → 票级 degraded（degraded_reason=close_deviation，不回填历史样本）。"""
    if official <= 0:
        return
    dev = abs(candidate - official) / official
    if dev <= CLOSE_DEV_TOL:
        return
    cov = l0store.get_coverage(state, symbol, trade_date)
    if cov is not None:
        l0store.update_coverage(
            state, symbol, trade_date, quality="degraded",
            degraded_reason="close_deviation",
            notes=f"{cov['notes']}；收盘偏差 {format(dev, '.4f')}")


def _l2_series(state, symbol: str, trade_date: str, *, feed=None,
               source: str = "") -> dict:
    """L2 日线区间兜底：官方收盘价 + 前收 + OHLC（spec-03 §4.1 降档，含当日不切源绑定）。"""
    start = (date.fromisoformat(trade_date) - timedelta(days=_DAY_RANGE_DAYS)
             ).isoformat()
    rows = _fetch_day_rows(state, symbol, start, trade_date, feed=feed,
                           source=source)
    on = [r for r in rows if str(r["date"]) == trade_date]
    if not on:
        raise QuoteGapError(f"{symbol} {trade_date} 非交易日或无日线")
    prev = [r for r in rows if str(r["date"]) < trade_date]
    if not prev:
        raise QuoteGapError(f"{symbol} {trade_date} 缺少前一日日线")
    row = on[-1]
    src, _ = _pair(state, feed, source, "l2")
    l0store.daily_cache_put(state, symbol, trade_date, row, src)
    l0store.set_official_close_source(state, trade_date, src)
    return {
        "level": "l2", "series": [], "close_candidates": [],
        "official_close": Decimal(row["close"]),
        "official_close_source": src,
        "prev_close": Decimal(prev[-1]["close"]),
        "session_date": trade_date, "source": src,
        "high": Decimal(row["high"]), "low": Decimal(row["low"]),
        "quality": "ok", "notes": "L2（日线区间近似）",
    }


def get_replay_series(state, symbol: str, trade_date: str, *, feed=None,
                      source: str = "") -> dict:
    """构造某票某交易日回放输入（spec-03 §4.1 get_replay_series）。

    选档：L0 就绪 → L0 判定序列（含 close_candidates 与官方收盘价偏差检测）；
    L0 不就绪 → L1 分钟（缓存优先；会话末价与官方收盘不一致/缺口 → QuoteGapError
    降 L2）；L2 日线区间兜底。任一档官方收盘价缺失 → 显式 QuoteGapError，不虚构。
    """
    ready = is_ready(state, symbol, trade_date)
    if ready["l0_ready"]:
        series = _l0_series(state, symbol, trade_date)
        oc = official_close(state, symbol, trade_date, feed=feed, source=source)
        if oc is None:
            raise QuoteGapError(f"{symbol} {trade_date} 官方收盘价缺失（L0 档拒绝供给）")
        candidates = _close_candidates(state, symbol, trade_date)
        quality = "ok"
        notes = "L0"
        if candidates:
            _mark_close_deviation(state, symbol, trade_date,
                                  candidates[-1][1], oc["official_close"])
            cov = l0store.get_coverage(state, symbol, trade_date)
            if cov and cov["quality"] == "degraded" and \
                    cov["degraded_reason"] == "close_deviation":
                quality = "degraded"
                notes = cov["notes"]
        return {
            "level": "l0", "series": series, "close_candidates": candidates,
            "official_close": oc["official_close"],
            "official_close_source": oc["source"],
            "prev_close": None, "session_date": trade_date,
            "source": oc["source"], "quality": quality, "notes": notes,
        }
    try:
        pm = pull_minute(state, symbol, trade_date, feed=feed, source=source)
    except (QuoteGapError, QuoteSourceError) as exc:
        return _l2_series(state, symbol, trade_date, feed=feed, source=source)
    last_close = pm["bars"][-1][1] if pm["bars"] else None
    if pm.get("official_close") is not None:
        official, prev, close_src = pm["official_close"], pm["prev_close"], pm["source"]
    else:
        oc = official_close(state, symbol, trade_date, feed=feed, source=source)
        if oc is None:
            raise QuoteGapError(f"{symbol} {trade_date} L1 无官方收盘价，拒绝供给")
        if last_close is not None and abs(last_close - oc["official_close"]) > Decimal("0.001"):
            raise QuoteGapError(f"{symbol} L1 分钟末价与官方收盘不一致，降 L2")
        official, prev, close_src = oc["official_close"], None, oc["source"]
    series = [(f"{trade_date}T{m}:00", c) for m, c in pm["bars"]]
    l0store.set_official_close_source(state, trade_date, close_src)
    return {
        "level": "l1", "series": series, "close_candidates": [],
        "official_close": official, "official_close_source": close_src,
        "prev_close": prev, "session_date": trade_date,
        "source": pm["source"], "quality": "ok", "notes": "L1（分钟近似）",
    }

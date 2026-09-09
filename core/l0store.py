"""spec-03 §5/§6 本地行情库：L0 3 秒序列/覆盖/绑定 + 观察集合/采集清单 + L1 分钟与日线缓存。

- L0 序列 PK(symbol, trade_date, ts) → 重复周期幂等（同 ts 覆盖不重复计数）；
  status='suspended' 的样本只落库审计（不进判定序列），suspended 秒数按样本折算；
  is_extended=1 的 15:00 后延续样本单独取用（close_candidates）。
- 覆盖率分母 = 该票当日实际可交易秒数 / 3（午休剔除；盘中停牌秒数从分母剔除，
  spec-03 §3.4/§4.1 A5）；中途启动分母仍按全天应有数、分子按实际采集数（禁止
  “启动过就算完整”）。
- 数值一律 TEXT 存 Decimal（spec-01 §0 数值精度约定），比较/展示时再还原。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from core.db import state_conn, write_txn

_BJT = timezone(timedelta(hours=8))

# spec-03 §3.2 交易时段连续段（上午/下午，均整 3 秒周期）；full_session_secs=14400
_SESSION_SPAN_SECS = 14400
# 观察集合上限（spec-03 §3.1：默认 ≤20，超出走审批）
DEFAULT_OBS_LIMIT = 20


def _now_ts() -> str:
    return datetime.now(_BJT).replace(tzinfo=None).isoformat(timespec="seconds")


def _txt(value) -> str:
    try:
        return format(Decimal(str(value)), "f")
    except (TypeError, ValueError):
        return "0"


def _d(value, default: str = "0"):
    try:
        return Decimal(str(value))
    except (TypeError, ValueError):
        return Decimal(default)


def expected_ticks(suspended_secs: int = 0) -> int:
    """当日应有采样点数（分母）：连续交易秒数 14400 − 盘中停牌秒数，除以 3 秒周期。"""
    return max(0, (_SESSION_SPAN_SECS - max(0, int(suspended_secs))) // 3)


# ---------------------------------------------------------------- L0 序列

def upsert_l0_ticks(state, rows: list[dict]) -> int:
    """批量落 L0 样本（INSERT OR REPLACE：同 ts 幂等）；返回写入行数。"""
    if not rows:
        return 0
    conn = state_conn(state)
    with write_txn(conn) as c:
        for r in rows:
            c.execute(
                "INSERT OR REPLACE INTO l0_ticks (symbol, trade_date, ts, price,"
                " prev_close, pct_chg, cum_turnover, status, is_extended, source,"
                " quality) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (r["symbol"], r["trade_date"], r["ts"], _txt(r.get("price", "0")),
                 _txt(r.get("prev_close", "0")), _txt(r.get("pct_chg", "0")),
                 _txt(r.get("cum_turnover", "0")),
                 r.get("status", "normal"),
                 1 if r.get("is_extended") else 0,
                 r.get("source", ""), r.get("quality", "ok")),
            )
    return len(rows)


def get_l0_ticks(state, symbol: str, trade_date: str, *,
                 extended: bool = False, suspended: bool = False) -> list[dict]:
    """取当日序列（按 ts 升序）。默认只取正常判定样本（15:00:00 前，含 15:00:00）。"""
    extra = []
    if not extended:
        extra.append(" AND is_extended = 0")
    if not suspended:
        extra.append(" AND status = 'normal'")
    sql = ("SELECT symbol, trade_date, ts, price, prev_close, pct_chg,"
           " cum_turnover, status, is_extended, source, quality"
           " FROM l0_ticks WHERE symbol=? AND trade_date=?" + "".join(extra)
           + " ORDER BY ts")
    rows = state_conn(state).execute(sql, (symbol, trade_date)).fetchall()
    return [dict(r) for r in rows]


def l0_counts(state, symbol: str, trade_date: str) -> dict:
    """当日各语义计数：normal / suspended / extended（供覆盖率重算与降级清单）。"""
    conn = state_conn(state)
    rows = conn.execute(
        "SELECT status, is_extended, COUNT(*) AS n FROM l0_ticks"
        " WHERE symbol=? AND trade_date=? GROUP BY status, is_extended",
        (symbol, trade_date)).fetchall()
    out = {"normal": 0, "suspended": 0, "extended": 0}
    for r in rows:
        if r["status"] == "suspended":
            out["suspended"] += int(r["n"])
        elif r["is_extended"]:
            out["extended"] += int(r["n"])
        else:
            out["normal"] += int(r["n"])
    return out


def refresh_coverage(state, symbol: str, trade_date: str) -> dict:
    """按已落样本重算该票当日覆盖（分子=有效判定样本，分母=可交易秒数/3）。

    返回 {expected, actual, suspended_secs, coverage_rate(str/Decimal)}；不覆写
    quality/degraded_reason（由采集器收盘后 finalize_coverage 统一收敛）。
    """
    c = l0_counts(state, symbol, trade_date)
    suspended_secs = c["suspended"] * 3
    expected = expected_ticks(suspended_secs)
    rate = Decimal(1) if expected == 0 and c["normal"] else (
        (Decimal(c["normal"]) / Decimal(expected)) if expected else Decimal(0))
    return {"expected": expected, "actual": c["normal"],
            "suspended_secs": suspended_secs,
            "coverage_rate": rate, "extended": c["extended"]}


def update_coverage(state, symbol: str, trade_date: str, *, started_ts: str = "",
                    ended_ts: str = "", notes: str = "",
                    quality: str = "ok", degraded_reason: str = "") -> dict:
    """持久化票级覆盖行（分子/分母/rate 来自 refresh_coverage 的最新口径）。

    quality 语义字典（spec-03 §7 B3 基础集）：ok | degraded；degraded_reason 取
    source_fault|coverage_gap|cross_check_hit|close_deviation 等，供日报降级清单区分。
    """
    stats = refresh_coverage(state, symbol, trade_date)
    conn = state_conn(state)
    rate_txt = _txt(stats["coverage_rate"])
    if not started_ts or not ended_ts:
        row = conn.execute(
            "SELECT MIN(ts) AS s, MAX(ts) AS e FROM l0_ticks"
            " WHERE symbol=? AND trade_date=?",
            (symbol, trade_date)).fetchone()
        started_ts = started_ts or (row["s"] if row and row["s"] else _now_ts())
        ended_ts = ended_ts or (row["e"] if row and row["e"] else _now_ts())
    with write_txn(conn) as c:
        c.execute(
            "INSERT INTO l0_coverage (symbol, trade_date, expected_ticks,"
            " actual_ticks, started_ts, ended_ts, suspended_secs, coverage_rate,"
            " quality, degraded_reason, notes)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(symbol, trade_date) DO UPDATE SET"
            " expected_ticks=excluded.expected_ticks,"
            " actual_ticks=excluded.actual_ticks,"
            " started_ts=CASE WHEN l0_coverage.started_ts='' THEN excluded.started_ts"
            "                 ELSE l0_coverage.started_ts END,"
            " ended_ts=excluded.ended_ts,"
            " suspended_secs=excluded.suspended_secs,"
            " coverage_rate=excluded.coverage_rate,"
            " quality=excluded.quality, degraded_reason=excluded.degraded_reason,"
            " notes=excluded.notes",
            (symbol, trade_date, stats["expected"], stats["actual"],
             started_ts or _now_ts(), ended_ts or _now_ts(), stats["suspended_secs"],
             rate_txt, quality, degraded_reason, (notes or "")[:2000]),
        )
    return get_coverage(state, symbol, trade_date)


def get_coverage(state, symbol: str, trade_date: str) -> dict | None:
    row = state_conn(state).execute(
        "SELECT * FROM l0_coverage WHERE symbol=? AND trade_date=?",
        (symbol, trade_date)).fetchone()
    return dict(row) if row else None


def coverage_for_day(state, trade_date: str) -> list[dict]:
    rows = state_conn(state).execute(
        "SELECT * FROM l0_coverage WHERE trade_date=? ORDER BY symbol", (trade_date,))
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 绑定

def set_source_binding(state, symbol: str, trade_date: str, source: str) -> None:
    conn = state_conn(state)
    with write_txn(conn) as c:
        c.execute(
            "INSERT INTO source_binding (symbol, trade_date, source)"
            " VALUES (?,?,?) ON CONFLICT(symbol, trade_date) DO NOTHING",
            (symbol, trade_date, source))


def get_source_binding(state, symbol: str, trade_date: str) -> str | None:
    row = state_conn(state).execute(
        "SELECT source FROM source_binding WHERE symbol=? AND trade_date=?",
        (symbol, trade_date)).fetchone()
    return row["source"] if row else None


def day_binding_sources(state, trade_date: str) -> list[str]:
    rows = state_conn(state).execute(
        "SELECT DISTINCT source FROM source_binding WHERE trade_date=?",
        (trade_date,)).fetchall()
    return [r["source"] for r in rows]


def set_official_close_source(state, trade_date: str, source: str) -> None:
    conn = state_conn(state)
    with write_txn(conn) as c:
        c.execute(
            "INSERT INTO official_close_source (trade_date, source)"
            " VALUES (?,?) ON CONFLICT(trade_date) DO UPDATE SET source=excluded.source",
            (trade_date, source))


def get_official_close_source(state, trade_date: str) -> str | None:
    row = state_conn(state).execute(
        "SELECT source FROM official_close_source WHERE trade_date=?",
        (trade_date,)).fetchone()
    return row["source"] if row else None


# ---------------------------------------------------------------- 观察集合 / 采集清单

def observation_list(state, *, active_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM market_observation"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY added_ts, symbol"
    return [dict(r) for r in state_conn(state).execute(sql).fetchall()]


def observation_add(state, symbol: str, reason: str = "") -> dict:
    """加入观察集合（常驻；重复添加幂等：已存在则激活并保留原行）。"""
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("证券代码不能为空")
    active = observation_list(state)
    exists = any(o["symbol"] == symbol for o in active)
    if not exists and len(active) >= DEFAULT_OBS_LIMIT:
        raise ValueError(f"观察集合已达上限 {DEFAULT_OBS_LIMIT}（超出需走审批）")
    conn = state_conn(state)
    now = _now_ts()
    with write_txn(conn) as c:
        c.execute(
            "INSERT INTO market_observation (symbol, added_ts, reason, active)"
            " VALUES (?,?,?,1) ON CONFLICT(symbol) DO UPDATE SET"
            " reason=excluded.reason, active=1",
            (symbol, now, (reason or "")[:500]))
    return next(o for o in observation_list(state) if o["symbol"] == symbol)


def observation_remove(state, symbol: str) -> bool:
    conn = state_conn(state)
    with write_txn(conn) as c:
        cur = c.execute("UPDATE market_observation SET active=0 WHERE symbol=?",
                        (symbol,))
    return cur.rowcount > 0


def replace_watchlist(state, trade_date: str, rows: list[dict]) -> int:
    """按日重建采集清单（当日 9:25/13:00 重算快照，幂等覆盖）。"""
    conn = state_conn(state)
    with write_txn(conn) as c:
        c.execute("DELETE FROM collector_watchlist WHERE trade_date=?", (trade_date,))
        for r in rows:
            c.execute(
                "INSERT INTO collector_watchlist (trade_date, symbol, reason)"
                " VALUES (?,?,?)",
                (trade_date, r["symbol"],
                 (r.get("reason") or "").strip() or "watchlist"))
    return len(rows)


def watchlist_for(state, trade_date: str) -> list[dict]:
    rows = state_conn(state).execute(
        "SELECT * FROM collector_watchlist WHERE trade_date=? ORDER BY symbol",
        (trade_date,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- L1 分钟 / L2 日线本地缓存

def minute_cache_put(state, symbol: str, trade_date: str, bars: list[tuple],
                     source: str) -> int:
    """写当日分钟缓存（清旧重写，增量拉取幂等）；bars=[(HH:MM, close)]。"""
    conn = state_conn(state)
    now = _now_ts()
    with write_txn(conn) as c:
        c.execute("DELETE FROM minute_cache WHERE symbol=? AND trade_date=?",
                  (symbol, trade_date))
        for minute, close in bars:
            c.execute(
                "INSERT INTO minute_cache (symbol, trade_date, minute, close,"
                " source, fetched_ts) VALUES (?,?,?,?,?,?)",
                (symbol, trade_date, minute, _txt(close), source or "", now))
    return len(bars)


def minute_cache_get(state, symbol: str, trade_date: str) -> list[dict]:
    rows = state_conn(state).execute(
        "SELECT * FROM minute_cache WHERE symbol=? AND trade_date=?"
        " ORDER BY minute", (symbol, trade_date)).fetchall()
    return [dict(r) for r in rows]


def daily_cache_put(state, symbol: str, trade_date: str, row: dict, source: str) -> None:
    conn = state_conn(state)
    now = _now_ts()
    with write_txn(conn) as c:
        c.execute(
            "INSERT INTO daily_cache (symbol, trade_date, open, close, high, low,"
            " volume, source, fetched_ts) VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(symbol, trade_date) DO UPDATE SET open=excluded.open,"
            " close=excluded.close, high=excluded.high, low=excluded.low,"
            " volume=excluded.volume, source=excluded.source,"
            " fetched_ts=excluded.fetched_ts",
            (symbol, trade_date, _txt(row.get("open", "0")),
             _txt(row.get("close", "0")), _txt(row.get("high", "0")),
             _txt(row.get("low", "0")), _txt(row.get("volume", "0")),
             source or "", now))


def daily_cache_get(state, symbol: str, trade_date: str) -> dict | None:
    row = state_conn(state).execute(
        "SELECT * FROM daily_cache WHERE symbol=? AND trade_date=?",
        (symbol, trade_date)).fetchone()
    return dict(row) if row else None


def daily_cache_range(state, symbol: str, start: str, end: str) -> list[dict]:
    rows = state_conn(state).execute(
        "SELECT * FROM daily_cache WHERE symbol=? AND trade_date>=? AND trade_date<=?"
        " ORDER BY trade_date", (symbol, start, end)).fetchall()
    return [dict(r) for r in rows]

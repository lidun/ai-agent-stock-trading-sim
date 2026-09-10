"""共享确定性行情 feed（EOD 结算/自动触发测试用）。

基于 core/tests/fixtures 录制样本回放 2026-09-04 的 600000 真实交易日：
replay_day 产出 L1 分钟序列（官方收盘一致校验），daily_pair 产出日线对，
realtime_batch 返回快照日期（供自动触发器的交易日探测）。
"""
from __future__ import annotations

from pathlib import Path

from core import quotes_tencent as q

FIX = Path(__file__).parent / "fixtures"
DATE = "2026-09-04"
SYMBOL = "600000"


class FakeFeed:
    """确定性 feed：600000 供给 2026-09-04，其余符号显式缺口。"""

    def _pair(self, symbol, trade_date):
        if symbol != SYMBOL or trade_date != DATE:
            raise q.QuoteGapError(f"{symbol} {trade_date} 超出 fixture 供给范围")
        rows = q.parse_day_rows((FIX / "tencent_day_sh600000.json").read_text("utf-8"))
        on = [r for r in rows if r["date"] == trade_date][-1]
        prev = [r for r in rows if r["date"] < trade_date][-1]
        return on["close"], prev["close"]

    def replay_day(self, symbol, trade_date):
        close, prev_close = self._pair(symbol, trade_date)
        sess = q.parse_minute_session(
            (FIX / "tencent_minute_sh600000.json").read_text("utf-8")
        )
        if abs(sess["close_last"] - close) > q.Decimal("0.001"):
            raise q.QuoteGapError("会话末价与官方收盘不一致")
        bars = [(f"{trade_date}T{h[:2]}:{h[2:]}:00", float(c)) for h, c, _ in sess["bars"]]
        return {"level": "l1", "official_close": float(close),
                "prev_close": float(prev_close), "bars": bars,
                "session_date": trade_date, "source": "tencent"}

    def replay_l2(self, symbol, trade_date):
        self._pair(symbol, trade_date)
        raise q.QuoteGapError(f"{symbol} {trade_date} 无 L2 日线区间 fixture")

    def daily_pair(self, symbol, trade_date):
        close, prev_close = self._pair(symbol, trade_date)
        return {"official_close": float(close), "prev_close": float(prev_close),
                "session_date": trade_date, "source": "tencent"}

    def day_rows(self, symbol, start, end):
        """日线区间（market_data 官方收盘/前收与 L2 兜底走 day_rows，非 replay_l2）。"""
        if symbol != SYMBOL:
            raise q.QuoteGapError(f"{symbol} 无日线区间 fixture")
        rows = q.parse_day_rows((FIX / "tencent_day_sh600000.json").read_text("utf-8"))
        return [r for r in rows if start <= str(r["date"]) <= end]

    def realtime_batch(self, symbols):
        return {}


class L2OnlyFeed(FakeFeed):
    """600000 仅供给 L2 日线档（replay_day 显式缺口）→ 驱动 run_day 的 L2 回退路径。"""

    def replay_day(self, symbol, trade_date):
        raise q.QuoteGapError(f"{symbol} {trade_date} 无分钟 fixture（L2 回退路径）")

    def replay_l2(self, symbol, trade_date):
        close, prev_close = self._pair(symbol, trade_date)
        rows = q.parse_day_rows((FIX / "tencent_day_sh600000.json").read_text("utf-8"))
        on = [r for r in rows if r["date"] == trade_date][-1]
        return {"level": "l2", "high": float(on["high"]), "low": float(on["low"]),
                "official_close": float(close), "prev_close": float(prev_close),
                "session_date": trade_date, "source": "tencent"}


class L1SparseFeed(FakeFeed):
    """L1 分钟档缺若干根（缺失 ≤ 容差带）→ market_data 仍走 L1 但标 degraded。"""

    def __init__(self, drop: int = 5):
        self._drop = drop

    def replay_day(self, symbol, trade_date):
        r = super().replay_day(symbol, trade_date)
        r["bars"] = r["bars"][: -self._drop] if self._drop else r["bars"]
        return r


class ReadySessionFeed(FakeFeed):
    """快照日期指向 fixture 交易日（自动触发需探测通过）。"""

    def realtime_batch(self, symbols):
        compact = DATE.replace("-", "")
        return {"600000": {"ts": compact + "153500"}}


class AxisFeed(FakeFeed):
    """交易日轴 feed：600000 day_rows 返回 fixture 全部日线（供试运行回放/快进轴）。
    不供给当日行情——配合“空日回放也计数”的回放语义，run_day 在无订单无持仓时跳过。"""

    def day_rows(self, symbol, start, end):
        if symbol != SYMBOL:
            raise q.QuoteGapError(f"{symbol} 无日线轴 fixture")
        return q.parse_day_rows((FIX / "tencent_day_sh600000.json").read_text("utf-8"))


class MultiDayL2Feed(FakeFeed):
    """历史多日 L2 供给：replay_day 显式缺口 → 全走日线 L2 档（试运行历史回放用）。

    供 fixture 全部交易日任意一日：replay_l2/daily_pair 返回该日 official_close/prev_close
    （prev 取更早最近交易日），replay_day 恒缺口驱动 L2 回退。"""

    def __init__(self, dates: set[str] | None = None):
        rows = q.parse_day_rows((FIX / "tencent_day_sh600000.json").read_text("utf-8"))
        rows = sorted(rows, key=lambda r: r["date"])
        self._rows = rows
        self._dates = dates or {str(r["date"]) for r in rows}

    def _idx(self, trade_date: str) -> int | None:
        for i, r in enumerate(self._rows):
            if str(r["date"]) == trade_date:
                return i
        return None

    def _pair(self, symbol, trade_date):
        if symbol != SYMBOL or trade_date not in self._dates:
            raise q.QuoteGapError(f"{symbol} {trade_date} 超出多日 L2 fixture 供给范围")
        i = self._idx(trade_date)
        assert i is not None
        on = self._rows[i]
        prev = self._rows[i - 1]["close"] if i >= 1 else on["close"]
        return {"close": float(on["close"]), "prev": float(prev),
                "high": float(on["high"]), "low": float(on["low"])}

    def replay_day(self, symbol, trade_date):
        raise q.QuoteGapError(f"{symbol} {trade_date} 历史日无分钟档（L2 档回放）")

    def replay_l2(self, symbol, trade_date):
        p = self._pair(symbol, trade_date)
        return {"level": "l2", "high": p["high"], "low": p["low"],
                "official_close": p["close"], "prev_close": p["prev"],
                "session_date": trade_date, "source": "tencent"}

    def daily_pair(self, symbol, trade_date):
        p = self._pair(symbol, trade_date)
        return {"official_close": p["close"], "prev_close": p["prev"],
                "session_date": trade_date, "source": "tencent"}

    def day_rows(self, symbol, start, end):
        if symbol != SYMBOL:
            raise q.QuoteGapError(f"{symbol} 无日线轴 fixture")
        return [r for r in self._rows if str(r["date"]) in self._dates]

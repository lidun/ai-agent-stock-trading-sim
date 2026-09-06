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

    def daily_pair(self, symbol, trade_date):
        close, prev_close = self._pair(symbol, trade_date)
        return {"official_close": float(close), "prev_close": float(prev_close),
                "session_date": trade_date, "source": "tencent"}

    def realtime_batch(self, symbols):
        return {}


class ReadySessionFeed(FakeFeed):
    """快照日期指向 fixture 交易日（自动触发需探测通过）。"""

    def realtime_batch(self, symbols):
        compact = DATE.replace("-", "")
        return {"600000": {"ts": compact + "153500"}}

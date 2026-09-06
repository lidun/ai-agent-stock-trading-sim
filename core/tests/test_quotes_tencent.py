"""腾讯系行情适配器解析测试（确定性：基于回放 fixtures，不依赖网络）。"""
from __future__ import annotations

from pathlib import Path
from decimal import Decimal

import pytest

from core import quotes_tencent as q

FIX = Path(__file__).parent / "fixtures"


def _day_text() -> str:
    return (FIX / "tencent_day_sh600000.json").read_text(encoding="utf-8")


def _minute_text() -> str:
    return (FIX / "tencent_minute_sh600000.json").read_text(encoding="utf-8")


def test_parse_day_rows_official_and_prev():
    rows = q.parse_day_rows(_day_text())
    dates = [r["date"] for r in rows]
    assert "2026-09-04" in dates
    on = [r for r in rows if r["date"] == "2026-09-04"][-1]
    assert on["close"] == Decimal("9.430") and on["high"] == Decimal("9.450")
    assert on["low"] == Decimal("9.260") and on["open"] == Decimal("9.270")
    prev = [r for r in rows if r["date"] == "2026-09-03"][-1]
    assert prev["close"] == Decimal("9.270")


def test_parse_minute_session_trims_extended():
    sess = q.parse_minute_session(_minute_text())
    bars = sess["bars"]
    assert bars[0][0] == "0930" and bars[-1][0] == "1500"
    assert all(hhmm <= "1500" for hhmm, _, _ in bars)
    assert sess["close_last"] == Decimal("9.430")
    # 冻结续段单独归入 extended（spec-03 is_extended 语义）
    assert any(hhmm > "1500" for hhmm, _, _ in sess["extended"])


def test_minute_ts_shaping():
    sess = q.parse_minute_session(_minute_text())
    ts = [f"2026-09-04T{h[:2]}:{h[2:]}:00" for h, _, _ in sess["bars"]]
    assert ts[0] == "2026-09-04T09:30:00"
    assert ts[-1] == "2026-09-04T15:00:00"


def test_parse_qt_snapshot():
    raw = (FIX / "tencent_qt_sample.txt").read_bytes().decode("gbk", errors="replace")
    snap = q.parse_qt_snapshot(raw)
    assert set(snap) == {"600000", "000001"}
    assert snap["600000"]["price"] == Decimal("9.43")
    assert snap["600000"]["prev_close"] == Decimal("9.27")
    assert snap["600000"]["ts"].startswith("20260904")


def test_secid_of():
    assert q.secid_of("600000") == "sh600000"
    assert q.secid_of("000001") == "sz000001"
    assert q.secid_of("300750") == "sz300750"
    with pytest.raises(ValueError):
        q.secid_of("abc")


def test_replay_l2_hist_range_from_day_rows(monkeypatch):
    """L2 历史档：由日 K 行构造 {high,low,官方收盘,前收}——区间触达判定与 T+1 前提。"""
    monkeypatch.setattr(q, "_http_get", lambda url: _day_text())
    l2 = q.replay_l2("600000", "2026-08-21")
    assert l2["level"] == "l2" and l2["session_date"] == "2026-08-21"
    assert l2["source"] == q.SOURCE
    rows = q.parse_day_rows(_day_text())
    on = [r for r in rows if r["date"] == "2026-08-21"][-1]
    prev = [r for r in rows if r["date"] == "2026-08-20"][-1]
    assert l2["high"] == float(on["high"]) and l2["low"] == float(on["low"])
    assert l2["official_close"] == float(on["close"])
    assert l2["prev_close"] == float(prev["close"])


def test_replay_l2_refuses_nontrading_or_missing_prev(monkeypatch):
    """非交易日无当日日 K → gap；窗口内无前收（fixture 首行）→ gap（宁缺毋错，绝不虚构前收）。"""
    monkeypatch.setattr(q, "_http_get", lambda url: _day_text())
    with pytest.raises(q.QuoteGapError, match="非交易日或无日线"):
        q.replay_l2("600000", "2026-09-05")     # 周末：fixture 末行为 09-04
    with pytest.raises(q.QuoteGapError, match="缺少"):
        q.replay_l2("600000", "2026-08-20")     # fixture 最早一日：无更早日线

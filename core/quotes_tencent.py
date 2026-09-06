"""spec-03 数据源适配器：腾讯系（web.ifzq.gtimg.cn / qt.gtimg.cn）免费源实现。

本模块实现统一适配器接口的 P1 最小子集（spec-03 §2.1）：
- realtime_batch(symbols)：qt.gtimg.cn 批量实时快照（当前价/昨收/最新时段）；
- day_rows(symbol, start, end)：ifzq 日 K 原始（未复权）OHLCV —— 官方收盘价/前收权威列；
- minute_session(symbol)：ifzq 当日分时（最新一个交易日整段 09:30-15:00 分钟价），
  15:06-15:30 的冻结续段按 is_extended 语义剔除（spec-03 §3.2/§4.1，不进判定序列）。

源侧约束（v1，单源）：
- 分钟数据仅提供最新一个交易会话；早于该日的分钟历史不可得 → 历史日/老交易日补跑
  走 L2 日线区间档（replay_l2，spec-01 §3.3 官方收盘价成交近似）；
- L0 3 秒序列不由此源供给（本地采集器职责，spec-03 §3，P1 未接）；
- quality/交叉抽检：source_family='tencent'，异族抽检第二源未配置前不做假抽检（spec-03 §7 B2 语义：跳过并记录）。

测试用确定性解析：解析函数以响应文本为输入、不依赖网络（fixtures 回放）。
"""
from __future__ import annotations

import json
import logging
import re
import urllib.request
from datetime import date, timedelta
from decimal import Decimal

log = logging.getLogger(__name__)

SOURCE = "tencent"
FAMILY = "tencent"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
_HTTP_TIMEOUT = 10


class QuoteSourceError(Exception):
    """数据源请求/解析失败。"""


class QuoteGapError(Exception):
    """数据源无法按请求口径供给（历史分钟深度不足等）。"""


def _http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 源级错误统一上抛
        raise QuoteSourceError(f"tencent http 失败 {url}: {exc}") from exc


def secid_of(symbol: str) -> str:
    """6 位代码 → 腾讯 secid（sh/sz 前缀）。"""
    if len(symbol) != 6 or not symbol.isdigit():
        raise ValueError(f"非法证券代码: {symbol}")
    return ("sh" if symbol[0] in ("6", "9", "5") else "sz") + symbol


def _day_rows_text(symbol: str, start: str, end: str) -> str:
    secid = secid_of(symbol)
    url = (
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
        f"param={secid},day,{start},{end},30,"
    )
    return _http_get(url)


def parse_day_rows(text: str) -> list[dict]:
    """解析 ifzq 日 K：返回 [{date, open, close, high, low, volume}]（原始未复权）。"""
    payload = json.loads(text)
    if payload.get("code") not in (0, 200, None):
        raise QuoteSourceError(f"day 响应异常: {payload.get('code')} {payload.get('msg')}")
    out: list[dict] = []
    data = payload.get("data") or {}
    for _, sym in data.items():
        for key in ("day",):
            for row in sym.get(key) or []:
                out.append({
                    "date": row[0],
                    "open": Decimal(row[1]),
                    "close": Decimal(row[2]),
                    "high": Decimal(row[3]),
                    "low": Decimal(row[4]),
                    "volume": Decimal(row[5]) if len(row) > 5 else Decimal("0"),
                })
    out.sort(key=lambda r: r["date"])
    return out


def day_rows(symbol: str, start: str, end: str) -> list[dict]:
    return parse_day_rows(_day_rows_text(symbol, start, end))


def _minute_text(symbol: str) -> str:
    url = "https://web.ifzq.gtimg.cn/appstock/app/minute/query?" f"code={secid_of(symbol)}"
    return _http_get(url)


def parse_minute_session(text: str) -> dict:
    """解析 ifzq 分时：{bars: [(hhmm, close, cum_amount)], close_last, extended}。

    剔除 15:06-15:30 冻结续段（spec-03 is_extended 语义），判定序列只到 15:00。
    """
    payload = json.loads(text)
    data = payload.get("data") or {}
    bars: list[tuple[str, Decimal, Decimal]] = []
    extended: list[tuple[str, Decimal, Decimal]] = []
    for _, sym in data.items():
        rows = ((sym.get("data") or {}).get("data")) or []
        for row in rows:
            parts = row.split()
            if len(parts) < 3:
                continue
            hhmm, price, cum_amt = parts[0], Decimal(parts[1]), Decimal(parts[2])
            if hhmm > "1500":
                extended.append((hhmm, price, cum_amt))
            else:
                bars.append((hhmm, price, cum_amt))
    bars.sort(key=lambda b: b[0])
    if not bars:
        raise QuoteSourceError("分时响应为空")
    return {
        "bars": bars,
        "close_last": bars[-1][1],
        "extended": extended,
    }


def minute_session(symbol: str) -> dict:
    return parse_minute_session(_minute_text(symbol))


def _qt_text(symbols: list[str]) -> str:
    codes = ",".join(secid_of(s) for s in symbols)
    req = urllib.request.Request(
        f"http://qt.gtimg.cn/q={codes}",
        headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("gbk", errors="replace")
    except Exception as exc:  # noqa: BLE001
        raise QuoteSourceError(f"qt http 失败: {exc}") from exc
    return raw


def parse_qt_snapshot(text: str) -> dict[str, dict]:
    """解析 qt 批量快照。字段：{name, price, prev_close, open, ts}。"""
    out: dict[str, dict] = {}
    for m in re.finditer(r'v_(\w+)="([^"]*)"', text):
        code, payload = m.group(1)[2:], m.group(2).split("~")
        if len(payload) < 35:
            continue
        out[code] = {
            "name": payload[1],
            "price": Decimal(payload[3] or "0"),
            "prev_close": Decimal(payload[4] or "0"),
            "open": Decimal(payload[5] or "0"),
            "ts": payload[30],
        }
    return out


def realtime_batch(symbols: list[str]) -> dict[str, dict]:
    if not symbols:
        return {}
    return parse_qt_snapshot(_qt_text(symbols))


def daily_pair(symbol: str, trade_date: str) -> dict:
    """仅日线口径的收盘/前收（供持仓估值，无需分钟序列）。"""
    start = (date.fromisoformat(trade_date) - timedelta(days=12)).isoformat()
    day = day_rows(symbol, start, trade_date)
    rows_on = [r for r in day if r["date"] == trade_date]
    if not rows_on:
        raise QuoteGapError(f"{symbol} {trade_date} 非交易日或无日线")
    prev = [r for r in day if r["date"] < trade_date]
    if not prev:
        raise QuoteGapError(f"{symbol} 缺少 {trade_date} 前一日日线")
    return {
        "official_close": rows_on[-1]["close"],
        "prev_close": prev[-1]["close"],
        "session_date": trade_date,
        "source": SOURCE,
    }


def replay_l2(symbol: str, trade_date: str) -> dict:
    """构造某票某交易日 L2 回放输入（spec-01 §3.3：日线区间触达 + 官方收盘价成交）。

    供历史日补跑/试运行回放：分钟深度不可得的交易日，退到日线 OHLC 区间近似档。
    返回 {level:'l2', high, low, official_close, prev_close, session_date, source}。
    """
    start = (date.fromisoformat(trade_date) - timedelta(days=20)).isoformat()
    day = day_rows(symbol, start, trade_date)
    on = [r for r in day if r["date"] == trade_date]
    if not on:
        raise QuoteGapError(f"{symbol} {trade_date} 非交易日或无日线")
    prev = [r for r in day if r["date"] < trade_date]
    if not prev:
        raise QuoteGapError(f"{symbol} 缺少 {trade_date} 前一日日线")
    row = on[-1]
    return {
        "level": "l2",
        "high": float(row["high"]),
        "low": float(row["low"]),
        "official_close": float(row["close"]),
        "prev_close": float(prev[-1]["close"]),
        "session_date": trade_date,
        "source": SOURCE,
    }


def replay_day(symbol: str, trade_date: str) -> dict:
    """构造某票某交易日 L1 回放输入（spec-03 §4.1 get_replay_series 的 P1 单源实现）。

    trade_date 必须是腾讯分时所能提供的最新完整交易会话（历史日分钟不可得 → QuoteGapError，
    由 settle_day 自动回退 replay_l2 的 L2 日线区间档）。分钟会话末价与官方日线收盘不一致 → 拒绝供给。
    返回 {level, official_close, prev_close, bars:[(ts_naive_beijing, close)], session_date, source}。
    """
    pair = daily_pair(symbol, trade_date)
    official_close = pair["official_close"]
    prev_close = pair["prev_close"]
    try:
        sess = minute_session(symbol)
    except QuoteSourceError as exc:
        raise QuoteGapError(f"{symbol} 分时不可得: {exc}") from exc
    last = sess["close_last"]
    if abs(last - official_close) > Decimal("0.001"):
        raise QuoteGapError(
            f"{symbol} 分时会话末价 {last} 与官方收盘 {official_close} 不一致——"
            "疑似会话日期非 trade_date，拒绝供给"
        )
    bars = [(f"{trade_date}T{h[:2]}:{h[2:]}:00", c) for h, c, _ in sess["bars"]]
    return {
        "level": "l1",
        "official_close": official_close,
        "prev_close": prev_close,
        "bars": bars,
        "session_date": trade_date,
        "source": SOURCE,
    }

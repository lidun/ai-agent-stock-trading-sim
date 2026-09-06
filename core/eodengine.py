"""EOD 回放撮合/结算引擎（spec-01 §3/§6 的受支持子集，P1 引擎切片）。

范围与边界（本版明确支持；其余拒绝而非静默跳过——宁可报 gap，不可错结）：
- 账户：granularity=eod_replay 的策略账户；单日结算；settle_key UNIQUE 幂等（§3.1.5/§3.8 单事务）；
- 条件单：scope=single、basis=replay_l0、order_type ∈ {buy, sell_take_profit, sell_stop,
  sell_trail, sell_open_board, time}、price_type ∈ {market, limit}、单票触发（trigger 统一 spec-01 §2.4 kind
  语义）——价格类 price_le/price_ge（canonical {"kind","price"}；legacy {"op","price"}
  兼容读取）、涨跌幅类 pct_chg（昨收折算静态触发价，可选 not_limit 未封板）、相对成本类
  vs_cost（可卖持仓成本逐点折算动态触发价，spec-01 §2.4）；
- 涨跌停封死例外（spec-01 §3.5/§3.6，可选启用）：board_map 显式提供板块且 prev_close_map
  含该票时启用——涨跌停价按板块系数与 §0 四舍五入实时计算；涨停封死段买入不成交、跌停封死段
  卖出不成交，盘中开板后恢复；L1/L2 仅整分钟/一字板封死才阻断（无日内粒度的近似口径）；
- ST/新股买入拦截（spec-01 §3.6）：restrict_map[symbol] 提供当日风险标记（st/ipo，逗号或空格
  分隔可多标）且 accounts.buy_exempt（JSON token 数组，需审批，默认 []）不含对应 token → 买入单
  开盘前置 invalid（invalid_reason=`restricted_buy:st[,ipo]`，进日报）；卖出类单不受拦截
  （已持仓遇 ST 不自动卖 §3.6）。板块细判（创业板/科创板 ST 仍 ±20）由调用方合成 st+board 信息。
- 开板卖出（sell_open_board，spec-01 §3.5/§4.1 事件行）：trigger {"kind":"open_board"} 仅 L0
  有效——L1/L2 当日该票该单置 invalid（invalid_reason=`basis_requires_l0`，进日报不静默）。L0 语义：
  需持仓且当日 L0 序列曾触涨停（price==涨停价，board_map×prev_close_map 裁定）后首次出现
  price<涨停价 → 开盘采样点价成交；未封板即收市 → 当日不触发、today 单到期 expired。
  未启用 board 涨跌停裁定（lim 不可得）时按无涨停概念处理：不触发也不报错。
- 移动止盈（sell_trail，spec-01 §3.3/§4.1）：trigger {"kind":"trail","drop_pct":N}，
  回撤基准=持仓期最高价（hist_high_map 提供买入以来至昨日的日线 high 累计最大，取值窗含
  当日）——L0 用当日采样价累计最大、L1 用分钟 high/close 累计最大（退化 quality=degraded）、
  L2 用 max(hist, 当日 high) 单点判定（退化 degraded），成交沿用各档价口径；
- 定时单（time，spec-01 §2.4/§4.1 时间行，与档无关）：trigger {"kind":"time","at":"HH:MM"}；
  到点按该档采样价/分钟收盘价成交（L2 日线近似档按官方收盘价），created_at 早于触发时刻才有效
  （#38），一次性判定——资金/可卖不足记 insufficient 后当日不再复判、today 单到期 expired；
- 档位：L0（series_map 采样点序列，触达采样点价成交）、L1（l1_map 分钟序列，spec-01 §3.3——
  相邻分钟确认触达、按条件价 X 成交、15:00 收盘分钟按保守口径 max/min(X, 官方收盘价) 并标
  close_minute_fill）与 L2（l2_map 当日 high/low 区间触达 + 官方收盘价成交，spec-01 §3.3，
  历史日/分钟不可得时的日线档）；L0/L1 下 price_type=market 无价单属未定义语义 → 显式
  EngineGapError；
- 成交价口径（§3.3 L0）：触达采样点价成交，滑点 0；L2 恒以官方收盘价成交；
- T+1（§6.2）：可卖数 = Σ lots(buy_date < trade_date).remaining，当日新 lot 不可卖；
- 全量成交：现金/可卖不足不成交、记 insufficient_events、继续参与后续采样（§2.3 v0.3）；
- 收盘对齐（§3.2 步3）：序列末价与官方收盘价偏差 >0.3% → trade.quality='degraded'；
- 份额法（§6.1）：NAV=(cash+Σqty×官方收盘价)/shares；today_pnl 对比期初估值（prev_close）；
- 对账双守恒（§6.5 D1=A）：单事务落账前自检——式一金额守恒（现金逐笔净流）与式二 NAV 守恒
  （现金+持仓市值独立推导）并列校验，任一不平 → EngineError → 整事务回滚（带病不落账）；
- 时间契约：交易日本地墙钟 naive ISO；order 仅在其 created_at 之后的采样点参与判定（防前视 #38）；
  L2 为日线近似档：created_at ≥ 当日 15:00 的 order 不参与当日判定。

未支持（命中即抛 EngineGapError，不写任何数据）：篮子/组合/封板确认事件类（seal_confirm，
需盘口/封单数据）、volume 量能类、signal_registry（候选/买入等信号注册需策略侧供给通道，
本版未建）、公司行动。

卖出跟踪（§8.1）引擎侧已实现：卖出成交自动登记 exit_trackings；跟踪推进与结清由
settle_exits 在结算日调用（N=10 交易日结清，确定性/零 token，停牌缺价→最近可得价 stale
结清标注，不虚构价）。signal_registry 的卖出登记语义在 §8.1 内以 exit_trackings 承载。
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from core.db import state_conn, write_txn

log = logging.getLogger(__name__)

SUPPORTED_ORDER_TYPES = {"buy", "sell_take_profit", "sell_stop", "sell_trail",
                           "sell_open_board", "time"}

_MONEY = Decimal("0.01")
_COST = Decimal("0.0001")
_CLOSE_TOL = Decimal("0.003")

FEES = {
    "commission_rate": Decimal("0.00025"),
    "commission_min": Decimal("5"),
    "stamp_tax_rate": Decimal("0.0005"),
    "transfer_fee_rate": Decimal("0.00001"),
}

# spec-01 §8.1 卖出跟踪：默认 N=10 个交易日后确定性结清（卖出当日不计，会计入下一起）；
# 基准指数 = 沪深300（同期超额）；结清阈值 = fwd/excess 百分数（引擎常量，P1 校准用）。
EXIT_DEFAULT_DAYS = 10
EXIT_BENCH_SYMBOL = "000300"
_EXIT_OK_PCT = Decimal("-3")      # fwd/excess ≤ −3% → 卖对
_EXIT_EARLY_PCT = Decimal("5")    # fwd/excess ≥ +5% → 卖早
_EXIT_REASONS = {"止盈", "止损", "调仓", "主动", "强平", "清仓"}


class EngineError(Exception):
    """前置校验失败（不改写任何数据）。"""


class EngineGapError(EngineError):
    """命中了本引擎尚未实现的 spec 语义。"""


def _D(v) -> Decimal:
    return Decimal(str(v)) if v is not None else Decimal("0")


def _q(v: Decimal) -> str:
    return str(v)


# spec-01 §3.6 板块涨跌幅系数（%）：main 主板 ±10%、gem 创业板 ±20%、star 科创板 ±20%、
# bj 北交所 ±30%、st_main ST 主板 ±5%（§3.5 判定表）。涨/跌停价 = prev_close×(1±系数)
# 按 §0 四舍五入到分；本引擎对上下限同为 half-up 对称口径（数据接入侧复核交易所规则时再调）。
_BOARD_LIMITS = {"main": "10", "gem": "20", "star": "20", "bj": "30", "st_main": "5"}


def _limit_px(prev_close: Decimal, board: str | None) -> tuple[Decimal, Decimal]:
    """返回 (涨停价, 跌停价)。board 缺省或未知板块名称回退 main（±10%）。"""
    pct = _BOARD_LIMITS.get(board or "main", "10")
    k = _D(pct) / _D("100")
    return (
        (prev_close * (_D("1") + k)).quantize(_MONEY, ROUND_HALF_UP),
        (prev_close * (_D("1") - k)).quantize(_MONEY, ROUND_HALF_UP),
    )


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fees(amount: Decimal, *, side: str, fee: dict) -> dict[str, Decimal]:
    comm = max(fee["commission_min"], amount * fee["commission_rate"])
    stamp = amount * fee["stamp_tax_rate"] if side == "sell" else Decimal("0")
    transfer = amount * fee["transfer_fee_rate"]
    total = comm + stamp + transfer
    return {
        "commission": comm.quantize(_MONEY, ROUND_HALF_UP),
        "stamp_tax": stamp.quantize(_MONEY, ROUND_HALF_UP),
        "transfer_fee": transfer.quantize(_MONEY, ROUND_HALF_UP),
        "total": total.quantize(_MONEY, ROUND_HALF_UP),
    }


_PRICE_KINDS = {"price_le": "le", "price_ge": "ge"}
_PCT_KINDS = {"pct_chg", "vs_cost"}
_UNIMPL_KINDS = ("open_board", "seal_confirm", "volume", "time", "and", "or")


def _kind_price(kind: str, raw: dict, *, prev: Decimal | None = None) -> tuple:
    """spec-01 §2.4 kind 触发解析 → (mode, op, price, pct, not_limit)。

    - price_le/price_ge：mode=px，price=触发价；
    - pct_chg（昨收基准）：mode=pct，price=昨收×（1+pct/100）折算静态触发价（spec-01 §2.4
      示例 le/−5% = 价 ≤ 昨收×0.95）；not_limit 可选（触发时刻未封板，方向随 op）；
    - vs_cost（持仓成本基准）：mode=cost，触发价按当日可卖持仓成本逐点折算（动态）；
    - 未实现/非法 kind 显式报错不静默。
    """
    if kind in _PRICE_KINDS:
        price = raw.get("price")
        if price is None:
            raise EngineError("trigger JSON kind 触发缺少 price")
        return "px", _PRICE_KINDS[kind], _D(price), None, False
    if kind in _PCT_KINDS:
        op = raw.get("op")
        if op not in ("le", "ge"):
            raise EngineError("trigger JSON pct_chg/vs_cost 需 op(le|ge)")
        try:
            pct = _D(raw.get("pct"))
        except Exception as exc:
            raise EngineError("trigger JSON pct 非法") from exc
        if pct == 0:
            raise EngineError("trigger JSON pct 须非 0")
        not_limit = bool(raw.get("not_limit", False))
        if kind == "pct_chg":
            if prev is None:
                raise EngineGapError(
                    "kind=pct_chg 缺少昨收基准——数据供给拒绝该账户结算（不落 invalid）"
                )
            price = (prev * (_D("100") + pct) / _D("100")).quantize(
                _MONEY, ROUND_HALF_UP
            )
            return "pct", op, price, pct, not_limit
        return "cost", op, None, pct, False
    if kind in _UNIMPL_KINDS:
        raise EngineGapError(f"trigger kind={kind} 尚未实现（spec-01 §2.4）")
    raise EngineError("trigger JSON kind 非法")


def _trigger_price(o: dict, *, prev: Decimal | None = None) -> tuple:
    """limit 单解析触发规则；market 单恒触发。返回 (mode, op, price, pct, not_limit)。

    kind 价格/涨跌幅/成本类为 spec-01 §2.4 canonical；op/price 为 legacy 兼容（历史落库单）。
    """
    raw = json.loads(o["trigger"]) if o["trigger"] else None
    if raw is None:
        return "px", None, None, None, False
    if "kind" in raw:
        return _kind_price(raw["kind"], raw, prev=prev)
    op, price = raw.get("op"), raw.get("price")
    if o["price_type"] == "market":
        if op not in ("le", "ge") or price is None:
            return "px", None, None, None, False
    if op not in ("le", "ge") or price is None:
        raise EngineError("trigger JSON 需含 op(le|ge) 与 price")
    return "px", op, _D(price), None, False


def _order_rule(o: dict, hist_high: dict[str, Decimal],
                prev: dict[str, Decimal] | None = None) -> tuple:
    """解析单条 order 的触发规则 → (mode, op, price, drop_pct, ref_high, pct, not_limit)。

    - 价格类/legacy 走 _trigger_price，drop_pct/ref_high/pct 均为 None；
    - kind=pct_chg 走昨收折算静态价（mode=pct）；kind=vs_cost 走成本动态价（mode=cost）；
    - sell_trail 解析 kind=trail 的 drop_pct，并取 hist_high[symbol]（买入以来至昨日的
      日线 high 累计最大，取值窗含当日——spec-01 §3.3）作回撤基准初值，mode 固定 'trail'
      （此时 op 为 None，三档判定按 mode 区分）。
    """
    if o["order_type"] != "sell_trail":
        pv = prev.get(o["symbol"]) if prev else None
        mode, op, trig, pct, not_limit = _trigger_price(o, prev=pv)
        return mode, op, trig, None, None, pct, not_limit
    raw = json.loads(o["trigger"]) if o["trigger"] else {}
    if raw.get("kind") != "trail":
        raise EngineError("sell_trail 单 trigger 须为 kind=trail")
    drop = _D(raw.get("drop_pct"))
    if drop <= 0:
        raise EngineError("sell_trail 单 drop_pct 须 > 0")
    return "trail", None, None, drop, _D(hist_high.get(o["symbol"])), None, False


_BOARD_LIMIT_PCT = {"main": "10", "gem": "20", "star": "20", "bj": "30", "st_main": "5"}


def _limit_px(prev_close: Decimal, board: str) -> tuple[Decimal, Decimal]:
    """板块差异化涨跌停价（spec-01 §3.6 判定表；§0 四舍五入到分，Decimal）。"""
    r = Decimal(_BOARD_LIMIT_PCT.get(board, "10")) / Decimal("100")
    up = (prev_close * (Decimal("1") + r)).quantize(_MONEY, ROUND_HALF_UP)
    down = (prev_close * (Decimal("1") - r)).quantize(_MONEY, ROUND_HALF_UP)
    return up, down


def _time_at(o: dict) -> str:
    """time 单 trigger（{"kind":"time","at":"HH:MM"}）解析；非法 → ValueError。"""
    try:
        trig = json.loads(o["trigger"] or "{}")
    except ValueError as exc:
        raise ValueError(f"time 触发器 JSON 非法: {exc}") from exc
    if trig.get("kind") != "time":
        raise ValueError("time 单 trigger.kind 须为 time")
    at = trig.get("at")
    if (not isinstance(at, str) or len(at) != 5 or at[2] != ":"
            or not at[:2].isdigit() or not at[3:].isdigit()):
        raise ValueError("time.at 须为 HH:MM（如 14:50）")
    return at


def _qty_ok(symbol: str, qty: int) -> bool:
    """申报数量规则委托 orderstore（单一事实源，spec-01 §3.7 D6）。"""
    from core.orderstore import qty_rule_ok  # noqa: PLC0415
    return qty_rule_ok(symbol, qty)


def _norm_l1(bars) -> list[tuple[str, Decimal | None, Decimal | None, Decimal | None, Decimal]]:
    """归一化 L1 分钟条：(ts, close) 或 (ts, open, high, low, close)。

    仅 close 时 OHLC 三列记 None（腾讯系分钟口径，spec-01 §3.3 close 插值路径）。
    """
    out: list[tuple[str, Decimal | None, Decimal | None, Decimal | None, Decimal]] = []
    for b in bars:
        ts = b[0]
        if len(b) >= 5:
            out.append((ts, _D(b[1]), _D(b[2]), _D(b[3]), _D(b[4])))
        else:
            out.append((ts, None, None, None, _D(b[1])))
    out.sort(key=lambda r: r[0])
    return out


def _audit_conservation(c, account_id: str, trade_date: str, *, cash_start: Decimal,
                        final_cash: Decimal, equity: Decimal, shares: Decimal, nav: Decimal,
                        close_map: dict[str, float]) -> None:
    """§6.5 对账双守恒自检（D1=A，单事务内、落账前执行）。

    式一（金额守恒）：cash_start + Σ当日成交净流（卖出 amount−费用 / 买入 −(amount+费用)，全 Decimal）
        == 期末现金，从 trades 逐笔独立推导，与引擎内存现金路径互相校验；
    式二（NAV 守恒）：期末现金 + Σ期末持仓×官方收盘价（从 holdings 独立推导）== equity，
        且 equity/shares 与落账 NAV 舍入一致。任何不平 → EngineError → 整事务回滚（带病不落账）。
    """
    flow = Decimal("0")
    rows = c.execute(
        "SELECT side, amount, fee_total FROM trades"
        " WHERE account_id=? AND settle_date=?",
        (account_id, trade_date),
    ).fetchall()
    for r in rows:
        amt = _D(r["amount"])
        fee = _D(r["fee_total"])
        flow += (amt - fee) if r["side"] == "sell" else -(amt + fee)
    if cash_start + flow != final_cash:
        raise EngineError(
            f"对账式一(金额守恒)不平 acct={account_id} {trade_date}: "
            f"期末现金 {final_cash} ≠ 期初 {cash_start} + 当日净流 {flow}"
        )
    mv = Decimal("0")
    for h in c.execute(
        "SELECT symbol, quantity FROM holdings WHERE account_id=? AND quantity > 0",
        (account_id,),
    ):
        sym = h["symbol"]
        if sym not in close_map:
            raise EngineError(f"对账式二缺 {sym} 官方收盘价——禁止虚构估值价")
        mv += _D(h["quantity"]) * _D(close_map[sym])
    if final_cash + mv != equity:
        raise EngineError(
            f"对账式二(NAV守恒)不平 acct={account_id} {trade_date}: "
            f"期末现金+持仓市值 {final_cash + mv} ≠ 引擎 equity {equity}"
        )
    if (equity / shares).quantize(_COST, ROUND_HALF_UP) != nav:
        raise EngineError(
            f"对账式二(NAV守恒)不平 acct={account_id} {trade_date}: "
            f"精确净值 {equity / shares} 与落账 nav {nav} 舍入不一致"
        )


def settle_exits(
    state,
    account_id: str,
    trade_date: str,
    *,
    market: dict[str, dict] | None = None,
    track_days: int | None = None,
) -> dict:
    """每日推进卖出跟踪并到期结清（spec-01 §8.1；幂等，单事务，不动撮合账务）。

    market[sym] = {"close": float, "high": float, "low": float}——当日官方收盘及日内
    高低（缺 high/low 时以 close 记极值）。基准指数以 market["000300"]（EXIT_BENCH_SYMBOL）
    提供，未提供时 excess 无法计算：结清仍按个股 fwd 判结论，quality 标 no_bench。

    推进规则：跟踪行仅在其卖出日**之后**的结算日被推进（当日卖出行由本函数排除，避免把
    卖出前盘中极值计入窗口）；每推进一日刷新 period_high/period_low 与最近可得价。自卖出
    日起第 track_days（默认 EXIT_DEFAULT_DAYS=10）个结算日志日后结清——取当日官方收盘价；
    当日缺价但此前有最近可得价 → 按最近价 stale 结清并标 quality=stale_close；两源皆缺时
    保持 tracking 不虚构价。回归安全：settle_log 日序计数（同键幂等）驱动，非墙钟天数。
    """
    n = int(track_days or EXIT_DEFAULT_DAYS)
    md = market or {}
    conn = state_conn(state)
    updated = closed = 0
    with write_txn(conn) as c:
        rows = c.execute(
            """
            SELECT * FROM exit_trackings
             WHERE account_id=? AND status='tracking' AND sell_date < ?
             ORDER BY sell_date ASC, id ASC
            """,
            (account_id, trade_date),
        ).fetchall()
        for r in rows:
            sym = r["symbol"]
            day = md.get(sym) or {}
            close = _D(day["close"]) if "close" in day else None
            high = _D(day["high"]) if "high" in day else None
            low = _D(day["low"]) if "low" in day else None
            if high is None or low is None:
                high = low = close
            bd = md.get(EXIT_BENCH_SYMBOL) or {}
            bclose = _D(bd["close"]) if "close" in bd else None
            if close is None and bclose is None:
                continue                     # 本日个股与基准双缺：不推进、不虚构价
            sessions = c.execute(
                """
                SELECT COUNT(DISTINCT trade_date) AS n FROM settlement_log
                 WHERE account_id=? AND trade_date > ? AND trade_date <= ?
                """,
                (account_id, r["sell_date"], trade_date),
            ).fetchone()["n"]
            if sessions > 0:                 # 非卖出当日 → 推进极值与最近价
                ph = _D(r["period_high"])
                pl = _D(r["period_low"])
                if close is not None:
                    np_high = high if high is not None else close
                    np_low = low if low is not None else close
                    if np_high > ph:
                        ph = np_high
                    if np_low < pl:
                        pl = np_low
                    c.execute(
                        "UPDATE exit_trackings SET period_high=?, period_low=?, last_close=? WHERE id=?",
                        (_q(ph), _q(pl), _q(close), r["id"]),
                    )
                if bclose is not None:
                    c.execute(
                        "UPDATE exit_trackings SET last_bench=? WHERE id=?",
                        (_q(bclose), r["id"]),
                    )
                updated += 1
            if sessions < n:
                continue                     # 未到结清日：继续 tracking
            sp = _D(r["sell_price"])
            if close is not None:
                ev = close
                stale = False
            elif _D(r["last_close"]) > 0:
                ev = _D(r["last_close"])
                stale = True
            else:
                continue                     # 到期但无任何可得价：保持 tracking（停牌挂起）
            base_b = _D(r["bench_sell_close"])
            bnow = bclose if bclose is not None else (_D(r["last_bench"]) if _D(r["last_bench"]) > 0 else None)
            fwd = ((ev - sp) / sp * Decimal("100")).quantize(_COST, ROUND_HALF_UP)
            bench = excess = None
            if base_b > 0 and bnow is not None and bnow > 0:
                bench = ((bnow - base_b) / base_b * Decimal("100")).quantize(_COST, ROUND_HALF_UP)
                excess = (fwd - bench).quantize(_COST, ROUND_HALF_UP)
            ok = fwd <= _EXIT_OK_PCT or (excess is not None and excess <= _EXIT_OK_PCT)
            early = fwd >= _EXIT_EARLY_PCT or (excess is not None and excess >= _EXIT_EARLY_PCT)
            conclusion = "卖早" if early else ("卖对" if ok else "卖平")
            is_loss = int(fwd > 0 or (excess is not None and excess > 0))
            qual = []
            if stale:
                qual.append("stale_close")
            if excess is None:
                qual.append("no_bench")
            c.execute(
                """
                UPDATE exit_trackings
                   SET status='done', track_end_date=?, fwd_return_pct=?, bench_return_pct=?,
                       excess_pct=?, conclusion=?, is_loss_case=?, quality=?, done_ts=?
                 WHERE id=?
                """,
                (
                    trade_date, _q(fwd),
                    _q(bench) if bench is not None else _q(Decimal("0")),
                    _q(excess) if excess is not None else _q(Decimal("0")),
                    conclusion, is_loss, "+".join(qual), _now(), r["id"],
                ),
            )
            closed += 1
    return {"account_id": account_id, "trade_date": trade_date, "updated": updated,
            "closed": closed}


def settle_account(
    state,
    account_id: str,
    trade_date: str,
    *,
    series_map: dict[str, list[tuple[str, float]]] | None = None,
    l1_map: dict[str, list[tuple]] | None = None,
    l2_map: dict[str, dict] | None = None,
    close_map: dict[str, float],
    prev_close_map: dict[str, float] | None = None,
    hist_high_map: dict[str, float] | None = None,
    board_map: dict[str, str] | None = None,
    restrict_map: dict[str, str] | None = None,
    fee: dict | None = None,
    exit_market: dict[str, dict] | None = None,
) -> dict:
    """对单个账户执行一日 EOD 结算（单 SQLite 事务原子写入）。

    series_map[symbol] = [(本地墙钟 naive ISO, price), ...]（升序 L0 采样点）。
    l1_map[symbol] = [(本地墙钟 naive ISO, close), ...] 或 [(ts, open, high, low, close), ...]
    （升序 1 分钟条，spec-01 §3.3 L1 判定）。
    l2_map[symbol] = {"high": float, "low": float}（当日日线区间，spec-01 §3.3 L2：
    区间触达 + 官方收盘价成交，供历史日/分钟不可得票）。同一 symbol 在三个 map 中至多出现其一。
    close_map[symbol] = 当日官方收盘价；prev_close_map[symbol] = 前一日官方收盘价。
    hist_high_map[symbol] = 该票买入以来至前一交易日的日线 high 累计最大（spec-01 §3.3
    移动止盈回撤基准取值窗含买入当日；缺省该票 trail 以当日窗口起判）。
    board_map[symbol] = 板块（main|gem|star|bj|st_main，spec-01 §3.6 判定表）；仅当
    board_map 显式给出且 prev_close_map 含该票时启用涨跌停封死例外（§3.5），否则不设涨停限制。
    restrict_map[symbol] = 当日风险标记（st=风险警示 ST、ipo=注册制新股上市 5 日内；逗号或空格
    分隔可多标）。命中且账户 accounts.buy_exempt（JSON token 数组，需审批）未豁免 → 买入单开盘前
    invalid（restricted_buy），见模块 doc。
    exit_market[sym] = {"close": float, "high": float, "low": float}：卖出登记时缓存当日基准
    指数（key=EXIT_BENCH_SYMBOL "000300"）与个股官方收盘（用于卖出价偏差侧极端计），仅登记
    期用；每日跟踪推进由 settle_exits 单独调用（见模块 doc §8.1）。
    """
    series_map = series_map or {}
    l1_map = {sym: _norm_l1(bars) for sym, bars in (l1_map or {}).items()}
    l2_map = l2_map or {}
    seen = set(series_map) | set(l1_map)
    for sym in l2_map:
        if sym in seen:
            raise EngineError(f"symbol {sym} 同时提供多档序列——档位须按票唯一")
    seen |= set(l2_map)

    def feed_kind(symbol: str) -> str:
        if symbol in l1_map:
            return "l1"
        if symbol in l2_map:
            return "l2"
        return "l0"

    def feed_last_price(symbol: str) -> Decimal:
        if symbol in l1_map:
            return l1_map[symbol][-1][4]
        if symbol in l2_map:
            return _D(close_map[symbol])
        return _D(series_map[symbol][-1][1])

    def cost_basis(symbol: str) -> Decimal | None:
        """可卖持仓成本 = Σ(lot.buy_price×remaining)/Σremaining（buy_date<今日，T+1 口径）。

        用作 vs_cost（spec-01 §2.4）的逐点触发基准；仅统计当日可卖 lot，避免当日新买
        批次污染成本基准。无剩余可卖 lot 时返回 None（该点视为不触达）。
        """
        row = c.execute(
            """
            SELECT COALESCE(SUM(l.buy_price * l.remaining), 0) AS w,
                   COALESCE(SUM(l.remaining), 0) AS t
              FROM lots l JOIN holdings h ON h.id = l.holding_id
             WHERE l.account_id=? AND h.symbol=? AND l.buy_date < ? AND l.remaining > 0
            """,
            (account_id, symbol, trade_date),
        ).fetchone()
        t = _D(row["t"])
        if t <= 0:
            return None
        return _D(row["w"]) / t

    def cost_trigger_price(symbol: str, pct: Decimal) -> Decimal | None:
        """vs_cost 动态折算价；无成本基准 → None。"""
        base = cost_basis(symbol)
        if base is None or base <= 0:
            return None
        return base * (_D("100") + pct) / _D("100")
    f = {k: Decimal(str(v)) for k, v in (fee or FEES).items()}
    prev = {k: _D(v) for k, v in (prev_close_map or {}).items()}
    hist_high = {k: _D(v) for k, v in (hist_high_map or {}).items()}
    boards = board_map or {}
    exit_market = exit_market or {}

    def lock_limits(symbol: str):
        """涨跌停封死判定专用：board_map 显式含该票且 prev_close 可得才返回 (up, down)。"""
        if not boards:
            return None
        b = boards.get(symbol)
        if not b:
            return None
        pc = prev.get(symbol)
        if pc is None:
            return None
        return _limit_px(pc, b)
    conn = state_conn(state)
    now = _now()
    settle_key = f"{trade_date}:{account_id}"

    with write_txn(conn) as c:
        acct = c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if acct is None:
            raise EngineError("账户不存在")
        if acct["granularity"] != "eod_replay":
            raise EngineGapError(f"账户粒度 {acct['granularity']} 超出 EOD 引擎支持范围")
        shares = _D(acct["shares"])
        if shares <= 0:
            raise EngineError("份额须 > 0")
        version_no = acct["active_version_no"] or ""

        raw_exempt = acct["buy_exempt"] if "buy_exempt" in acct.keys() else "[]"
        try:
            buy_exempt = {str(t) for t in json.loads(raw_exempt or "[]")}
        except ValueError as exc:
            raise EngineError("账户 buy_exempt 配置非法（须 JSON token 数组）") from exc
        restrict_syms: dict[str, set[str]] = {}
        for sym, tok in (restrict_map or {}).items():
            toks = {t.strip().lower() for t in str(tok or "").split(",")}
            keep = {t for t in toks if t in ("st", "ipo")}
            if keep:
                restrict_syms[sym] = keep

        if c.execute(
            "SELECT 1 FROM settlement_log WHERE settle_key=?", (settle_key,)
        ).fetchone():
            return {"already_settled": True, "account_id": account_id, "trade_date": trade_date}

        orders = c.execute(
            """
            SELECT * FROM condition_orders
             WHERE account_id=? AND status='active'
             ORDER BY created_at ASC, id ASC
            """,
            (account_id,),
        ).fetchall()
        active: list[dict] = []
        for o in orders:
            if o["order_type"] not in SUPPORTED_ORDER_TYPES:
                raise EngineGapError(f"order {o['id']} type={o['order_type']} 未实现")
            if o["scope"] != "single" or o["basis"] != "replay_l0":
                raise EngineGapError(f"order {o['id']} scope/basis 超出支持范围")
            if o["symbol"] not in series_map and o["symbol"] not in l1_map \
                    and o["symbol"] not in l2_map:
                raise EngineError(f"order {o['id']} 标的 {o['symbol']} 缺少当日序列")
            o = dict(o)
            if o["order_type"] == "sell_open_board" and feed_kind(o["symbol"]) != "l0":
                # 开板事件类仅 L0 有效（spec-01 §4.1 矩阵 / §3.5）：L1/L2 → 显式 invalid
                o["status"] = "invalid"
                o["invalid_reason"] = "basis_requires_l0"
            active.append(o)

        cash_start = _D(acct["cash"])    # 事务内期初快照（today_pnl 基准）
        cash = cash_start
        initial_capital = _D(acct["initial_capital"])

        # ---- 开盘前静态校验：买入数量规则 → invalid（qty_rule，§3.7）----
        for o in active:
            if o["direction"] == "buy":
                try:
                    qty = int(Decimal(str(o["qty"])))
                except Exception as exc:
                    raise EngineError(f"order {o['id']} qty 非法: {exc}") from exc
                if not _qty_ok(o["symbol"], qty):
                    o["status"] = "invalid"
                    o["invalid_reason"] = "qty_rule"

        # 开盘前静态校验：ST/新股买入拦截 → invalid（restricted_buy，§3.6）
        for o in active:
            if o["order_type"] != "buy":
                continue
            toks = restrict_syms.get(o["symbol"], set()) - buy_exempt
            if toks:
                o["status"] = "invalid"
                o["invalid_reason"] = "restricted_buy:" + ",".join(sorted(toks))

        granularity_used: dict[str, str] = {}

        def require_close(symbol: str) -> Decimal:
            if symbol not in close_map:
                raise EngineError(f"{symbol} 缺少官方收盘价——禁止以替代价虚构收盘价")
            if symbol in series_map or symbol in l1_map or symbol in l2_map:
                granularity_used[symbol] = feed_kind(symbol)
            return _D(close_map[symbol])

        held_init = c.execute(
            "SELECT symbol, quantity FROM holdings WHERE account_id=?", (account_id,)
        ).fetchall()
        held_qty: dict[str, Decimal] = {r["symbol"]: _D(r["quantity"]) for r in held_init}

        def holding(symbol: str, delta: Decimal, *, cost_basis: Decimal | None = None) -> str:
            row = c.execute(
                "SELECT id, quantity, avg_cost FROM holdings WHERE account_id=? AND symbol=?",
                (account_id, symbol),
            ).fetchone()
            if row is None:
                hid = "h" + secrets.token_hex(10)
                c.execute(
                    "INSERT INTO holdings(id, account_id, symbol, quantity, avg_cost, updated_ts)"
                    " VALUES (?,?,?,?,?,?)",
                    (hid, account_id, symbol, _q(delta), _q(cost_basis or Decimal("0")), now),
                )
                return hid
            new_qty = _D(row["quantity"]) + delta
            if delta > 0 and cost_basis is not None:
                new_avg = cost_basis / new_qty if new_qty else Decimal("0")
            else:
                new_avg = _D(row["avg_cost"])
            c.execute(
                "UPDATE holdings SET quantity=?, avg_cost=?, updated_ts=? WHERE id=?",
                (_q(new_qty), _q(new_avg.quantize(_COST, ROUND_HALF_UP)), now, row["id"]),
            )
            return row["id"]

        def write_trade(o: dict, side: str, qty: int, price: Decimal, ts: str, fees: dict,
                        *, quality: str = "", basis_used: str | None = None) -> str:
            tid = "t" + secrets.token_hex(10)
            amount = (price * qty).quantize(_MONEY, ROUND_HALF_UP)
            c.execute(
                """
                INSERT INTO trades(id, account_id, order_id, symbol, side, qty, price, amount,
                    fee_total, commission, stamp_tax, transfer_fee, trade_time, basis_requested,
                    basis_used, quality, settle_date, reason, strategy_version_no)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    tid, account_id, o["id"], o["symbol"], side, qty, _q(price), _q(amount),
                    _q(fees["total"]), _q(fees["commission"]), _q(fees["stamp_tax"]),
                    _q(fees["transfer_fee"]), ts, o["basis"] or "replay_l0",
                    basis_used or feed_kind(o["symbol"]), quality, trade_date,
                    o["reason"], o["strategy_version_no"] or version_no,
                ),
            )
            return tid

        # 内存运行态：insufficient 计数 / 终态（写盘集中在事务收尾）
        runtime = {o["id"]: {"ins": o["insufficient_events"], "final": "active"} for o in active}
        ever_pinned: dict[str, bool] = {}

        def register_exit(o: dict, tid: str, qty: int, price: Decimal) -> None:
            """§8.1 卖出成交登记：每笔卖出 trade 一行，卖出日基准指数收盘随行缓存。"""
            reason = str(o.get("reason") or "").strip()
            if reason not in _EXIT_REASONS:
                reason = "主动"
            bd = exit_market.get(EXIT_BENCH_SYMBOL) or {}
            bsl = _D(bd["close"]) if "close" in bd else Decimal("0")
            sp = price.quantize(_MONEY, ROUND_HALF_UP)
            c.execute(
                """
                INSERT INTO exit_trackings(id, account_id, sell_trade_id, symbol, sell_date,
                    sell_price, qty, sell_reason, status, track_end_date, fwd_return_pct,
                    bench_return_pct, excess_pct, period_high, period_low, conclusion,
                    is_loss_case, bench_sell_close, last_close, last_bench, quality,
                    created_ts, done_ts)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "x" + secrets.token_hex(10), account_id, tid, o["symbol"], trade_date,
                    _q(sp), qty, reason, "tracking", "", _q(Decimal("0")), _q(Decimal("0")),
                    _q(Decimal("0")), _q(sp), _q(sp), "", 0, _q(bsl), _q(sp),
                    _q(Decimal("0")), "", now, "",
                ),
            )

        def sell_fill(o: dict, qty: int, p: Decimal, ts: str, *, oid: str) -> None:
            """卖出撮合公共路径（L0/开板共用）：T+1 可卖校验、现金、FIFO 核销、清仓删除。"""
            nonlocal cash
            sellable = c.execute(
                """
                SELECT COALESCE(SUM(l.remaining), 0) AS s
                  FROM lots l JOIN holdings h ON h.id = l.holding_id
                 WHERE l.account_id=? AND h.symbol=? AND l.buy_date < ?
                """,
                (account_id, o["symbol"], trade_date),
            ).fetchone()["s"]
            if _D(sellable) < qty:         # 含 T+1 未到期：记事件继续
                runtime[oid]["ins"] += 1
                return
            amount = (p * qty).quantize(_MONEY, ROUND_HALF_UP)
            fz = _fees(amount, side="sell", fee=f)
            cash += amount - fz["total"]
            tid = write_trade(o, "sell", qty, p, ts, fz)
            rem = qty
            lots = c.execute(
                """
                SELECT l.id, l.remaining FROM lots l
                  JOIN holdings h ON h.id = l.holding_id
                 WHERE l.account_id=? AND h.symbol=? AND l.buy_date < ? AND l.remaining > 0
                 ORDER BY l.buy_date ASC, l.id ASC
                """,
                (account_id, o["symbol"], trade_date),
            ).fetchall()
            for lot in lots:
                if rem <= 0:
                    break
                take = min(int(_D(lot["remaining"])), rem)
                c.execute(
                    "UPDATE lots SET remaining=remaining-? WHERE id=?",
                    (take, lot["id"]),
                )
                rem -= take
            if rem:
                raise EngineError(f"卖出 FIFO 核销不一致 {o['symbol']} rem={rem}")
            held_qty[o["symbol"]] -= qty
            holding(o["symbol"], Decimal(-qty))
            if held_qty[o["symbol"]] <= 0:
                c.execute(
                    """
                    DELETE FROM lots WHERE holding_id IN
                      (SELECT id FROM holdings WHERE account_id=? AND symbol=?)
                    """,
                    (account_id, o["symbol"]),
                )
                c.execute(
                    "DELETE FROM holdings WHERE account_id=? AND symbol=?",
                    (account_id, o["symbol"]),
                )
                del held_qty[o["symbol"]]
            runtime[oid]["final"] = "filled"
            register_exit(o, tid, qty, p)

        def buy_fill(o: dict, qty: int, p: Decimal, ts: str, *, oid: str) -> None:
            """买入撮合公共路径（L0/定时共用）：资金校验、费用、持仓/lot 落账与摊薄。"""
            nonlocal cash
            amount = (p * qty).quantize(_MONEY, ROUND_HALF_UP)
            fz = _fees(amount, side="buy", fee=f)
            if cash < amount + fz["total"]:      # 资金不足：记事件继续（§2.3）
                runtime[oid]["ins"] += 1
                return
            cash -= amount + fz["total"]
            tid = write_trade(o, "buy", qty, p, ts, fz)
            hid = holding(o["symbol"], Decimal(qty), cost_basis=None)
            amt_with_fee = amount + fz["total"]
            cur = c.execute("SELECT quantity FROM holdings WHERE id=?", (hid,)).fetchone()
            avg = (amt_with_fee / _D(cur["quantity"])).quantize(_COST, ROUND_HALF_UP)
            c.execute("UPDATE holdings SET avg_cost=? WHERE id=?", (_q(avg), hid))
            c.execute(
                """
                INSERT INTO lots(id, account_id, holding_id, buy_trade_id, buy_date,
                    buy_price, quantity, remaining, strategy_version_no, corp_action_flags)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "l" + secrets.token_hex(10), account_id, hid, tid, trade_date,
                    _q(p.quantize(_COST, ROUND_HALF_UP)), qty, qty,
                    o["strategy_version_no"] or version_no, "[]",
                ),
            )
            held_qty[o["symbol"]] = held_qty.get(o["symbol"], Decimal("0")) + qty
            runtime[oid]["final"] = "filled"

        # ---- L0 逐票回放（§3.2）----
        for symbol in sorted({o["symbol"] for o in active}):
            if feed_kind(symbol) != "l0":
                continue
            ser = series_map[symbol]
            for o in active:
                if o["symbol"] != symbol or o["status"] != "active":
                    continue
                oid = o["id"]
                mode = op = trig = trail_drop = ref_high = pct = not_limit = None
                if o["order_type"] not in ("sell_open_board", "time"):
                    try:
                        mode, op, trig, trail_drop, ref_high, pct, not_limit = _order_rule(
                            o, hist_high, prev
                        )
                    except EngineGapError:
                        raise
                    except (json.JSONDecodeError, EngineError):
                        runtime[oid]["final"] = "invalid"
                        continue
                at = None
                if o["order_type"] == "time":
                    try:
                        at = _time_at(o)
                    except ValueError:
                        runtime[oid]["final"] = "invalid"
                        continue
                qty = int(Decimal(str(o["qty"])))
                lim = lock_limits(o["symbol"])
                for ts, pv in ser:
                    if o["created_at"] and o["created_at"] >= ts:
                        continue                      # #38 防前视
                    if runtime[oid]["final"] != "active":
                        break
                    p = _D(pv)
                    if o["order_type"] == "sell_open_board":
                        if lim is None:
                            continue
                        if p == lim[0]:               # 曾封板（触及涨停价，§3.5）
                            ever_pinned[oid] = True
                            continue
                        if not ever_pinned.get(oid) or p > lim[0]:
                            continue
                        sell_fill(o, qty, p, ts, oid=oid)   # 首次开板采样点价成交
                        if runtime[oid]["final"] == "filled":
                            break
                        continue
                    if o["order_type"] == "time":
                        if ts[11:16] < at:
                            continue
                        if o["direction"] == "buy":
                            buy_fill(o, qty, p, ts, oid=oid)
                        else:
                            sell_fill(o, qty, p, ts, oid=oid)
                        break                        # 定时单一次性判定（资金/可卖不足亦不复判）
                    if mode == "trail":
                        ref_high = max(ref_high, p)
                        if ref_high <= 0 or p > ref_high * (_D("100") - trail_drop) / _D("100"):
                            continue
                    elif mode == "cost":
                        X = cost_trigger_price(o["symbol"], pct)
                        if X is None:
                            continue                  # 无可卖成本基准：不触达（保持 active）
                        if (op == "le" and p > X) or (op == "ge" and p < X):
                            continue
                        if not_limit and lim is not None:
                            if p == (lim[1] if op == "le" else lim[0]):
                                continue              # 触发时刻恰好封板：不触达（§2.4 not_limit）
                    elif trig is not None:
                        if op == "le" and p > trig:
                            continue
                        if op == "ge" and p < trig:
                            continue
                        if not_limit and lim is not None:
                            if p == (lim[1] if op == "le" else lim[0]):
                                continue              # 触发时刻恰好封板：不触达（§2.4 not_limit）
                    if lim is not None and p == (lim[0] if o["order_type"] == "buy" else lim[1]):
                        continue                    # 涨停封死买不成交 / 跌停封死卖不成交（§3.5）
                    if o["order_type"] == "buy":
                        buy_fill(o, qty, p, ts, oid=oid)
                        if runtime[oid]["final"] == "filled":
                            break
                        continue
                    else:
                        sell_fill(o, qty, p, ts, oid=oid)
                        if runtime[oid]["final"] == "filled":
                            break
                        continue

        # ---- L1 档逐票回放（spec-01 §3.3：相邻分钟确认 + 按条件价 X 成交）----
        for symbol in sorted({o["symbol"] for o in active}):
            if feed_kind(symbol) != "l1":
                continue
            bars = l1_map[symbol]
            for o in active:
                if o["symbol"] != symbol or o["status"] != "active":
                    continue
                oid = o["id"]
                mode = op = trig = trail_drop = ref_high = pct = not_limit = None
                if o["order_type"] not in ("sell_open_board", "time"):
                    try:
                        mode, op, trig, trail_drop, ref_high, pct, not_limit = _order_rule(
                            o, hist_high, prev
                        )
                    except EngineGapError:
                        raise
                    except (json.JSONDecodeError, EngineError):
                        runtime[oid]["final"] = "invalid"
                        continue
                at = None
                if o["order_type"] == "time":
                    try:
                        at = _time_at(o)
                    except ValueError:
                        runtime[oid]["final"] = "invalid"
                        continue
                if mode not in ("trail", "cost") and trig is None and at is None:
                    raise EngineGapError(
                        f"order {o['id']} L1 档 price_type=market 无价单属未定义语义（§3.3）"
                    )
                qty = int(Decimal(str(o["qty"])))
                lim = lock_limits(o["symbol"])
                touched_prev = False           # 前一根分钟是否已处触达态
                prev_failed = False            # 上一根触达分钟资金/可卖不足（可复判 §2.3）
                seen_after = False             # 是否已越过 created_at（首根可判分钟不要求“穿越”）
                for ts, bopen, bhigh, blow, bclose in bars:
                    if o["order_type"] == "time":
                        if o["created_at"] and o["created_at"] >= ts:
                            continue                      # #38 防前视
                        if ts[11:16] < at:
                            continue
                        p = bclose if bclose is not None else Decimal("0")
                        if o["direction"] == "buy":
                            buy_fill(o, qty, p, ts, oid=oid)
                        else:
                            sell_fill(o, qty, p, ts, oid=oid)
                        break                            # 定时单一次性判定（同 L0 口径）
                    if mode == "trail":
                        high_px = bhigh if bhigh is not None else bclose
                        if high_px is not None and high_px > ref_high:
                            ref_high = high_px
                        if ref_high > 0:
                            X = ref_high * (_D("100") - trail_drop) / _D("100")
                            probe = blow if blow is not None else bclose
                            touched = probe is not None and probe <= X
                        else:
                            X = Decimal("0")
                            touched = False
                    else:
                        if mode == "cost":
                            X = cost_trigger_price(o["symbol"], pct)
                            touched = False
                            if X is not None:
                                if op == "le":
                                    touched = bclose <= X or (blow is not None and blow <= X)
                                else:
                                    touched = bclose >= X or (bhigh is not None and bhigh >= X)
                        else:
                            X = trig
                            touched = False
                            if X is not None:
                                if op == "le":
                                    touched = bclose <= X or (blow is not None and blow <= X)
                                else:
                                    touched = bclose >= X or (bhigh is not None and bhigh >= X)
                    if lim is not None and bhigh is not None and blow is not None:
                        lv = lim[0] if o["order_type"] == "buy" else lim[1]
                        if bhigh == lv and blow == lv:
                            touched = False     # 整分钟封死（一字段）：买/卖不成交，开板分钟恢复（§3.5）
                    if not_limit and lim is not None and bhigh is not None and blow is not None:
                        lv = lim[1] if op == "le" else lim[0]
                        if bhigh == lv and blow == lv:
                            touched = False     # 整分钟封死方向板：pct_chg 触发要求未封板（§2.4）
                    is_after = not (o["created_at"] and o["created_at"] >= ts)
                    if is_after and touched and (not seen_after or not touched_prev or prev_failed):
                        p = X
                        quality = ""
                        if o["order_type"] == "sell_trail":
                            quality = "degraded"
                        if ts.endswith("T15:00:00"):
                            cl = require_close(symbol)
                            if o["order_type"] == "buy":
                                p = max(X, cl)
                            else:
                                p = min(X, cl)
                            quality = "close_minute_fill"
                        if o["order_type"] == "buy":
                            amount = (p * qty).quantize(_MONEY, ROUND_HALF_UP)
                            fz = _fees(amount, side="buy", fee=f)
                            if cash < amount + fz["total"]:
                                runtime[oid]["ins"] += 1
                                prev_failed = True
                            else:
                                cash -= amount + fz["total"]
                                tid = write_trade(o, "buy", qty, p, ts, fz, quality=quality)
                                hid = holding(o["symbol"], Decimal(qty), cost_basis=None)
                                amt_with_fee = amount + fz["total"]
                                cur = c.execute(
                                    "SELECT quantity FROM holdings WHERE id=?", (hid,)
                                ).fetchone()
                                avg = (amt_with_fee / _D(cur["quantity"])).quantize(
                                    _COST, ROUND_HALF_UP
                                )
                                c.execute("UPDATE holdings SET avg_cost=? WHERE id=?", (_q(avg), hid))
                                c.execute(
                                    """
                                    INSERT INTO lots(id, account_id, holding_id, buy_trade_id,
                                        buy_date, buy_price, quantity, remaining,
                                        strategy_version_no, corp_action_flags)
                                    VALUES (?,?,?,?,?,?,?,?,?,?)
                                    """,
                                    (
                                        "l" + secrets.token_hex(10), account_id, hid, tid,
                                        trade_date, _q(p.quantize(_COST, ROUND_HALF_UP)), qty, qty,
                                        o["strategy_version_no"] or version_no, "[]",
                                    ),
                                )
                                held_qty[o["symbol"]] = held_qty.get(o["symbol"], Decimal("0")) + qty
                                runtime[oid]["final"] = "filled"
                                break
                        else:
                            sellable = c.execute(
                                """
                                SELECT COALESCE(SUM(l.remaining), 0) AS s
                                  FROM lots l JOIN holdings h ON h.id = l.holding_id
                                 WHERE l.account_id=? AND h.symbol=? AND l.buy_date < ?
                                """,
                                (account_id, o["symbol"], trade_date),
                            ).fetchone()["s"]
                            if _D(sellable) < qty:
                                runtime[oid]["ins"] += 1
                                prev_failed = True
                            else:
                                amount = (p * qty).quantize(_MONEY, ROUND_HALF_UP)
                                fz = _fees(amount, side="sell", fee=f)
                                cash += amount - fz["total"]
                                tid = write_trade(o, "sell", qty, p, ts, fz, quality=quality)
                                rem = qty
                                lots = c.execute(
                                    """
                                    SELECT l.id, l.remaining FROM lots l
                                      JOIN holdings h ON h.id = l.holding_id
                                     WHERE l.account_id=? AND h.symbol=? AND l.buy_date < ?
                                       AND l.remaining > 0
                                     ORDER BY l.buy_date ASC, l.id ASC
                                    """,
                                    (account_id, o["symbol"], trade_date),
                                ).fetchall()
                                for lot in lots:
                                    if rem <= 0:
                                        break
                                    take = min(int(_D(lot["remaining"])), rem)
                                    c.execute(
                                        "UPDATE lots SET remaining=remaining-? WHERE id=?",
                                        (take, lot["id"]),
                                    )
                                    rem -= take
                                if rem:
                                    raise EngineError(
                                        f"卖出 FIFO 核销不一致 {o['symbol']} rem={rem}"
                                    )
                                held_qty[o["symbol"]] -= qty
                                holding(o["symbol"], Decimal(-qty))
                                if held_qty[o["symbol"]] <= 0:
                                    c.execute(
                                        """
                                        DELETE FROM lots WHERE holding_id IN
                                          (SELECT id FROM holdings WHERE account_id=? AND symbol=?)
                                        """,
                                        (account_id, o["symbol"]),
                                    )
                                    c.execute(
                                        "DELETE FROM holdings WHERE account_id=? AND symbol=?",
                                        (account_id, o["symbol"]),
                                    )
                                    del held_qty[o["symbol"]]
                                runtime[oid]["final"] = "filled"
                                break
                    touched_prev = touched
                    if not touched:
                        prev_failed = False
                    seen_after = seen_after or is_after

        # ---- L2 档逐票回放（spec-01 §3.3/§4.1：日线区间触达 + 官方收盘价成交；历史日档）----
        for symbol in sorted({o["symbol"] for o in active}):
            if feed_kind(symbol) != "l2":
                continue
            hi = _D(l2_map[symbol]["high"])
            lo = _D(l2_map[symbol]["low"])
            close_ts = f"{trade_date}T15:00:00"
            for o in active:
                if o["symbol"] != symbol or o["status"] != "active":
                    continue
                oid = o["id"]
                mode = op = trig = trail_drop = ref_high = pct = not_limit = None
                if o["order_type"] not in ("sell_open_board", "time"):
                    try:
                        mode, op, trig, trail_drop, ref_high, pct, not_limit = _order_rule(
                            o, hist_high, prev
                        )
                    except EngineGapError:
                        raise
                    except (json.JSONDecodeError, EngineError):
                        runtime[oid]["final"] = "invalid"
                        continue
                at = None
                if o["order_type"] == "time":
                    try:
                        at = _time_at(o)
                    except ValueError:
                        runtime[oid]["final"] = "invalid"
                        continue
                if mode not in ("trail", "cost") and trig is None and at is None:
                    raise EngineGapError(
                        f"order {o['id']} L2 档 price_type=market 无价单属未定义语义（§3.3）"
                    )
                qty = int(Decimal(str(o["qty"])))
                lim = lock_limits(o["symbol"])
                if o["created_at"] and o["created_at"] >= close_ts:
                    continue                      # 收盘后创建的 order 不参与当日判定（#38）
                if o["order_type"] == "time":
                    # L2 为日线近似档：无日内分钟 → 定时单按官方收盘价成交近似（§4.1 时间行）
                    p = require_close(symbol)
                    if o["direction"] == "buy":
                        buy_fill(o, qty, p, close_ts, oid=oid)
                    else:
                        sell_fill(o, qty, p, close_ts, oid=oid)
                    continue
                trail_quality = ""
                if mode == "trail":
                    # spec-01 §3.3 L2 移动止盈：触达 = 当日 low ≤ 参考高点×(1−drop_pct)，
                    # 参考高点 = max(买入以来日线 high 累计最大, 当日 high)
                    ref_high = max(ref_high, hi)
                    if ref_high <= 0 or lo > ref_high * (_D("100") - trail_drop) / _D("100"):
                        continue
                    trail_quality = "degraded"
                else:
                    if mode == "cost":
                        X = cost_trigger_price(o["symbol"], pct)
                    else:
                        X = trig
                    if X is None or ((op == "le" and lo > X) or (op == "ge" and hi < X)):
                        continue                  # 未触达：保持 active（today → expired）
                if lim is not None and hi == lo:
                    lv = lim[0] if o["order_type"] == "buy" else lim[1]
                    if hi == lv:
                        continue                  # 一字封死全天（日线近似口径）：买/卖不成交（§3.5）
                if not_limit and lim is not None and hi == lo:
                    lv = lim[1] if op == "le" else lim[0]
                    if hi == lv:
                        continue                  # 一字封死方向板：pct_chg 要求未封板（§2.4）
                p = require_close(symbol)         # L2 恒以官方收盘价成交
                if o["order_type"] == "buy":
                    amount = (p * qty).quantize(_MONEY, ROUND_HALF_UP)
                    fz = _fees(amount, side="buy", fee=f)
                    if cash < amount + fz["total"]:
                        runtime[oid]["ins"] += 1
                        continue                  # 收盘无现金 → 记 insufficient，不再有采样点
                    cash -= amount + fz["total"]
                    tid = write_trade(o, "buy", qty, p, close_ts, fz, quality=trail_quality)
                    hid = holding(o["symbol"], Decimal(qty), cost_basis=None)
                    amt_with_fee = amount + fz["total"]
                    cur = c.execute(
                        "SELECT quantity FROM holdings WHERE id=?", (hid,)
                    ).fetchone()
                    avg = (amt_with_fee / _D(cur["quantity"])).quantize(
                        _COST, ROUND_HALF_UP
                    )
                    c.execute("UPDATE holdings SET avg_cost=? WHERE id=?", (_q(avg), hid))
                    c.execute(
                        """
                        INSERT INTO lots(id, account_id, holding_id, buy_trade_id,
                            buy_date, buy_price, quantity, remaining,
                            strategy_version_no, corp_action_flags)
                        VALUES (?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            "l" + secrets.token_hex(10), account_id, hid, tid,
                            trade_date, _q(p.quantize(_COST, ROUND_HALF_UP)), qty, qty,
                            o["strategy_version_no"] or version_no, "[]",
                        ),
                    )
                    held_qty[o["symbol"]] = held_qty.get(o["symbol"], Decimal("0")) + qty
                    runtime[oid]["final"] = "filled"
                else:
                    sellable = c.execute(
                        """
                        SELECT COALESCE(SUM(l.remaining), 0) AS s
                          FROM lots l JOIN holdings h ON h.id = l.holding_id
                         WHERE l.account_id=? AND h.symbol=? AND l.buy_date < ?
                        """,
                        (account_id, o["symbol"], trade_date),
                    ).fetchone()["s"]
                    if _D(sellable) < qty:        # T+1 未到期/无持仓 → 记 insufficient
                        runtime[oid]["ins"] += 1
                        continue
                    amount = (p * qty).quantize(_MONEY, ROUND_HALF_UP)
                    fz = _fees(amount, side="sell", fee=f)
                    cash += amount - fz["total"]
                    tid = write_trade(o, "sell", qty, p, close_ts, fz, quality=trail_quality)
                    rem = qty
                    lots = c.execute(
                        """
                        SELECT l.id, l.remaining FROM lots l
                          JOIN holdings h ON h.id = l.holding_id
                         WHERE l.account_id=? AND h.symbol=? AND l.buy_date < ?
                           AND l.remaining > 0
                         ORDER BY l.buy_date ASC, l.id ASC
                        """,
                        (account_id, o["symbol"], trade_date),
                    ).fetchall()
                    for lot in lots:
                        if rem <= 0:
                            break
                        take = min(int(_D(lot["remaining"])), rem)
                        c.execute(
                            "UPDATE lots SET remaining=remaining-? WHERE id=?",
                            (take, lot["id"]),
                        )
                        rem -= take
                    if rem:
                        raise EngineError(f"卖出 FIFO 核销不一致 {o['symbol']} rem={rem}")
                    held_qty[o["symbol"]] -= qty
                    holding(o["symbol"], Decimal(-qty))
                    if held_qty[o["symbol"]] <= 0:
                        c.execute(
                            """
                            DELETE FROM lots WHERE holding_id IN
                              (SELECT id FROM holdings WHERE account_id=? AND symbol=?)
                            """,
                            (account_id, o["symbol"]),
                        )
                        c.execute(
                            "DELETE FROM holdings WHERE account_id=? AND symbol=?",
                            (account_id, o["symbol"]),
                        )
                        del held_qty[o["symbol"]]
                    runtime[oid]["final"] = "filled"
                    register_exit(o, tid, qty, p)

        # 写盘条件单运行态 + today 单未成交 → expired（§3.2 步4）
        for o in active:
            st = runtime[o["id"]]
            if o["created_at"] and not o["created_at"].startswith(trade_date):
                continue      # 非本日单（跨日 long / 未来单）：不属本日结算处理范围（#38 语义）
            if o["status"] == "invalid":          # 开盘前静态校验（qty_rule）
                c.execute(
                    "UPDATE condition_orders SET status='invalid', invalid_reason=?, settled_on=? WHERE id=?",
                    (o["invalid_reason"], trade_date, o["id"]),
                )
                continue
            if st["final"] == "invalid":
                c.execute(
                    "UPDATE condition_orders SET status='invalid', invalid_reason=?, settled_on=? WHERE id=?",
                    ("trigger_schema", trade_date, o["id"]),
                )
            else:
                final = st["final"]
                if final == "active" and o["validity"] == "today":
                    final = "expired"
                if final != "active":
                    c.execute(
                        "UPDATE condition_orders SET status=?, settled_on=? WHERE id=?",
                        (final, trade_date, o["id"]),
                    )
                if st["ins"] != o["insufficient_events"]:
                    c.execute(
                        "UPDATE condition_orders SET insufficient_events=? WHERE id=?",
                        (st["ins"], o["id"]),
                    )

        # 收盘价对齐 → degraded（§3.2 步3 / §8.5；已有质量标记不覆盖）
        trs = c.execute(
            "SELECT id, symbol, price, quality FROM trades WHERE settle_date=? AND account_id=?",
            (trade_date, account_id),
        ).fetchall()
        for tr in trs:
            cl = require_close(tr["symbol"])
            lastp = feed_last_price(tr["symbol"])
            if cl > 0 and not tr["quality"] and (abs(lastp - cl) / cl) > _CLOSE_TOL:
                c.execute("UPDATE trades SET quality='degraded' WHERE id=?", (tr["id"],))

        # 份额法净值（§6.1）：equity = 期末现金(分位) + 期末持仓×官方收盘价
        final_cash = cash.quantize(_MONEY, ROUND_HALF_UP)
        equity = final_cash
        for symbol, qty in held_qty.items():
            equity += qty * require_close(symbol)
        init_hold_value = Decimal("0")               # 期初持仓按前收估值
        for row in held_init:
            q = _D(row["quantity"])
            if q <= 0:
                continue
            if row["symbol"] not in prev:
                raise EngineError(f"期初持仓 {row['symbol']} 缺少 prev_close——禁止虚构估值价")
            init_hold_value += q * prev[row["symbol"]]
        start_equity = cash_start + init_hold_value
        nav = (equity / shares).quantize(_COST, ROUND_HALF_UP)
        # 对账自检（§6.5 双守恒，D1=A）：任一不平 → 抛错 → 整事务回滚（带病不落账）
        _audit_conservation(c, account_id, trade_date, cash_start=cash_start,
                            final_cash=final_cash, equity=equity, shares=shares,
                            nav=nav, close_map=close_map)
        total_pnl = (equity - initial_capital).quantize(_MONEY, ROUND_HALF_UP)
        today_pnl = (equity - start_equity).quantize(_MONEY, ROUND_HALF_UP)
        c.execute(
            "UPDATE accounts SET cash=?, nav=?, total_pnl=?, today_pnl=?, updated_ts=? WHERE id=?",
            (_q(final_cash), _q(nav), _q(total_pnl), _q(today_pnl), now, account_id),
        )
        c.execute(
            "INSERT INTO settlement_log(id, settle_key, trade_date, account_id, granularity_used, status, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                "sl" + secrets.token_hex(10), settle_key, trade_date, account_id,
                json.dumps(granularity_used, ensure_ascii=False), "done", now,
            ),
        )
        summary = {
            "already_settled": False,
            "account_id": account_id,
            "trade_date": trade_date,
            "cash": _q(final_cash),
            "nav": _q(nav),
            "total_pnl": _q(total_pnl),
            "today_pnl": _q(today_pnl),
        }
        log.info("settle %s %s -> %s", trade_date, account_id, summary["today_pnl"])
        return summary

"""EOD 回放撮合/结算引擎（spec-01 §3/§6 的受支持子集，P1 引擎切片）。

范围与边界（本版明确支持；其余拒绝而非静默跳过——宁可报 gap，不可错结）：
- 账户：granularity=eod_replay 的策略账户；单日结算；settle_key UNIQUE 幂等（§3.1.5/§3.8 单事务）；
- 条件单：scope=single、basis=replay_l0、order_type ∈ {buy, sell_take_profit, sell_stop}、
  price_type ∈ {market, limit}、单票价格触发（trigger JSON {"op":"le"|"ge","price":X}）；
- 成交价口径（§3.3 L0）：触达采样点价成交，滑点 0；
- T+1（§6.2）：可卖数 = Σ lots(buy_date < trade_date).remaining，当日新 lot 不可卖；
- 全量成交：现金/可卖不足不成交、记 insufficient_events、继续参与后续采样（§2.3 v0.3）；
- 收盘对齐（§3.2 步3）：序列末价与官方收盘价偏差 >0.3% → trade.quality='degraded'；
- 份额法（§6.1）：NAV=(cash+Σqty×官方收盘价)/shares；today_pnl 对比期初估值（prev_close）；
- 时间契约：交易日本地墙钟 naive ISO；order 仅在其 created_at 之后的采样点参与判定（防前视 #38）。

未支持（命中即抛 EngineGapError，不写任何数据）：L1/L2 档、篮子/组合/定时/开板封板类、
trail 移动止盈、signal_registry/exit_trackings、涨跌停/ST/板块、公司行动。
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from core.db import state_conn, write_txn

log = logging.getLogger(__name__)

SUPPORTED_ORDER_TYPES = {"buy", "sell_take_profit", "sell_stop"}

_MONEY = Decimal("0.01")
_COST = Decimal("0.0001")
_CLOSE_TOL = Decimal("0.003")

FEES = {
    "commission_rate": Decimal("0.00025"),
    "commission_min": Decimal("5"),
    "stamp_tax_rate": Decimal("0.0005"),
    "transfer_fee_rate": Decimal("0.00001"),
}


class EngineError(Exception):
    """前置校验失败（不改写任何数据）。"""


class EngineGapError(EngineError):
    """命中了本引擎尚未实现的 spec 语义。"""


def _D(v) -> Decimal:
    return Decimal(str(v)) if v is not None else Decimal("0")


def _q(v: Decimal) -> str:
    return str(v)


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


def _trigger_price(o: dict):
    """limit 单解析触发价；market 单恒触发。返回 (op, price)。"""
    if o["price_type"] == "market":
        if not o["trigger"]:
            return None, None
        raw = json.loads(o["trigger"])
        return raw.get("op") or None, _D(raw.get("price"))
    if not o["trigger"]:
        raise EngineError("limit 单缺少 trigger JSON")
    raw = json.loads(o["trigger"])
    op, price = raw.get("op"), raw.get("price")
    if op not in ("le", "ge") or price is None:
        raise EngineError("trigger JSON 需含 op(le|ge) 与 price")
    return op, _D(price)


def _qty_ok(symbol: str, qty: int) -> bool:
    """申报数量规则委托 orderstore（单一事实源，spec-01 §3.7 D6）。"""
    from core.orderstore import qty_rule_ok  # noqa: PLC0415
    return qty_rule_ok(symbol, qty)


def settle_account(
    state,
    account_id: str,
    trade_date: str,
    *,
    series_map: dict[str, list[tuple[str, float]]],
    close_map: dict[str, float],
    prev_close_map: dict[str, float] | None = None,
    fee: dict | None = None,
) -> dict:
    """对单个账户执行一日 EOD 结算（单 SQLite 事务原子写入）。

    series_map[symbol] = [(本地墙钟 naive ISO, price), ...]（升序 L0 采样点）。
    close_map[symbol] = 当日官方收盘价；prev_close_map[symbol] = 前一日官方收盘价。
    """
    f = {k: Decimal(str(v)) for k, v in (fee or FEES).items()}
    prev = {k: _D(v) for k, v in (prev_close_map or {}).items()}
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
            if o["symbol"] not in series_map:
                raise EngineError(f"order {o['id']} 标的 {o['symbol']} 缺少当日序列")
            active.append(dict(o))

        cash_start = _D(acct["cash"])    # 事务内期初快照（today_pnl 基准）
        cash = cash_start
        initial_capital = _D(acct["initial_capital"])

        # 开盘前静态校验：买入数量规则 → invalid（qty_rule，§3.7）
        for o in active:
            if not o["order_type"].startswith("sell"):
                try:
                    qty = int(Decimal(str(o["qty"])))
                except Exception as exc:
                    raise EngineError(f"order {o['id']} qty 非法: {exc}") from exc
                if not _qty_ok(o["symbol"], qty):
                    o["status"] = "invalid"
                    o["invalid_reason"] = "qty_rule"

        granularity_used: dict[str, str] = {}

        def require_close(symbol: str) -> Decimal:
            if symbol not in close_map:
                raise EngineError(f"{symbol} 缺少官方收盘价——禁止以替代价虚构收盘价")
            granularity_used[symbol] = "l0"
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

        def write_trade(o: dict, side: str, qty: int, price: Decimal, ts: str, fees: dict) -> str:
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
                    _q(fees["transfer_fee"]), ts, "replay_l0", "l0", "", trade_date,
                    o["reason"], o["strategy_version_no"] or version_no,
                ),
            )
            return tid

        # 内存运行态：insufficient 计数 / 终态（写盘集中在事务收尾）
        runtime = {o["id"]: {"ins": o["insufficient_events"], "final": "active"} for o in active}

        # ---- 逐票回放（§3.2）----
        for symbol in sorted({o["symbol"] for o in active}):
            ser = series_map[symbol]
            for o in active:
                if o["symbol"] != symbol or o["status"] != "active":
                    continue
                oid = o["id"]
                try:
                    op, trig = _trigger_price(o)
                except (json.JSONDecodeError, EngineError):
                    runtime[oid]["final"] = "invalid"
                    continue
                qty = int(Decimal(str(o["qty"])))
                for ts, pv in ser:
                    if o["created_at"] and o["created_at"] >= ts:
                        continue                      # #38 防前视
                    if runtime[oid]["final"] != "active":
                        break
                    p = _D(pv)
                    if trig is not None:
                        if op == "le" and p > trig:
                            continue
                        if op == "ge" and p < trig:
                            continue
                    if o["order_type"] == "buy":
                        amount = p * qty
                        fz = _fees(amount, side="buy", fee=f)
                        if cash < amount + fz["total"]:      # 资金不足：记事件继续（§2.3）
                            runtime[oid]["ins"] += 1
                            continue
                        cash -= amount + fz["total"]
                        tid = write_trade(o, "buy", qty, p, ts, fz)
                        hid = holding(o["symbol"], Decimal(qty), cost_basis=None)
                        # 摊薄 avg_cost（含费，§2.2 口径）
                        amt_with_fee = amount + fz["total"]
                        cur = c.execute(
                            "SELECT quantity FROM holdings WHERE id=?", (hid,)
                        ).fetchone()
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
                        if _D(sellable) < qty:         # 含 T+1 未到期
                            runtime[oid]["ins"] += 1
                            continue
                        amount = p * qty
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
                        break

        # 写盘条件单运行态 + today 单未成交 → expired（§3.2 步4）
        for o in active:
            st = runtime[o["id"]]
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

        # 收盘价对齐 → degraded（§3.2 步3 / §8.5）
        trs = c.execute(
            "SELECT id, symbol, price FROM trades WHERE settle_date=? AND account_id=?",
            (trade_date, account_id),
        ).fetchall()
        for tr in trs:
            cl = require_close(tr["symbol"])
            lastp = _D(series_map[tr["symbol"]][-1][1])
            if cl > 0 and (abs(lastp - cl) / cl) > _CLOSE_TOL:
                c.execute("UPDATE trades SET quality='degraded' WHERE id=?", (tr["id"],))

        # 份额法净值（§6.1）：equity = 期末现金 + 期末持仓×官方收盘价
        equity = cash
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
        final_cash = cash.quantize(_MONEY, ROUND_HALF_UP)
        nav = (equity / shares).quantize(_COST, ROUND_HALF_UP)
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

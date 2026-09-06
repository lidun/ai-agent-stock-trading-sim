"""条件单创建（spec-01 §2.3 交易意图唯一通道）——引擎/Agent 下单的统一入口。

创建即 schema 前置校验（spec-01 #45）：标的格式/数量规则（§3.7）/触发 JSON/账户
状态；通过后落 condition_orders(status=active)。撮合与结算由 eodengine 消费，本层
不承载资金判定（引擎按当日行情执行）。

时间约定：created_at 记录为**交易日本地墙钟（北京时间，naive ISO）**——eodengine
回放按同口径做 #38 防前视（order 创建时刻之后的采样点才参与判定）。
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from core.db import state_conn, write_txn

_SYMBOL_RE = re.compile(r"^\d{6}$")
_BJT = timezone(timedelta(hours=8))

VALID_ORDER_TYPES = {
    "buy",
    "sell_take_profit",
    "sell_stop",
    "sell_trail",
    "sell_open_board",
    "buy_seal_confirm",
    "basket",
    "time",
    "combo",
}


class OrderError(Exception):
    """下单前置校验失败。"""


def now_beijing_naive() -> str:
    return datetime.now(_BJT).replace(tzinfo=None).isoformat(timespec="seconds")


def qty_rule_ok(symbol: str, qty: int) -> bool:
    """申报数量规则（spec-01 §3.7 D6 子集：主板 100 整数倍 / 科创板 ≥200 起任意整数）。"""
    if symbol.startswith("688"):
        return qty >= 200
    return qty > 0 and qty % 100 == 0


def validate_order_payload(*, symbol: str, order_type: str, direction: str,
                           qty, trigger, price_type: str) -> dict:
    if order_type not in VALID_ORDER_TYPES:
        raise OrderError(f"不支持的 order_type: {order_type}")
    if direction not in ("buy", "sell"):
        raise OrderError("direction 须为 buy|sell")
    if not _SYMBOL_RE.match(symbol):
        raise OrderError("symbol 须为 6 位 A 股代码")
    if price_type not in ("market", "limit"):
        raise OrderError("price_type 须为 market|limit")
    trig: dict | None = None
    if trigger:
        trig = trigger if isinstance(trigger, dict) else json.loads(trigger)
        op = trig.get("op")
        if op not in ("le", "ge") or trig.get("price") is None:
            raise OrderError("trigger 需含 op(le|ge) 与 price")
    if price_type == "limit" and trig is None:
        raise OrderError("limit 单必须携带 trigger")
    if qty is None or int(qty) <= 0:
        raise OrderError("qty 须为正整数")
    qty = int(qty)
    if not qty_rule_ok(symbol, qty):
        raise OrderError(f"数量 {qty} 违反申报规则（主板 100 整数倍/科创板 ≥200）")
    return {"qty": qty, "trigger": trig}


def place_order(
    state,
    *,
    account_id: str,
    creator: str,
    order_type: str = "buy",
    direction: str = "buy",
    symbol: str,
    qty: int | None = None,
    trigger: dict | None = None,
    price_type: str = "market",
    validity: str = "today",
    reason: str = "",
    basis: str = "replay_l0",
) -> dict:
    """为策略账户登记一条 active 条件单并返回序列化行。"""
    payload = validate_order_payload(
        symbol=symbol, order_type=order_type, direction=direction,
        qty=qty, trigger=trigger, price_type=price_type,
    )
    qty = payload["qty"]
    trig = payload["trigger"]
    conn = state_conn(state)
    with write_txn(conn) as c:
        row = c.execute(
            """
            SELECT a.id, a.granularity, a.status AS acct_status, ag.role AS agent_role,
                   ag.status AS agent_status
              FROM accounts a JOIN agents ag ON ag.id = a.id
             WHERE a.id = ?
            """,
            (account_id,),
        ).fetchone()
        if row is None:
            raise OrderError("账户不存在（仅策略 Agent 拥有模拟账户）")
        if row["agent_role"] != "strategy":
            raise OrderError("管理 Agent 非交易账户，不能下单")
        if row["acct_status"] not in ("normal",):
            raise OrderError(f"账户状态 {row['acct_status']} 不允许下单")
        if row["agent_status"] not in ("running",):
            raise OrderError(f"Agent 生命周期 {row['agent_status']} 不允许下单")
        oid = "co" + secrets.token_hex(10)
        c.execute(
            """
            INSERT INTO condition_orders(id, account_id, order_type, direction, scope, symbol,
                symbols, trigger, basis, price_ref, qty, price_type, limit_price, validity,
                priority, status, insufficient_events, created_at, creator, reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                oid, account_id, order_type, direction, "single", symbol, "[]",
                json.dumps(trig, ensure_ascii=False) if trig else "",
                basis, "absolute", qty, price_type,
                trig["price"] if trig and price_type == "limit" else None,
                validity, 0, "active", 0, now_beijing_naive(), creator, reason,
            ),
        )
    return {"id": oid, "account_id": account_id, "symbol": symbol,
            "order_type": order_type, "direction": direction, "qty": qty,
            "price_type": price_type, "status": "active"}

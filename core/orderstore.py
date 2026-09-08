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

from core.db import read_txn, state_conn, write_txn

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


def qty_rule_ok(symbol: str, qty: int, *, side: str = "buy") -> bool:
    """申报数量规则（spec-01 §3.7 D6：买入按板块整手，卖出可零股）。

    - side=buy：科创板 ≥200 起可 1 股递增；其余板块 100 整数倍；
    - side=sell：A 股卖出允许零股（不足 100 股一次申报卖出），仅需正整数
      （一次性卖出/尾股清仓语义由持仓可卖校验兜底，引擎记 insufficient）。
    """
    if side == "sell":
        return qty > 0
    if symbol.startswith("688"):
        return qty >= 200
    return qty > 0 and qty % 100 == 0


_PRICE_KIND_OP = {"price_le": "le", "price_ge": "ge"}
_UNIMPL_KINDS = {"open_board", "seal_confirm", "volume", "and", "or"}


def _parse_pct(trig: dict, *, kind: str) -> float:
    try:
        pct = float(trig.get("pct"))
    except (TypeError, ValueError):
        raise OrderError(f"kind={kind} 需 pct 数值") from None
    if pct == 0:
        raise OrderError(f"kind={kind} 的 pct 须非 0")
    return pct


def _canon_trigger(trig: dict, *, order_type: str) -> dict:
    """规范化 trigger → spec-01 §2.4 kind 语义（新单落库统一 canonical）。

    - price_le/price_ge 原样返回；legacy {"op":"le"|"ge","price"} 等价映射；
    - kind=trail（移动止盈）仅允许 sell_trail 且 drop_pct>0；
    - kind=pct_chg（昨收基准涨跌幅）与 kind=vs_cost（持仓成本基准）需 op(le|ge)+pct≠0，
      pct_chg 可选 not_limit（触发时刻未封板）；vs_cost 为卖出相对成本，先不做 not_limit；
    - 其余 kind 属未实现语义 → 显式 OrderError 拒单（不静默放行）。
    """
    kind = trig.get("kind")
    if kind is not None:
        if kind == "trail":
            if order_type != "sell_trail":
                raise OrderError("kind=trail 仅适用于 sell_trail 移动止盈单")
            try:
                drop = float(trig.get("drop_pct"))
            except (TypeError, ValueError):
                raise OrderError("kind=trail 需 drop_pct 数值") from None
            if not drop > 0:
                raise OrderError("kind=trail 的 drop_pct 须 > 0")
            return {"kind": "trail", "drop_pct": drop}
        if kind in _PRICE_KIND_OP:
            if trig.get("price") is None:
                raise OrderError("trigger kind 非法或缺少 price")
            return {"kind": kind, "price": trig["price"]}
        if kind == "pct_chg":
            op = trig.get("op")
            if op not in ("le", "ge"):
                raise OrderError("kind=pct_chg 需 op(le|ge)")
            pct = _parse_pct(trig, kind=kind)
            out: dict = {"kind": kind, "op": op, "pct": pct}
            nl = trig.get("not_limit", False)
            if not isinstance(nl, bool):
                raise OrderError("kind=pct_chg 的 not_limit 须为布尔")
            if nl:
                out["not_limit"] = True
            return out
        if kind == "vs_cost":
            op = trig.get("op")
            if op not in ("le", "ge"):
                raise OrderError("kind=vs_cost 需 op(le|ge)")
            pct = _parse_pct(trig, kind=kind)
            return {"kind": kind, "op": op, "pct": pct}
        if kind == "time":
            if order_type != "time":
                raise OrderError("kind=time 仅适用于 time 定时单")
            at = trig.get("at")
            if (not isinstance(at, str) or len(at) != 5 or at[2] != ":"
                    or not at[:2].isdigit() or not at[3:].isdigit()):
                raise OrderError("kind=time 的 at 须为 HH:MM（如 14:50）")
            return {"kind": "time", "at": at}
        if kind in _UNIMPL_KINDS:
            raise OrderError(f"trigger kind={kind} 引擎尚未实现——拒绝下单")
        raise OrderError("trigger kind 非法或缺少 price")
    op = trig.get("op")
    if op not in ("le", "ge") or trig.get("price") is None:
        raise OrderError("trigger 需含 op(le|ge) 与 price")
    return {"kind": "price_le" if op == "le" else "price_ge", "price": trig["price"]}


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
        trig = _canon_trigger(trig, order_type=order_type)
    if trig is not None and trig.get("kind") == "trail" and price_type != "market":
        raise OrderError("trail 移动止盈单须 price_type=market")
    if trig is not None and trig.get("kind") == "time" and price_type != "market":
        raise OrderError("time 定时单须 price_type=market")
    if price_type == "limit" and trig is None:
        raise OrderError("limit 单必须携带 trigger")
    if qty is None or int(qty) <= 0:
        raise OrderError("qty 须为正整数")
    qty = int(qty)
    if not qty_rule_ok(symbol, qty, side=direction):
        raise OrderError(f"数量 {qty} 违反申报规则（买入主板 100 整数倍/科创板 ≥200，卖出可零股）")
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
            SELECT a.id, a.granularity, a.status AS acct_status, a.role AS acct_role,
                   ag.id AS agent_id, ag.role AS agent_role, ag.status AS agent_status
              FROM accounts a JOIN agents ag ON ag.id = a.agent_id
             WHERE a.id = ?
            """,
            (account_id,),
        ).fetchone()
        if row is None:
            raise OrderError("账户不存在（仅策略 Agent 拥有模拟账户）")
        if row["agent_role"] != "strategy":
            raise OrderError("管理 Agent 非交易账户，不能下单")
        # 冻结证券（spec-06 §6.3）：该 Agent 单票冻结买入，卖出不受影响（即时生效）
        if direction == "buy":
            frozen = c.execute(
                "SELECT 1 FROM frozen_securities WHERE agent_id=? AND symbol=?",
                (row["agent_id"], symbol)).fetchone()
            if frozen:
                raise OrderError(f"证券 {symbol} 已冻结买入（用户直控，保留卖出与风控）")
        # 下单资格（#63 双账户 + §6.3 直控）：主账户 normal 由 running Agent 交易；
        # 直控冻结买入（acct=paused_buy）保留卖出与风控 → direction=sell 仍放行；
        # trial 账户 status=trial 由试运行期 Agent 交易（回放期下单）；其余组合拒绝
        sell_only_frozen = (
            row["agent_status"] == "running"
            and row["acct_status"] == "paused_buy"
            and direction == "sell"
        )
        allowed = (
            row["agent_status"] == "running" and row["acct_status"] == "normal"
        ) or sell_only_frozen or (
            row["agent_status"] == "trial" and row["acct_status"] == "trial"
        )
        if not allowed:
            raise OrderError(
                f"账户状态 {row['acct_status']}/{row['agent_status']} 不允许下单")
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


def emergency_sell_all(state, *, agent_id: str,
                       reason: str = "紧急清仓（用户直控）") -> dict:
    """紧急清仓（spec-06 §6.3）：主账户全部持仓逐票挂市价卖出条件单，即时生成。

    读出即下、秒级生效（不经 LLM）；卖单经 place_order 闸门——冻结买入（paused_buy）
    下 direction=sell 仍放行，熔断全停（halted）时卖出同样被闸门拦截并在回执明示。
    返回 {agent_id, account_id, orders:[{symbol,qty,id}], blocked_halted:bool}。
    """
    conn = state_conn(state)
    with read_txn(conn) as c:
        agent = c.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        if agent is None:
            raise OrderError(f"Agent {agent_id} 不存在")
        if agent["role"] != "strategy":
            raise OrderError(f"Agent {agent_id} 为非策略 Agent")
        main = c.execute(
            "SELECT * FROM accounts WHERE agent_id=? AND role='main'", (agent_id,)
        ).fetchone()
        if main is None:
            raise OrderError(f"Agent {agent_id} 无主账户")
        rows = c.execute(
            """
            SELECT symbol, SUM(quantity) AS qty FROM holdings
             WHERE account_id=? AND quantity > 0
             GROUP BY symbol ORDER BY symbol
            """,
            (main["id"],),
        ).fetchall()
    orders: list[dict] = []
    blocked = main["status"] == "halted"
    for r in rows:
        if blocked:
            break
        try:
            placed = place_order(
                state, account_id=main["id"], creator="user", direction="sell",
                order_type="sell_open_board", symbol=r["symbol"], qty=r["qty"],
                reason=reason, basis="intraday",
            )
        except OrderError as e:
            continue
        orders.append({"symbol": r["symbol"], "qty": r["qty"], "id": placed["id"]})
    return {"agent_id": agent_id, "account_id": main["id"],
            "holdings": len(rows), "orders": orders,
            "blocked_halted": blocked}


def emergency_sell_all_global(state) -> dict:
    """全局紧急清仓（spec-06 §6.3 全局层）：对全部运行中策略 Agent 逐票市价卖出。"""
    conn = state_conn(state)
    with read_txn(conn) as c:
        agents = [r[0] for r in c.execute(
            """
            SELECT ag.id FROM agents ag
             JOIN accounts ac ON ac.agent_id = ag.id AND ac.role = 'main'
             WHERE ag.role = 'strategy' AND ag.status = 'running'
             ORDER BY ag.id
            """
        ).fetchall()]
    per_agent = []
    total_orders = 0
    total_holdings = 0
    for agent_id in agents:
        r = emergency_sell_all(state, agent_id=agent_id)
        per_agent.append({
            "agent_id": agent_id,
            "account_id": r["account_id"],
            "holdings": r["holdings"],
            "orders": r["orders"],
            "blocked_halted": r["blocked_halted"],
        })
        total_orders += len(r["orders"])
        total_holdings += r["holdings"]
    return {"agents": per_agent, "agents_count": len(agents),
            "total_holdings": total_holdings, "total_orders": total_orders}

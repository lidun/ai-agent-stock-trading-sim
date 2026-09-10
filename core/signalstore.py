"""信号注册表落库与 N 日前瞻结算（spec-01 §2.5/§8；统计口径归 spec-05）。

登记点（引擎/策略侧）：
- candidate：子 Agent 选股产出时经 register_candidate 登记（策略侧供给通道）；
- buy / sell：结算事务内随买入/卖出成交自动登记（eodengine.settle_account 同事务）；
- pitfall_intercept：反向避坑库剔除候选时登记（#54，pitfall_id + exception 标记）。

字段语义：quality 为登记时票级数据质量（spec-03 §7 传播：degraded_reason 等）；
strategy_version_no 为产生信号的策略版本；trial_flag=1 表示试运行/验证账户样本
（spec-05 §3.2/§3.11 统计默认排除，防污染知识库证据）。ref_price 为登记日官方收盘
（前瞻收益基准，不虚构）；fwd_return_pct 于第 N 个交易日结清日填入，停牌/退市缺价
按最近可得价 stale 结清并标注（不虚构价）。session 计数按 reg_date 之后的交易日推进
（last_seen 幂等，无本地日历表），与 settlement_log 是否活跃无关。
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from core.db import state_conn, write_txn

SIGNAL_DEFAULT_DAYS = 10
SIG_TYPES = ("candidate", "buy", "sell", "pitfall_intercept")
_COST = Decimal("0.01")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _q(v: Decimal) -> float:
    return float(v.quantize(_COST, ROUND_HALF_UP))


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def register(state, account_id: str, sig_type: str, symbol: str, reg_date: str, *,
             concept_tag: str = "", env_bucket: str = "", exception: int = 0,
             pitfall_id: str = "", quality: str = "", strategy_version_no: str = "",
             trial_flag: int = 0, ref_price: float | None = None,
             conn=None) -> str:
    """登记一条信号（幂等由调用方保证：结算事务内随成交一次；候选按选股事件一次）。"""
    if sig_type not in SIG_TYPES:
        raise ValueError(f"未知信号类型 {sig_type}（须属 {SIG_TYPES}）")
    sid = "sg" + secrets.token_hex(10)
    ref = _q(Decimal(str(ref_price))) if ref_price is not None else 0.0
    own = conn is None
    c = conn if conn is not None else state_conn(state)
    sql = (
        "INSERT INTO signal_registry(id, account_id, sig_type, symbol, reg_date,"
        " concept_tag, env_bucket, exception, pitfall_id, fwd_return_pct, fwd_end_date,"
        " quality, strategy_version_no, trial_flag, ref_price, last_close, sessions_done,"
        " last_seen, created_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    params = (sid, account_id, sig_type, symbol, reg_date, concept_tag, env_bucket,
              int(exception), pitfall_id, None, "", quality, strategy_version_no,
              int(trial_flag), ref, ref, 0, "", _now())
    if own:
        with write_txn(c) as cc:
            cc.execute(sql, params)
    else:
        c.execute(sql, params)
    return sid


def register_candidate(state, account_id: str, symbol: str, reg_date: str, *,
                       concept_tag: str = "", env_bucket: str = "",
                       strategy_version_no: str = "", trial_flag: int = 0,
                       ref_price: float | None = None, quality: str = "") -> str:
    """候选入选信号登记（子 Agent 选股产出的策略侧供给通道，spec-01 §8）。"""
    return register(state, account_id, "candidate", symbol, reg_date,
                    concept_tag=concept_tag, env_bucket=env_bucket,
                    strategy_version_no=strategy_version_no, trial_flag=trial_flag,
                    ref_price=ref_price, quality=quality)


def register_pitfall(state, account_id: str, symbol: str, reg_date: str, *,
                     pitfall_id: str, exception: int = 0, concept_tag: str = "",
                     env_bucket: str = "", ref_price: float | None = None,
                     quality: str = "", trial_flag: int = 0) -> str:
    """避坑拦截信号登记（#54：被剔除标的入注册表，破例放行 exception=1）。"""
    return register(state, account_id, "pitfall_intercept", symbol, reg_date,
                    concept_tag=concept_tag, env_bucket=env_bucket,
                    exception=exception, pitfall_id=pitfall_id,
                    ref_price=ref_price, quality=quality, trial_flag=trial_flag)


def settle_due(state, account_id: str, trade_date: str, *,
               market: dict[str, dict] | None = None,
               base_prices: dict[str, float] | None = None,
               n_days: int | None = None) -> dict:
    """推进某账户在途信号并在第 N 个交易日结清前瞻收益（确定性/零 token/幂等）。

    market[sym]={"close": float} 为当日官方收盘；base_prices[sym] 为登记日官方收盘，
    仅在登记时 ref_price 缺失时补基准（策略侧候选登记未给价的兜底，缺则保持在途不虚构）。
    规则同 exit_trackings：仅推进 reg_date 之后的交易日；同日重复调用/重试不重复推进；
    到期日缺价但此前有最近可得价 → stale 结清并标 quality=stale_close。
    """
    n = int(n_days or SIGNAL_DEFAULT_DAYS)
    md = market or {}
    bp = base_prices or {}
    conn = state_conn(state)
    updated = closed = 0
    with write_txn(conn) as c:
        rows = c.execute(
            "SELECT * FROM signal_registry WHERE account_id=? AND fwd_end_date=''"
            " AND reg_date < ? ORDER BY reg_date ASC, id ASC",
            (account_id, trade_date),
        ).fetchall()
        for r in rows:
            ref = _f(r["ref_price"])
            if ref <= 0 and r["symbol"] in bp:
                ref = _f(bp[r["symbol"]])
                c.execute("UPDATE signal_registry SET ref_price=? WHERE id=?",
                          (_q(Decimal(str(ref))), r["id"]))
            if ref <= 0:
                continue                     # 无登记日基准价：保持在途，不虚构收益
            if r["last_seen"] == trade_date:
                continue
            if r["last_seen"] and r["last_seen"] > trade_date:
                continue
            sym = r["symbol"]
            close = md.get(sym, {}).get("close")
            close = _f(close) if close is not None else None
            if close is None and _f(r["last_close"]) <= 0:
                continue                     # 当日缺价且历史无可得价：不推进（停牌挂起）
            sess = int(r["sessions_done"]) + 1
            c.execute("UPDATE signal_registry SET sessions_done=?, last_seen=? WHERE id=?",
                      (sess, trade_date, r["id"]))
            if close is not None:
                c.execute("UPDATE signal_registry SET last_close=? WHERE id=?",
                          (_q(Decimal(str(close))), r["id"]))
            updated += 1
            if sess < n:
                continue
            stale = close is None
            ev = close if close is not None else _f(r["last_close"])
            fwd = ((Decimal(str(ev)) - Decimal(str(ref))) / Decimal(str(ref))
                   * Decimal("100"))
            qual = [x for x in (r["quality"] or "").split("+") if x]
            if stale:
                qual.append("stale_close")
            c.execute(
                "UPDATE signal_registry SET fwd_return_pct=?, fwd_end_date=?, quality=?"
                " WHERE id=?",
                (_q(fwd), trade_date, "+".join(qual), r["id"]),
            )
            closed += 1
    return {"account_id": account_id, "trade_date": trade_date,
            "updated": updated, "closed": closed}


def list_signals(state, account_id: str, *, sig_type: str = "",
                 settled: bool | None = None, limit: int = 200) -> dict:
    """只读列表（供统计/UI 消费）：按登记日倒序。"""
    conn = state_conn(state)
    sql = "SELECT * FROM signal_registry WHERE account_id=?"
    params: list = [account_id]
    if sig_type:
        if sig_type not in SIG_TYPES:
            raise ValueError(f"未知信号类型 {sig_type}")
        sql += " AND sig_type=?"
        params.append(sig_type)
    if settled is not None:
        sql += " AND fwd_end_date != ''" if settled else " AND fwd_end_date = ''"
    sql += " ORDER BY reg_date DESC, created_ts DESC LIMIT ?"
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    items = [{
        "id": r["id"], "account_id": r["account_id"], "sig_type": r["sig_type"],
        "symbol": r["symbol"], "reg_date": r["reg_date"],
        "concept_tag": r["concept_tag"], "env_bucket": r["env_bucket"],
        "exception": bool(r["exception"]), "pitfall_id": r["pitfall_id"],
        "fwd_return_pct": (None if r["fwd_return_pct"] is None
                           else _q(Decimal(str(r["fwd_return_pct"])))),
        "fwd_end_date": r["fwd_end_date"], "settled": bool(r["fwd_end_date"]),
        "quality": r["quality"], "strategy_version_no": r["strategy_version_no"],
        "trial_flag": bool(r["trial_flag"]), "sessions_done": int(r["sessions_done"]),
        "created_ts": r["created_ts"],
    } for r in rows]
    return {"account_id": account_id, "total": len(items), "items": items}

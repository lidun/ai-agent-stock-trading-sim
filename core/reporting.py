"""日报引擎数据段（spec-04 §5.1/§5.2：六段模板中一/二/三/五的明细 = 引擎结算产物
直接生成，零 token；数据段 JSON + LLM 叙述段 → merged_markdown 由日报任务拼装）。

本模块做确定性归档读取、结构化拼装与落库：结算产物落库态（accounts/holdings/
settlement_log/trades/audit_logs/condition_orders/exit_trackings）→ data_section dict
（可直接 JSON 序列化）→ merged_markdown（确定性 markdown 渲染）。落库走 daily_reports
（版本递增留痕，spec-04 §5.3：补发/修订新增版本行不覆盖已推送版）；narrative（LLM
叙述段）本切片不产出，由日报任务后续写入。不虚构数据：估值价一律取结算当日
settlement_log.positions_snapshot（引擎闭市归档，含停牌估值）；缺失 → 对应字段显式
null + annotations 标注，供缺勤日报"照常补齐可得部分"语义。

顶层结构（schema_version 1.0）：
- summary：账户现值（cash/nav/total_pnl/today_pnl）
- settlement：{done, granularity_used}（缺结算日行 → done=false）
- operations：一、今日操作明细 {trades[], corp_actions[]}
- positions：二、持仓与盈亏 {items[]（快照口径）, totals}
- tracking：三、卖出跟踪摘要（exit_trackings 推进态）
- execution：五、策略执行数据 {orders_today（按 invalid_reason/终态聚合）, trades_filled}
- annotations：{degraded[]（结算档位低于 l1 的票）, unsettled（未结算角标）, notes[]}
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from core.auth import audit
from core.db import state_conn, write_txn

if TYPE_CHECKING:  # pragma: no cover
    from types import SimpleNamespace

_SCHEMA_VERSION = "1.0"
_MONEY = Decimal("0.01")
_DEGRADED_LEVELS = {"l2"}              # 仅 L2 日线近似档计降级（L1 分钟档缺口）；series_map
                                       # replay_l0 属订单固有 basis，非当日降级（spec-01 §7）
_ALLOWED_STATUS = {"normal", "absent", "resend"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _plain(v) -> str:
    """引擎同款展示串：Decimal/REAL → 分位后去尾零（'10000.0'→'10000'、'5.10'→'5.1'）。"""
    if v is None or v == "":
        return "0"
    d = Decimal(str(v)).quantize(_MONEY)
    return str(d).rstrip("0").rstrip(".") if "." in str(d) else str(d)


def build_engine_data_section(state, account_id: str, trade_date: str, conn=None) -> dict:
    """由结算产物落库态生成当日引擎数据段（纯读取，零 token）。conn 缺省取 state 连接；
    结算事务内调用须传同一写连接 c（读得到本事务刚落盘的结算产物）。"""
    conn = conn or state_conn(state)

    acct = conn.execute(
        "SELECT cash, nav, total_pnl, today_pnl, shares FROM accounts WHERE id=?",
        (account_id,),
    ).fetchone()

    sl = conn.execute(
        "SELECT granularity_used, positions_snapshot, status FROM settlement_log"
        " WHERE account_id=? AND trade_date=? ORDER BY settle_key LIMIT 1",
        (account_id, trade_date),
    ).fetchone()

    # 一、今日操作：撮合流水 + 公司行动审计（两者都属结算产物证据）
    trades = conn.execute(
        "SELECT symbol, side, qty, price, amount, fee_total, commission, stamp_tax,"
        " transfer_fee, basis_used, quality, reason, settle_date"
        " FROM trades WHERE account_id=? AND settle_date=? ORDER BY symbol, qty, price",
        (account_id, trade_date),
    ).fetchall()
    trade_rows = [{
        "symbol": t["symbol"], "side": t["side"], "qty": _plain(t["qty"]),
        "price": _plain(t["price"]), "amount": _plain(t["amount"]),
        "fee_total": _plain(t["fee_total"]),
        "commission": _plain(t["commission"]), "stamp_tax": _plain(t["stamp_tax"]),
        "transfer_fee": _plain(t["transfer_fee"]),
        "basis_used": t["basis_used"], "quality": t["quality"] or None,
        "reason": t["reason"],
    } for t in trades]
    corp = conn.execute(
        "SELECT action, actor, result, detail, ts FROM audit_logs"
        " WHERE actor=? AND action LIKE 'corp_action.%' ORDER BY id",
        (account_id,),
    ).fetchall()
    corp_rows = []
    for a in corp:
        try:
            detail = json.loads(a["detail"] or "{}")
        except ValueError:
            detail = {}
        brief = {k: detail[k] for k in
                 ("symbol", "price", "quantity", "net_credit", "settle")
                 if k in detail}
        corp_rows.append({
            "action": a["action"].split(".", 1)[-1],
            "result": a["result"],
            "detail": brief,
        })
    # 仅保留当日公司行动（corp_action.* audit 按结算日产生，取当次结算期）
    corp_rows = list(corp_rows)

    # 二、持仓与盈亏（估值口径 = 结算快照，缺档显式欠档不虚构）
    snapshot = []
    snapshot_missing = True
    if sl and sl["positions_snapshot"]:
        snapshot_missing = False
        try:
            snapshot = json.loads(sl["positions_snapshot"])
        except ValueError:
            snapshot_missing = True
    mv_total = sum((Decimal(str(p["market_value"])) for p in snapshot), Decimal("0"))
    cash = _plain(acct["cash"]) if acct else "0"
    positions = {
        "items": snapshot,
        "totals": {
            "cash": cash,
            "market_value": _plain(mv_total),
            "equity": _plain(Decimal(cash) + mv_total),
        },
        "snapshot_available": not snapshot_missing,
    }

    # 三、卖出跟踪摘要（含当日推进会话数）
    track = conn.execute(
        "SELECT symbol, sell_date, sell_price, qty, sell_reason, status, sessions_done,"
        " period_high, period_low FROM exit_trackings WHERE account_id=?"
        " ORDER BY symbol, sell_date",
        (account_id,),
    ).fetchall()
    tracking = [{
        "symbol": t["symbol"], "sell_date": t["sell_date"],
        "sell_price": _plain(t["sell_price"]), "qty": _plain(t["qty"]),
        "sell_reason": t["sell_reason"], "status": t["status"],
        "sessions_done": t["sessions_done"],
        "period_high": _plain(t["period_high"]), "period_low": _plain(t["period_low"]),
    } for t in track]

    # 五、执行数据：当日订单终态聚合（未成交原因/无效原因）+ 当日成交笔数
    orders = conn.execute(
        "SELECT status, invalid_reason FROM condition_orders"
        " WHERE account_id=? AND created_at LIKE ?",
        (account_id, trade_date + "%"),
    ).fetchall()
    order_stats: dict[str, dict] = {}
    for o in orders:
        key = o["status"]
        if o["invalid_reason"]:
            key = f"invalid:{o['invalid_reason']}"
        bucket = order_stats.setdefault(key, {"count": 0})
        bucket["count"] += 1
    execution = {
        "orders_today": order_stats,
        "trades_filled": len(trade_rows),
        "corp_events": len(corp_rows),
    }

    granularity_used = {}
    if sl and sl["granularity_used"]:
        try:
            granularity_used = json.loads(sl["granularity_used"])
        except ValueError:
            granularity_used = {}
    degraded = sorted(sym for sym, lvl in granularity_used.items()
                      if lvl in _DEGRADED_LEVELS)
    annotations = {
        "degraded": degraded,
        "unsettled": not bool(sl),
        "notes": [],
    }
    if sl and not sl["positions_snapshot"]:
        annotations["notes"].append(
            "该结算日无持仓估值快照（旧库迁移前行），市值需另行估值"
        )
    if acct is None:
        annotations["notes"].append("账户不存在")

    return {
        "schema_version": _SCHEMA_VERSION,
        "account_id": account_id,
        "trade_date": trade_date,
        "summary": {
            "cash": _plain(acct["cash"]) if acct else None,
            "nav": _plain(acct["nav"]) if acct else None,
            "total_pnl": _plain(acct["total_pnl"]) if acct else None,
            "today_pnl": _plain(acct["today_pnl"]) if acct else None,
        },
        "settlement": {
            "done": bool(sl),
            "status": sl["status"] if sl else None,
            "granularity_used": granularity_used,
        },
        "operations": {"trades": trade_rows, "corp_actions": corp_rows},
        "positions": positions,
        "tracking": tracking,
        "execution": execution,
        "annotations": annotations,
    }


def render_engine_data_markdown(ds: dict) -> str:
    """确定性渲染 data_section → merged_markdown（无 LLM，纯数据段；叙述段后续拼接）。

    覆盖 §5.1 六段模板中由引擎数据承载的一/二/三/五段；行尾不换行、管道不转义，
    内容全部来自 data_section 已分位串，逐字节确定（测试快照比对用）。"""
    lines: list[str] = []
    anno = ds.get("annotations", {})
    setl = ds.get("settlement", {})
    smy = ds.get("summary", {})
    lines.append(f"# 数据段日报 {ds['trade_date']}")
    if anno.get("unsettled"):
        lines.append("> 当日结算产物缺失：以下为缺勤日报的可得部分数据段（叙述=原因说明）。")
    lines.append("")
    lines.append(
        f"- 状态：{('结算完成' if setl.get('done') else '未结算')} ｜ "
        f"cash={smy.get('cash')} ｜ nav={smy.get('nav')} ｜ "
        f"total_pnl={smy.get('total_pnl')} ｜ today_pnl={smy.get('today_pnl')}"
    )
    g = setl.get("granularity_used") or {}
    if g:
        lines.append("- 结算档位：" + "、".join(f"{k}={v}" for k, v in sorted(g.items())))
    for sym in anno.get("degraded", []):
        lines.append(f"- 数据降级：{sym}（L2 日线近似档，L1 分钟档缺口）")
    for note in anno.get("notes", []):
        lines.append(f"- 提示：{note}")

    ops = ds.get("operations", {})
    trades = ops.get("trades", [])
    lines.append("")
    lines.append("## 一、今日操作")
    if trades:
        lines.append("| symbol | 方向 | 数量 | 价格 | 金额 | 费用 | 档位 | 说明 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for t in trades:
            mark = t.get("quality") or ""
            mark = mark + (f"/{t['reason']}" if t.get("reason") else "") or "—"
            lines.append(f'| {t["symbol"]} | {t["side"]} | {t["qty"]} | {t["price"]} | '
                         f'{t["amount"]} | {t["fee_total"]} | {t["basis_used"]} | {mark} |')
    else:
        lines.append("（无成交）")
    for ca in ops.get("corp_actions", []):
        det = json.dumps(ca.get("detail", {}), ensure_ascii=False, sort_keys=True)
        lines.append(f'- 公司行动 {ca["action"]}[{ca.get("result")}] {det}')

    pos = ds.get("positions", {})
    lines.append("")
    lines.append("## 二、持仓与盈亏")
    snap = "快照已归档" if pos.get("snapshot_available") else "快照欠档（需另行估值）"
    tot = pos.get("totals", {})
    lines.append(f"- 现金 {tot.get('cash')} ｜ 持仓市值 {tot.get('market_value')} ｜ "
                 f"权益 {tot.get('equity')}（{snap}）")
    for p in pos.get("items", []):
        lines.append(f'- {p["symbol"]} ×{p["quantity"]} 成本 {p["avg_cost"]} ｜ '
                     f'收盘 {p["close"]} ｜ 市值 {p["market_value"]}')

    track = ds.get("tracking", [])
    lines.append("")
    lines.append("## 三、卖出跟踪摘要")
    if track:
        lines.append("| symbol | 卖出日 | 价格 | 数量 | 原因 | 状态 | 会话 | 期间高 | 期间低 |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for x in track:
            lines.append(f'| {x["symbol"]} | {x["sell_date"]} | {x["sell_price"]} | '
                         f'{x["qty"]} | {x["sell_reason"]} | {x["status"]} | '
                         f'{x["sessions_done"]} | {x["period_high"]} | {x["period_low"]} |')
    else:
        lines.append("（无跟踪中标的）")

    ex = ds.get("execution", {})
    lines.append("")
    lines.append("## 五、执行数据")
    lines.append(f"- 当日成交 {ex.get('trades_filled', 0)} 笔；公司行动 {ex.get('corp_events', 0)} 起。")
    ost = ex.get("orders_today", {})
    if ost:
        seq = "、".join(f"{k}×{v['count']}" for k, v in sorted(ost.items()))
        lines.append(f"- 当日订单终态：{seq}。")
    else:
        lines.append("- 当日无新建条件单。")
    return "\n".join(lines)


def store_engine_report(state, account_id: str, trade_date: str, *,
                        status: str = "normal", narrative: str = "",
                        conn=None) -> dict:
    """数据段 + 确定性 markdown 落 daily_reports（版本递增留痕）。settle_account 在
    结算事务内调用（conn 传同一写连接）→ 结算与首版日报同事务落盘；调度器修订/缺勤
    补生成也可单独调用（version+1 新行，不覆盖既有版本，spec-04 §5.3）。"""
    if status not in _ALLOWED_STATUS:
        raise ValueError(f"daily_reports.status 非法: {status}")
    c = conn or state_conn(state)
    ds = build_engine_data_section(state, account_id, trade_date, conn=c)
    markdown = render_engine_data_markdown(ds)
    row = c.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM daily_reports"
        " WHERE agent_id=? AND trade_date=?",
        (account_id, trade_date),
    ).fetchone()
    version = int(row["v"]) + 1
    rid = "dr" + secrets.token_hex(10)
    c.execute(
        "INSERT INTO daily_reports(id, agent_id, trade_date, version, data_section,"
        " narrative, merged_markdown, status, created_ts) VALUES (?,?,?,?,?,?,?,?,?)",
        (rid, account_id, trade_date, version, json.dumps(ds, ensure_ascii=False),
         narrative, markdown, status, _now_iso()),
    )
    return {"id": rid, "account_id": account_id, "trade_date": trade_date,
            "version": version, "status": status}


def fill_absent_report(state, account_id: str, trade_date: str, reason: str,
                       *, actor: str = "scheduler", conn=None) -> dict:
    """缺勤日报补生成（spec-04 §5.3）：结算任务未完成/缺口 → status=absent 落库。

    数据段照常补齐"可得部分"（账户当前现值口径 + annotations.unsettled 角标），叙述段
    = 原因说明。幂等护栏：该 (account_id, trade_date) 已有任意版本日报 → 直接返回
    skipped（结算成功后由后续版本补发，同日不再叠加 absent 行）。返回：
    {"skipped": True, "reason": reason} 或 {"skipped": False, "report": {…}}。"""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("缺勤日报须提供原因说明")
    reason = reason.strip()
    c = conn or state_conn(state)
    if c.execute(
        "SELECT 1 FROM daily_reports WHERE agent_id=? AND trade_date=? LIMIT 1",
        (account_id, trade_date),
    ).fetchone():
        return {"skipped": True, "reason": reason}
    if conn is None:
        from core.db import write_txn
        with write_txn(c) as cw:
            out = _insert_absent_locked(cw, account_id, trade_date, reason, actor)
        return out
    return _insert_absent_locked(c, account_id, trade_date, reason, actor)


def _insert_absent_locked(c, account_id: str, trade_date: str, reason: str,
                          actor: str) -> dict:
    """事务连接内插入 absent 日报 + 审计（调用方已持有写事务/连接）。"""
    ds = build_engine_data_section(None, account_id, trade_date, conn=c)
    markdown = render_engine_data_markdown(ds)
    rid = "dr" + secrets.token_hex(10)
    c.execute(
        "INSERT INTO daily_reports(id, agent_id, trade_date, version, data_section,"
        " narrative, merged_markdown, status, created_ts) VALUES (?,?,?,?,?,?,?,?,?)",
        (rid, account_id, trade_date, 1, json.dumps(ds, ensure_ascii=False),
         reason, markdown, "absent", _now_iso()),
    )
    c.execute(
        "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
        " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
        (_now_iso(), actor, "report.absent_auto", "daily_reports", rid, "absent",
         json.dumps({"trade_date": trade_date, "reason": reason[:400]},
                    ensure_ascii=False), ""),
    )
    return {"skipped": False,
            "report": {"id": rid, "account_id": account_id,
                       "trade_date": trade_date, "version": 1, "status": "absent"}}


def list_report_dates(state, account_id: str, conn=None) -> list[dict]:
    """日报时间线（spec-06 §6.6 数据源）：每日最新版本概览，新→旧。"""
    c = conn or state_conn(state)
    rows = c.execute(
        "SELECT trade_date, version, status, created_ts FROM daily_reports"
        " WHERE agent_id=? ORDER BY trade_date DESC, version DESC",
        (account_id,),
    ).fetchall()
    seen: dict[str, dict] = {}
    for r in rows:
        seen.setdefault(r["trade_date"], {
            "trade_date": r["trade_date"],
            "latest_version": r["version"],
            "status": r["status"],
            "latest_created_ts": r["created_ts"],
        })
    return [seen[d] for d in sorted(seen, reverse=True)]


def list_engine_reports(state, account_id: str, trade_date: str | None = None,
                        conn=None) -> list[dict]:
    """读 daily_reports 全部版本（新→旧）。data_section 已解析为 dict，供日报中心/API。"""
    c = conn or state_conn(state)
    if trade_date:
        rows = c.execute(
            "SELECT id, agent_id, trade_date, version, data_section, narrative,"
            " merged_markdown, status, created_ts FROM daily_reports"
            " WHERE agent_id=? AND trade_date=? ORDER BY version DESC",
            (account_id, trade_date),
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT id, agent_id, trade_date, version, data_section, narrative,"
            " merged_markdown, status, created_ts FROM daily_reports"
            " WHERE agent_id=? ORDER BY trade_date DESC, version DESC",
            (account_id,),
        ).fetchall()
    out = []
    for r in rows:
        try:
            ds = json.loads(r["data_section"] or "{}")
        except ValueError:
            ds = {}
        out.append({
            "id": r["id"], "trade_date": r["trade_date"], "version": r["version"],
            "status": r["status"], "narrative": r["narrative"],
            "merged_markdown": r["merged_markdown"], "data_section": ds,
            "created_ts": r["created_ts"],
        })
    return out


def update_narrative(state, account_id: str, trade_date: str, version: int,
                     narrative: str, actor: str) -> dict | None:
    """写叙述段（spec-04 §5.2 narrative 由日报任务/人工写入；同版本原地更新留审计，
    版本留痕语义由修订 v+1 承担）。返回更新后的版本概览；无该行返回 None。"""
    if not isinstance(narrative, str) or len(narrative) > 12000:
        raise ValueError("叙述段须为文本且不超过 12000 字")
    conn = state_conn(state)
    with write_txn(conn) as c:
        row = c.execute(
            "SELECT id, narrative FROM daily_reports"
            " WHERE agent_id=? AND trade_date=? AND version=?",
            (account_id, trade_date, version),
        ).fetchone()
        if row is None:
            return None
        old = row["narrative"]
        if old == narrative:
            return {"id": row["id"], "account_id": account_id,
                    "trade_date": trade_date, "version": version, "unchanged": True}
        c.execute(
            "UPDATE daily_reports SET narrative=? WHERE id=?", (narrative, row["id"]))
        c.execute(
            "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
            " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
            (_now_iso(), actor, "report.narrative_update", "daily_reports", row["id"],
             "updated", json.dumps({"old_len": len(old), "new_len": len(narrative)},
                                   ensure_ascii=False), ""),
        )
    return {"id": row["id"], "account_id": account_id,
            "trade_date": trade_date, "version": version, "unchanged": False}


def export_markdown(state, account_id: str, trade_date: str,
                    version: int | None = None, conn=None) -> str | None:
    """单篇导出（spec-06 §6.6）：数据段 merged_markdown +（若有）叙述段；无该版本 None。"""
    c = conn or state_conn(state)
    if version is not None:
        row = c.execute(
            "SELECT version, status, narrative, merged_markdown FROM daily_reports"
            " WHERE agent_id=? AND trade_date=? AND version=?",
            (account_id, trade_date, version),
        ).fetchone()
    else:
        row = c.execute(
            "SELECT version, status, narrative, merged_markdown FROM daily_reports"
            " WHERE agent_id=? AND trade_date=? ORDER BY version DESC LIMIT 1",
            (account_id, trade_date),
        ).fetchone()
    if row is None:
        return None
    parts = [row["merged_markdown"]]
    if row["narrative"]:
        parts += ["", "---", "## 叙述段", "", row["narrative"]]
    parts += ["", f"---", f"_版本 v{row['version']}（{row['status']}）· {account_id} · 确定性引擎数据段（spec-04 §5.2）_"]
    return "\n".join(parts)


# ---------------- 日报直达站内推送（spec-04 §6.2 事件→站内通知 web，P1 单通道） ----------------

_PUSH_STATUS_LABEL = {
    "normal": "结算日报",
    "absent": "缺勤日报（当日结算缺口，数据段为可得部分）",
    "resend": "修订补发",
}


def push_report_body(account_id: str, report: dict) -> str:
    """日报直达推送正文：确定性摘要（账户现值 + 叙述首段），纯文本无 LLM。"""
    status = report.get("status", "")
    header = f"【{report['trade_date']} · v{report.get('version')}】" \
             f"{_PUSH_STATUS_LABEL.get(status, status)}"
    lines = [header, ""]
    try:
        ds = json.loads(report.get("data_section") or "{}")
    except (TypeError, ValueError):
        ds = {}
    s = ds.get("summary") or {}
    lines.append(
        f"- 现金 {s.get('cash')} ｜ 净值 {s.get('nav')} ｜ "
        f"累计盈亏 {s.get('total_pnl')} ｜ 当日盈亏 {s.get('today_pnl')}"
    )
    narrative = (report.get("narrative") or "").strip()
    if narrative:
        preview = narrative if len(narrative) <= 160 else narrative[:160] + "…"
        lines += ["", f"> {preview}"]
    lines += ["", "详细数据段与历史版本请到「日报中心」查看。"]
    return "\n".join(lines)


def push_pending_report_deliveries(state, trade_date: str) -> list[dict]:
    """将该交易日最新一版结算日报/缺勤日报作为“日报直达”消息推送进用户会话
    （spec-04 §6.1 联系人式直达，非中转）；幂等：同 payload 已推送过则跳过。

    推送范围：main 策略账户（试运行/退役 Agent 全程不推，spec-04 §6.2 v0.6）；
    仅首版（version=1）normal/absent 直达——修订/补发以 v+1 留痕由日报中心承载，
    不重复轰炸；同一交易日已存在更高版本（后到补发）时首版也不再补推。返回新推送的
    messages（serialize 形态，含 conv_id，供 WS 广播）。
    """
    from core import chatstore  # noqa: PLC0415
    conn = state_conn(state)
    rows = conn.execute(
        """
        SELECT dr.agent_id, dr.trade_date, dr.version, dr.status, dr.narrative,
               dr.data_section
          FROM daily_reports dr
          JOIN accounts ac ON ac.id = dr.agent_id AND ac.role = 'main'
          JOIN agents ag ON ag.id = ac.agent_id
           AND ag.status NOT IN ('trial', 'archived')
         WHERE dr.trade_date = ?
           AND dr.status IN ('normal', 'absent')
           AND dr.version = 1
           AND NOT EXISTS (
               SELECT 1 FROM daily_reports dr3
                WHERE dr3.agent_id = dr.agent_id
                  AND dr3.trade_date = dr.trade_date
                  AND dr3.version > 1)
         ORDER BY dr.agent_id, dr.version
        """,
        (trade_date,),
    ).fetchall()
    pushed: list[dict] = []
    for r in rows:
        account_id = r["agent_id"]
        payload = json.dumps({
            "account_id": account_id,
            "trade_date": trade_date,
            "version": r["version"],
            "status": r["status"],
        }, ensure_ascii=False, sort_keys=True)
        try:
            conv = chatstore.ensure_user_chat(state, account_id)
        except LookupError:  # noqa: PERF203
            continue
        if conn.execute(
            "SELECT 1 FROM messages WHERE conv_id=? AND payload_ref=? LIMIT 1",
            (conv["id"], payload),
        ).fetchone():
            continue
        msg = chatstore.insert_message(
            state, conv_id=conv["id"], agent_id=account_id, direction="agent",
            msg_type="report", body=push_report_body(account_id, dict(r)),
            payload_ref=payload, status="delivered", delivered_via="web")
        try:
            audit(state, account_id, "report.push", result="ok",
                  object_type="message", object_id=msg["id"],
                  detail=f"{trade_date} v{r['version']} {r['status']} 日报直达站内推送")
        except Exception:  # noqa: BLE001
            pass
        pushed.append(msg)
    return pushed

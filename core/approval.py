"""审批流（spec-04 §4.1/§4.2/§4.4 最小确定性版，零 LLM）。

统一模式落地为独立 approval_requests 表；本模块承载确定性短路（内容哈希去重、
pending 配额、驳回冷却），决定效果器按 type 注册（当前实现 exemption = 写入
accounts.buy_exempt 豁免 token，eodengine 侧 ST/新股买入拦截读该列）。expired/
withdrawn 属"单失效"而非决定（§4.4：过期需重新申请），哈希去重只回放
approved/rejected 的已决结果。

管理 Agent LLM 未接入：决定方为单用户（decided_by=user），评审人工在审批中心
执行；审计 action 统一 approval.* 留痕。
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone

from core.db import state_conn, write_txn

APPROVAL_TYPES = ("task", "granularity", "capability", "risk", "exemption", "launch")
APPROVAL_STATUS = ("pending", "approved", "rejected", "expired", "withdrawn")

PENDING_LIMIT = 3            # §4.2：每 Agent 每类型 pending ≤3
COOLDOWN_SECONDS = 24 * 3600  # §4.2：驳回同类 24h 冷却（用户 UI 可豁免）
DEFAULT_EXPIRES_SECONDS = 24 * 3600  # §4.4：pending 默认 24h 过期

TYPE_LABEL = {
    "task": "任务", "granularity": "撮合粒度", "capability": "能力",
    "risk": "风险", "exemption": "豁免", "launch": "上线确认",
}
STATUS_LABEL = {
    "pending": "待决", "approved": "已通过", "rejected": "已驳回",
    "expired": "已过期", "withdrawn": "已撤回",
}

STATUS_MSG = {
    "pending": "审批待办", "approved": "已通过", "rejected": "已驳回",
    "expired": "已过期（24h 未决）",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _bj(iso: str) -> str:
    """UTC ISO → 北京时间 'YYYY-MM-DD HH:MM'（阅读用）。"""
    try:
        dt = datetime.fromisoformat(iso)
        return (dt + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def content_hash_of(type_: str, agent_id: str, payload: dict) -> str:
    """§4.2 内容哈希：type+agent_id+payload 规范化 JSON → sha256。"""
    canonical = json.dumps(
        {"type": type_, "agent_id": agent_id, "payload": payload},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _row_to_dict(r) -> dict:
    try:
        payload = json.loads(r["payload"] or "{}")
    except ValueError:
        payload = {}
    return {
        "id": r["id"], "type": r["type"], "agent_id": r["agent_id"],
        "payload": payload, "content_hash": r["content_hash"],
        "status": r["status"], "decided_by": r["decided_by"],
        "decided_ts": r["decided_ts"], "reason": r["reason"],
        "expires_ts": r["expires_ts"], "close_note": r["close_note"],
        "result_ref": r["result_ref"], "created_ts": r["created_ts"],
        "type_label": TYPE_LABEL.get(r["type"], r["type"]),
        "status_label": STATUS_LABEL.get(r["status"], r["status"]),
    }


def _effect_exemption(c, row: dict, now: str) -> str:
    """exemption 效果器：把 payload.tokens 并入该账户 accounts.buy_exempt（去重）。
    result_ref = 生效后的 buy_exempt JSON 文本。"""
    tokens = list(row["payload"].get("tokens", []))
    if not tokens:
        raise ValueError("豁免单须提供 tokens")
    acc = c.execute(
        "SELECT buy_exempt FROM accounts WHERE id=?", (row["agent_id"],)).fetchone()
    if acc is None:
        raise LookupError(f"账户不存在：{row['agent_id']}")
    try:
        existing = json.loads(acc["buy_exempt"] or "[]")
    except ValueError:
        existing = []
    if not isinstance(existing, list):
        existing = []
    merged: list[str] = []
    for t in list(existing) + tokens:
        if isinstance(t, str) and t not in merged:
            merged.append(t)
    c.execute(
        "UPDATE accounts SET buy_exempt=?, updated_ts=? WHERE id=?",
        (json.dumps(merged, ensure_ascii=False), now, row["agent_id"]),
    )
    return json.dumps(merged, ensure_ascii=False)


def _effect_granularity(c, row: dict, now: str) -> str:
    """granularity 效果器：账户撮合粒度变更（spec-04 §4.1 配置变更全程留痕）。
    payload={"granularity": "intraday_5m"}；写入 granularity_history + 账户当前值。"""
    allowed = ("eod_replay", "intraday_5m", "intraday_1m")
    target = row["payload"].get("granularity", "")
    if target not in allowed:
        raise ValueError(f"撮合粒度须为 {'/'.join(allowed)} 之一")
    acc = c.execute(
        "SELECT granularity, granularity_history FROM accounts WHERE id=?",
        (row["agent_id"],)).fetchone()
    if acc is None:
        raise LookupError(f"账户不存在：{row['agent_id']}")
    try:
        history = json.loads(acc["granularity_history"] or "[]")
    except ValueError:
        history = []
    if not isinstance(history, list):
        history = []
    from_ = acc["granularity"]
    if from_ == target:
        return json.dumps({"granularity": target, "changed": False},
                          ensure_ascii=False)
    history = list(history) + [{"from": from_, "to": target, "ts": now}]
    c.execute(
        "UPDATE accounts SET granularity=?, granularity_history=?, updated_ts=? WHERE id=?",
        (target, json.dumps(history, ensure_ascii=False), now, row["agent_id"]),
    )
    return json.dumps({"granularity": target, "changed": True,
                       "history_entries": len(history)}, ensure_ascii=False)


def _effect_single_stock_cap(c, row: dict, now: str) -> str:
    """risk 效果器：事故性单票上限下调（spec-01 §7 硬红线账户可配）。
    payload={"single_stock_cap": 0.2}；0<cap≤1。"""
    raw = row["payload"].get("single_stock_cap")
    if raw is None:
        raise ValueError("单票上限单须提供 single_stock_cap")
    try:
        cap = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("single_stock_cap 须为数值") from exc
    if not 0 < cap <= 1.0:
        raise ValueError("single_stock_cap 须在 (0, 1] 区间（1.0=满仓允许，越低越保守）")
    acc = c.execute(
        "SELECT single_stock_cap FROM accounts WHERE id=?", (row["agent_id"],)).fetchone()
    if acc is None:
        raise LookupError(f"账户不存在：{row['agent_id']}")
    cap_text = f"{cap:.4f}"
    c.execute("UPDATE accounts SET single_stock_cap=?, updated_ts=? WHERE id=?",
              (float(cap_text), now, row["agent_id"]))
    return json.dumps({"single_stock_cap": float(cap_text)}, ensure_ascii=False)


def _EFFECTS() -> dict:
    return {"exemption": _effect_exemption,
            "granularity": _effect_granularity,
            "risk": _effect_single_stock_cap}


def submit_approval(state, *, type_: str, agent_id: str, payload: dict,
                    reason: str = "", requested_by: str = "agent",
                    expires_seconds: int | None = None,
                    cooldown_seconds: int | None = None,
                    cooldown_exempt: bool = False) -> dict:
    """提交审批单（§4.1/§4.2 确定性短路）。

    返回：
      {"ok": True, "approval": {…}}              新建 pending 单；
      {"ok": False, "reason": <enum>, "detail": …} 短路退回（附上次决定/配额待决单/冷却剩余）。
    短路原因：hash_hit_approved / hash_hit_rejected / cooldown / pending_full。
    """
    if type_ not in APPROVAL_TYPES:
        raise ValueError(f"未知审批类型：{type_}")
    if not isinstance(payload, dict):
        raise ValueError("payload 须为 JSON 对象")
    if not reason or not reason.strip():
        raise ValueError("审批单须提供理由（策略依据）")
    reason = reason.strip()
    hash_ = content_hash_of(type_, agent_id, payload)
    cooldown = cooldown_seconds if cooldown_seconds is not None else COOLDOWN_SECONDS
    expires = expires_seconds if expires_seconds is not None else DEFAULT_EXPIRES_SECONDS
    now = _utcnow()
    now_iso = _now_iso()

    c = state_conn(state)

    # ① 内容哈希命中已决（approved/rejected）→ 附上次结果返回，不进评审（防申请-驳回-再申请振荡）
    prior = c.execute(
        "SELECT * FROM approval_requests WHERE content_hash=? AND status IN ('approved','rejected')"
        " ORDER BY decided_ts DESC LIMIT 1",
        (hash_,),
    ).fetchone()
    if prior is not None:
        prior_d = _row_to_dict(prior)
        if prior["status"] == "approved":
            return {"ok": False, "reason": "hash_hit_approved",
                    "detail": "相同申请此前已通过（确定性短路，零 LLM）",
                    "approval": prior_d}
        return {"ok": False, "reason": "hash_hit_rejected",
                "detail": "相同申请此前已驳回（确定性短路，零 LLM）",
                "approval": prior_d}

    # ② 驳回同类冷却（不同 payload）：cooldown_exempt 由用户 UI 人工放行（v0.3 B2）
    if not cooldown_exempt and cooldown > 0:
        recent = c.execute(
            "SELECT decided_ts FROM approval_requests"
            " WHERE agent_id=? AND type=? AND status='rejected'"
            " ORDER BY decided_ts DESC LIMIT 1",
            (agent_id, type_),
        ).fetchone()
        if recent and recent["decided_ts"]:
            decided = datetime.fromisoformat(recent["decided_ts"])
            remaining = cooldown - (now - decided).total_seconds()
            if remaining > 0:
                return {"ok": False, "reason": "cooldown",
                        "detail": f"同类申请被驳回后 24h 冷却中，剩余 {int(remaining)} 秒"
                                  "（可在审批中心人工放行豁免）"}

    # ③ pending 配额（每 Agent 每类型 ≤3）
    pending = c.execute(
        "SELECT id FROM approval_requests"
        " WHERE agent_id=? AND type=? AND status='pending' ORDER BY created_ts",
        (agent_id, type_),
    ).fetchall()
    if len(pending) >= PENDING_LIMIT:
        return {"ok": False, "reason": "pending_full",
                "detail": "同类待决审批单已达上限（≤3），请先处理："
                          + ", ".join(r["id"] for r in pending),
                "pending_ids": [r["id"] for r in pending]}

    # 通过全部短路 → 新建 pending 单
    rid = "ap" + secrets.token_hex(10)
    expires_iso = (now + timedelta(seconds=expires)).isoformat(timespec="seconds")
    payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    with write_txn(c) as cw:
        cw.execute(
            "INSERT INTO approval_requests (id, type, agent_id, payload, content_hash,"
            " status, reason, expires_ts, created_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (rid, type_, agent_id, payload_text, hash_, "pending", reason,
             expires_iso, now_iso),
        )
        cw.execute(
            "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
            " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
            (now_iso, requested_by, "approval.submit", "approval_requests", rid,
             "pending", json.dumps(
                 {"type": type_, "agent_id": agent_id, "reason": reason[:400]},
                 ensure_ascii=False), ""),
        )
    row = c.execute("SELECT * FROM approval_requests WHERE id=?", (rid,)).fetchone()
    ap_out = _row_to_dict(row)
    try:
        _notify_approval(state, kind="submit", approval=ap_out,
                         detail=f"{type_} 审批单提交待决 → 该 Agent 用户会话")
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "approval": ap_out}


def decide_approval(state, approval_id: str, *, decision: str, reason: str = "",
                    decided_by: str = "user") -> dict:
    """决定审批单（§4.1）：仅 pending 可决；通过 → 效果器生效写配置并留痕。

    返回 {"ok": True, "approval": {…}}；非 pending/已过期分别返回 ok=False。
    """
    if decision not in ("approved", "rejected"):
        raise ValueError(f"未知决定：{decision}")
    now_iso = _now_iso()
    c = state_conn(state)
    row = c.execute("SELECT * FROM approval_requests WHERE id=?",
                    (approval_id,)).fetchone()
    if row is None:
        return {"ok": False, "reason": "not_found"}
    if row["status"] != "pending":
        # 已决/已过期后到达的决定：仅留痕不生效（§4.4 迟到决定 / §4.2 二次决）
        with write_txn(c) as cw:
            cw.execute(
                "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
                " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
                (now_iso, decided_by, "approval.decide", "approval_requests",
                 approval_id, row["status"], "迟到/重复决定仅留痕不生效", ""),
            )
        return {"ok": False, "reason": "already",
                "detail": f"审批单当前为 {STATUS_LABEL.get(row['status'], row['status'])}，不可再决",
                "approval": _row_to_dict(row)}
    if row["expires_ts"] and row["expires_ts"] < now_iso:
        # §4.4 过期后到达的决定仅留痕不生效
        with write_txn(c) as cw:
            cw.execute(
                "UPDATE approval_requests SET status='expired', close_note='24h 未决自动过期',"
                " decided_by=?, decided_ts=?, reason=? WHERE id=?",
                (decided_by, now_iso, "迟到决定：单已过期，仅留痕不生效", approval_id),
            )
            cw.execute(
                "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
                " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
                (now_iso, decided_by, "approval.decide", "approval_requests",
                 approval_id, "expired", "迟到决定仅留痕", ""),
            )
        fresh = c.execute("SELECT * FROM approval_requests WHERE id=?",
                          (approval_id,)).fetchone()
        ap_out = _row_to_dict(fresh)
        try:
            _notify_approval(state, kind="decide", approval=ap_out,
                             detail="迟到决定：单已过期仅留痕（不生效）")
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "reason": "expired",
                "approval": ap_out}

    if decision == "approved":
        # 效果器与决定在同一写事务（读-改-写一致）
        result_ref = ""
        try:
            with write_txn(c) as cw:
                row_d = _row_to_dict(row)
                effect = _EFFECTS().get(row_d["type"])
                if effect is not None:
                    result_ref = effect(cw, row_d, now_iso)
                cw.execute(
                    "UPDATE approval_requests SET status='approved', decided_by=?,"
                    " decided_ts=?, reason=?, result_ref=?, close_note='' WHERE id=?",
                    (decided_by, now_iso, reason or "", result_ref, approval_id),
                )
                cw.execute(
                    "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
                    " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
                    (now_iso, decided_by, "approval.decide", "approval_requests",
                     approval_id, "approved",
                     json.dumps({"reason": (reason or "")[:400],
                                 "result_ref": result_ref[:400]},
                                ensure_ascii=False), ""),
                )
        except LookupError as exc:
            return {"ok": False, "reason": "effect_failed", "detail": str(exc)}
        except ValueError as exc:
            return {"ok": False, "reason": "effect_failed", "detail": str(exc)}
    else:
        with write_txn(c) as cw:
            cw.execute(
                "UPDATE approval_requests SET status='rejected', decided_by=?,"
                " decided_ts=?, reason=?, close_note='' WHERE id=?",
                (decided_by, now_iso, reason or "", approval_id),
            )
            cw.execute(
                "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
                " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
                (now_iso, decided_by, "approval.decide", "approval_requests",
                 approval_id, "rejected",
                 json.dumps({"reason": (reason or "")[:400]}, ensure_ascii=False), ""),
            )
    fresh = c.execute("SELECT * FROM approval_requests WHERE id=?",
                      (approval_id,)).fetchone()
    ap_out = _row_to_dict(fresh)
    try:
        _notify_approval(state, kind="decide", approval=ap_out,
                         detail=f"审批决定：{ap_out['status']} → 该 Agent 用户会话回执")
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "approval": ap_out}


def expire_overdue(state, now=None) -> int:
    """§4.4 过期清扫：pending 且 expires_ts < now → expired。调度器每 tick 调用。"""
    now_iso = (now or _utcnow()).isoformat(timespec="seconds")
    c = state_conn(state)
    overdue = c.execute(
        "SELECT id FROM approval_requests WHERE status='pending' AND expires_ts < ?",
        (now_iso,),
    ).fetchall()
    for r in overdue:
        with write_txn(c) as cw:
            cw.execute(
                "UPDATE approval_requests SET status='expired', close_note='24h 未决自动过期'"
                " WHERE id=? AND status='pending'",
                (r["id"],),
            )
            cw.execute(
                "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
                " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
                (now_iso, "scheduler", "approval.expire", "approval_requests",
                 r["id"], "expired", "24h 未决自动过期（§4.4）", ""),
            )
    return len(overdue)


def list_approvals(state, *, agent_id: str | None = None, status: str | None = None,
                   limit: int = 100, conn=None) -> list[dict]:
    """审批单列表（审批中心数据源），新→旧。"""
    c = conn or state_conn(state)
    sql = "SELECT * FROM approval_requests"
    where, params = [], []
    if agent_id:
        where.append("agent_id=?")
        params.append(agent_id)
    if status:
        where.append("status=?")
        params.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_ts DESC, id DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))
    return [_row_to_dict(r) for r in c.execute(sql, params).fetchall()]


def get_approval(state, approval_id: str, *, conn=None) -> dict | None:
    c = conn or state_conn(state)
    row = c.execute("SELECT * FROM approval_requests WHERE id=?",
                    (approval_id,)).fetchone()
    return _row_to_dict(row) if row else None


# ---------------- 审批事件站内消息（spec-04 §6.0：审批待办/回执入该 Agent 用户会话） ----------------

def _payload_snippet(payload: dict) -> str:
    try:
        s = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        s = str(payload)
    return s if len(s) <= 200 else s[:200] + "…"


def _notify_approval(state, *, kind: str, approval: dict, detail: str) -> dict | None:
    """把审批事件写成确定性 markdown 送入该 Agent 用户会话（联系人式直达）。

    kind: submit（审批待办）/ decide（审批回执）；msg_type 分别为 approval /
    approval_receipt。payload_ref 幂等键 = {kind, approval_id}，重跑零重复。
    """
    from core import chatstore  # noqa: PLC0415
    a = approval
    agent_name = ""
    try:
        agent = chatstore.get_agent(state, a["agent_id"])
        agent_name = agent["name"] if agent else ""
    except Exception:  # noqa: BLE001
        agent_name = ""
    if kind == "submit":
        msg_type = "approval"
        head = f"## 审批待办 · {a['type_label']}"
        lines = [
            head,
            f"- 单号：{a['id']}",
            f"- Agent：{agent_name or a['agent_id']}",
            f"- 类型：{a['type_label']}",
            f"- 内容：{_payload_snippet(a.get('payload') or {})}",
            f"- 理由：{a.get('reason') or '—'}",
            f"- 截止：{_bj(a['expires_ts'])}（24h 未决自动过期，spec-04 §4.4）",
            "- 处理：审批中心 → 通过 / 驳回（人工评审，决定方=登录用户）",
        ]
    else:
        msg_type = "approval_receipt"
        st = a.get("status", "")
        lines = [
            f"## 审批回执 · {a['type_label']} · {STATUS_MSG.get(st, st)}",
            f"- 单号：{a['id']}",
            f"- Agent：{agent_name or a['agent_id']}",
            f"- 决定方：{a.get('decided_by') or '—'} 于 "
            f"{_bj(a.get('decided_ts') or '')}",
            f"- 意见/说明：{a.get('reason') or a.get('close_note') or '—'}",
        ]
        if a.get("result_ref"):
            lines.append(f"- 生效值：{a['result_ref'][:200]}")
    body = "\n".join(lines)
    payload = json.dumps({"kind": kind, "approval_id": a["id"]},
                         ensure_ascii=False, sort_keys=True)
    try:
        conv = chatstore.ensure_user_chat(state, a["agent_id"])
    except LookupError:
        return None
    conn = state_conn(state)
    if conn.execute(
        "SELECT 1 FROM messages WHERE conv_id=? AND payload_ref=? LIMIT 1",
        (conv["id"], payload),
    ).fetchone():
        return None
    msg = chatstore.insert_message(
        state, conv_id=conv["id"], agent_id=a["agent_id"], direction="agent",
        msg_type=msg_type, body=body, payload_ref=payload,
        status="delivered", delivered_via="web")
    try:
        c = state_conn(state)
        with write_txn(c) as cw:
            cw.execute(
                "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
                " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
                (_now_iso(), "scheduler" if kind == "decide" else "agent",
                 "approval.notify", "message", msg["id"], "delivered",
                 detail[:400], ""),
            )
    except Exception:  # noqa: BLE001
        pass
    return msg

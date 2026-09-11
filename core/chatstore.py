"""会话/消息/Agent 注册表的存储访问（spec-02 §6.2、总纲 §3.5）。

单用户系统：无多租户；conversations 每个 agent+conv_type 唯一（联系人式单会话）。
所有写操作走 db.write_txn（单写队列）。时间一律 UTC ISO-8601（前端转北京时区）。
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone

from core.db import read_txn, state_conn, write_txn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _nid() -> str:
    return secrets.token_hex(10)


# ---------- Agent 注册表（生命周期状态机） ----------

def list_agents(state) -> list[dict]:
    conn = state_conn(state)
    with read_txn(conn) as c:
        rows = c.execute(
            "SELECT id, name, role, status, created_ts FROM agents ORDER BY"
            " CASE role WHEN 'manager' THEN 0 ELSE 1 END, name"
        ).fetchall()
    return [dict(r) for r in rows]


def get_agent(state, agent_id: str) -> dict | None:
    conn = state_conn(state)
    with read_txn(conn) as c:
        row = c.execute(
            "SELECT id, name, role, status, created_ts FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()
    return dict(row) if row else None


# ---------- Conversations（联系人式） ----------

def ensure_user_chat(state, agent_id: str) -> dict:
    """用户对话会话：agent+user_chat 唯一，存在即复用（spec-06 §6.1）。"""
    now = _now_iso()
    conn = state_conn(state)
    with write_txn(conn) as c:
        c.execute(
            "INSERT OR IGNORE INTO conversations (id, agent_id, conv_type, created_ts)"
            " SELECT ?, ?, 'user_chat', ?"
            " WHERE EXISTS (SELECT 1 FROM agents WHERE id = ?)",
            (_nid(), agent_id, now, agent_id),
        )
        row = c.execute(
            "SELECT id, agent_id, conv_type, created_ts FROM conversations"
            " WHERE agent_id = ? AND conv_type = 'user_chat'",
            (agent_id,),
        ).fetchone()
    if row is None:
        raise LookupError(f"Agent 不存在: {agent_id}")
    return dict(row)


def _row_to_conversation(c, row: dict) -> dict:
    last = None
    if row.get("last_ts"):
        last = {
            "id": row["last_id"],
            "direction": row["last_direction"],
            "msg_type": row["last_msg_type"],
            "status": row["last_status"],
            "body": row["last_body"],
            "ts": row["last_ts"],
        }
    return {
        "id": row["id"],
        "agent_id": row["agent_id"],
        "agent_name": row["agent_name"],
        "agent_role": row["agent_role"],
        "agent_status": row["agent_status"],
        "conv_type": row["conv_type"],
        "created_ts": row["created_ts"],
        "unread": row["unread"],
        "last_message": last,
    }


def list_conversations(state) -> list[dict]:
    conn = state_conn(state)
    with read_txn(conn) as c:
        rows = c.execute(
            """
            SELECT cv.id, cv.agent_id, cv.conv_type, cv.created_ts,
                   ag.name AS agent_name, ag.role AS agent_role, ag.status AS agent_status,
                   (SELECT COUNT(*) FROM messages m
                     WHERE m.conv_id = cv.id AND m.direction = 'agent'
                       AND m.status = 'delivered' AND m.read_ts = '') AS unread,
                   lm.id AS last_id, lm.direction AS last_direction,
                   lm.msg_type AS last_msg_type, lm.status AS last_status,
                   lm.body AS last_body, lm.ts AS last_ts
              FROM conversations cv
              JOIN agents ag ON ag.id = cv.agent_id
              LEFT JOIN messages lm ON lm.id = (
                  SELECT m2.id FROM messages m2
                   WHERE m2.conv_id = cv.id ORDER BY m2.ts DESC, m2.id DESC LIMIT 1)
            """
        ).fetchall()
    out = []
    for r in rows:
        out.append(_row_to_conversation(c, dict(r)))
    out.sort(key=lambda x: (x["last_message"] or {"ts": x["created_ts"]})["ts"], reverse=True)
    return out


# ---------- Messages ----------

def _serialize_message(row: dict) -> dict:
    payload = None
    if row.get("payload_ref"):
        try:
            payload = json.loads(row["payload_ref"])
        except (TypeError, json.JSONDecodeError):
            payload = None
    return {
        "id": row["id"],
        "conv_id": row["conv_id"],
        "agent_id": row["agent_id"],
        "direction": row["direction"],
        "msg_type": row["msg_type"],
        "body": row["body"],
        "payload": payload,
        "delivered_via": row["delivered_via"],
        "status": row["status"],
        "delivery_attempts": row["delivery_attempts"],
        "read_ts": row["read_ts"] or None,
        "ts": row["ts"],
    }


def insert_message(state, *, conv_id: str, agent_id: str, direction: str, msg_type: str,
                   body: str, payload_ref: str = "", status: str = "queued",
                   delivered_via: str = "") -> dict:
    mid = _nid()
    now = _now_iso()
    conn = state_conn(state)
    with write_txn(conn) as c:
        c.execute(
            "INSERT INTO messages (id, conv_id, agent_id, direction, msg_type, body,"
            " payload_ref, delivered_via, sync_to_manager, status, delivery_attempts,"
            " last_error, read_ts, ts) VALUES (?,?,?,?,?,?,?,?,0,?,0,'','',?)",
            (mid, conv_id, agent_id, direction, msg_type, body, payload_ref,
             delivered_via, status, now),
        )
    row = get_message(state, mid)
    assert row is not None
    return row


def get_message(state, message_id: str) -> dict | None:
    conn = state_conn(state)
    with read_txn(conn) as c:
        row = c.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    return _serialize_message(dict(row)) if row else None


def get_conversation(state, conv_id: str) -> dict | None:
    conn = state_conn(state)
    with read_txn(conn) as c:
        row = c.execute(
            "SELECT id, agent_id, conv_type, created_ts FROM conversations WHERE id = ?",
            (conv_id,),
        ).fetchone()
    return dict(row) if row else None


def set_message_status(state, message_id: str, status: str,
                       last_error: str = "") -> dict | None:
    conn = state_conn(state)
    with write_txn(conn) as c:
        c.execute("UPDATE messages SET status = ?, last_error = ? WHERE id = ?",
                  (status, last_error, message_id))
    return get_message(state, message_id)


def list_messages(state, conv_id: str, *, before_ts: str | None = None,
                  before_id: str | None = None, limit: int = 50) -> tuple[list[dict], bool]:
    """按 (ts,id) 复合游标倒序取最近 N 条，返回 (升序消息, 是否还有更早)。"""
    conn = state_conn(state)
    params: list = []
    where = "conv_id = ?"
    params.append(conv_id)
    if before_ts:
        if before_id:
            # 复合游标：同秒多消息也不遗漏/重复（id 单调递增，二级排序）
            where += " AND (ts < ? OR (ts = ? AND id < ?))"
            params.extend([before_ts, before_ts, before_id])
        else:
            where += " AND ts < ?"
            params.append(before_ts)
    params.append(limit + 1)
    with read_txn(conn) as c:
        rows = c.execute(
            f"SELECT * FROM messages WHERE {where}"
            " ORDER BY ts DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
    has_older = len(rows) > limit
    page = [dict(r) for r in rows[:limit]]
    page.reverse()
    return [_serialize_message(r) for r in page], has_older


def mark_conversation_read(state, conv_id: str) -> int:
    now = _now_iso()
    conn = state_conn(state)
    with write_txn(conn) as c:
        cur = c.execute(
            "UPDATE messages SET read_ts = ? WHERE conv_id = ? AND direction = 'agent'"
            " AND status = 'delivered' AND read_ts = ''",
            (now, conv_id),
        )
        updated = cur.rowcount
    return updated


# ---------- 消息白名单与审阅（spec-02 §6.2 / §10.4） ----------

# 子 Agent → 用户的消息白名单（不可闲聊）；非白名单落 pending_review 等管理 Agent 审阅
AGENT_MSG_WHITELIST = ("日报", "异常上报", "审批回执", "提问回复", "总汇报", "系统事件")


def _audit(state, *, action: str, result: str, object_id: str, actor: str,
           detail: str) -> None:
    with write_txn(state_conn(state)) as c:
        c.execute(
            "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
            " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
            (_now_iso(), actor, action, "message", object_id, result,
             detail[:400], ""))


def deliver_agent_message(state, *, conv_id: str, agent_id: str, msg_type: str,
                          body: str, payload_ref: str = "",
                          delivered_via: str = "web",
                          actor: str = "engine") -> dict:
    """子 Agent → 用户投递入口：白名单强制校验，非白名单转 pending_review。"""
    if msg_type not in AGENT_MSG_WHITELIST:
        msg = insert_message(
            state, conv_id=conv_id, agent_id=agent_id, direction="agent",
            msg_type=msg_type, body=body, payload_ref=payload_ref,
            status="pending_review", delivered_via="")
        _audit(state, action="message.pending_review", result="pending_review",
               object_id=msg["id"], actor=actor,
               detail=f"非白名单 msg_type={msg_type}，转管理 Agent 审阅")
        return {"message": msg, "delivered": False, "pending_review": True}
    msg = insert_message(
        state, conv_id=conv_id, agent_id=agent_id, direction="agent",
        msg_type=msg_type, body=body, payload_ref=payload_ref,
        status="delivered", delivered_via=delivered_via)
    return {"message": msg, "delivered": True, "pending_review": False}


def list_pending_reviews(state, *, limit: int = 100) -> list[dict]:
    conn = state_conn(state)
    with read_txn(conn) as c:
        rows = c.execute(
            "SELECT * FROM messages WHERE status='pending_review'"
            " ORDER BY ts ASC LIMIT ?", (int(limit),)).fetchall()
    return [_serialize_message(dict(r)) for r in rows]


def review_message(state, message_id: str, *, approve: bool,
                   reviewer: str = "manager", reason: str = "") -> dict:
    """管理 Agent 审阅：放行转 delivered，拒绝转 failed（审阅留痕）。"""
    msg = get_message(state, message_id)
    if msg is None:
        raise LookupError(f"消息不存在：{message_id}")
    if msg["status"] != "pending_review":
        return {"message": msg, "changed": False,
                "reason": f"非待审阅状态：{msg['status']}"}
    if approve:
        updated = set_message_status(state, message_id, "delivered")
        _audit(state, action="message.review_release", result="delivered",
               object_id=message_id, actor=reviewer,
               detail=reason or "审阅放行转 delivered")
        return {"message": updated, "changed": True, "approved": True}
    updated = set_message_status(state, message_id, "failed",
                                 last_error=reason or "管理 Agent 审阅拒绝")
    _audit(state, action="message.review_reject", result="failed",
           object_id=message_id, actor=reviewer,
           detail=reason or "管理 Agent 审阅拒绝")
    return {"message": updated, "changed": True, "approved": False}

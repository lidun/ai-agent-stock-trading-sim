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

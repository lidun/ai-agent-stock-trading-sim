"""能力配置中心只读域（spec-05 §2.1/§2.4，capabilities + capability_bindings）。

本片为"统一域"落地：建表 + 只读聚合 + 受控 seed（register/bind/unbind 仅测试与
迁移用，非公开 API）。公开写方=管理 Agent 走 spec-05 §2.2 上架四门槛 + spec-04
申请-下发审批流（type=capability），语义本片不定义，路由不开放写。

约束（§2.4）：
- 能力=一行 (name, version)，id 为 ULID；升级=新版本新行，子 Agent 可保留旧引用；
- 绑定=capability 行 × agent 一条，unbound_ts 留痕（解绑/回滚可查）；
- 同一 (capability_id, version, agent) 至多一条在绑（db 部分唯一索引兜底）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone

from core.db import state_conn

CAP_TYPES = ("skill", "tool", "mcp", "datasource")
SOURCE_TYPES = ("opensource", "api", "selfmade")
_SANDBOX_OK = "passed"
_STATUS = ("active", "deprecated")
_MANAGER = "agent-manager"  # spec-05 §2.1 maintainer/bound_by（管理 Agent）

_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _now_ts_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ulid() -> str:
    """Crockford base32 ULID（spec-05 §2.1 id）：48 位毫秒 + 80 位随机。"""
    ts_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = int.from_bytes(os.urandom(10), "big")
    value = (ts_ms << 80) | rand
    return "".join(
        _ULID_ALPHABET[(value >> (5 * i)) & 31] for i in range(25, -1, -1)
    )


def _parse_meta(raw: str) -> dict:
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
        return loaded if isinstance(loaded, dict) else {"raw": loaded}
    except (TypeError, ValueError):
        return {}


def _require_agent(state, agent_id: str) -> None:
    c = state_conn(state)
    if c.execute("SELECT 1 FROM agents WHERE id=?", (agent_id,)).fetchone() is None:
        raise LookupError(f"Agent {agent_id} 不存在")


def _require_capability(state, capability_id: str) -> dict | None:
    c = state_conn(state)
    row = c.execute("SELECT * FROM capabilities WHERE id=?", (capability_id,)).fetchone()
    return _serialize(row) if row else None


def _serialize(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["capability_type"],
        "version": row["version"],
        "description": row["description"],
        "maintainer": row["maintainer"],
        "source_type": row["source_type"],
        "source_ref": row["source_ref"],
        "sandbox_status": row["sandbox_status"],
        "sandbox_report_ref": row["sandbox_report_ref"],
        "metadata": _parse_meta(row["metadata"]),
        "status": row["status"],
        "created_ts": row["created_ts"],
        "updated_ts": row["updated_ts"],
    }


def catalog(state, *, capability_type: str = "", status: str = "",
            keyword: str = "", limit: int = 200) -> dict:
    """能力目录：类型/状态过滤 + 关键词命中 name/description + 在绑数。"""
    limit = max(1, min(int(limit), 500))
    c = state_conn(state)
    clauses, params = ["1=1"], []
    if capability_type:
        clauses.append("capability_type = ?")
        params.append(capability_type)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if keyword:
        clauses.append("(name LIKE ? OR description LIKE ?)")
        params += [f"%{keyword}%", f"%{keyword}%"]
    where = " AND ".join(clauses)
    rows = c.execute(
        f"""
        SELECT cp.*,
               (SELECT COUNT(*) FROM capability_bindings cb
                 WHERE cb.capability_id = cp.id AND cb.unbound_ts = '') AS active_bindings
          FROM capabilities cp
         WHERE {where}
         ORDER BY cp.name ASC, cp.version DESC
         LIMIT ?
        """,
        params + [limit],
    ).fetchall()
    items = [_serialize(r) for r in rows]
    for it, r in zip(items, rows):
        it["active_bindings"] = int(r["active_bindings"])
    return {
        "total": len(items),
        "types": list(CAP_TYPES),
        "items": items,
    }


def detail(state, capability_id: str) -> dict:
    """能力详情 + 绑定清单（含解绑留痕，spec-05 §2.4 可查可回滚）。"""
    cap = _require_capability(state, capability_id)
    if cap is None:
        raise LookupError(f"Capability {capability_id} 不存在")
    c = state_conn(state)
    rows = c.execute(
        """
        SELECT cb.id AS bid, cb.bound_by, cb.bound_ts, cb.unbound_ts,
               ag.name AS agent_name
          FROM capability_bindings cb
          JOIN agents ag ON ag.id = cb.agent_id
         WHERE cb.capability_id = ?
         ORDER BY cb.bound_ts DESC
        """,
        (capability_id,),
    ).fetchall()
    bindings = [
        {
            "binding_id": r["bid"],
            "bound_by": r["bound_by"],
            "bound_ts": r["bound_ts"],
            "unbound_ts": r["unbound_ts"],
            "active": r["unbound_ts"] == "",
            "agent_id": None,
            "agent_name": r["agent_name"],
        }
        for r in rows
    ]
    # 补 agent_id（bindings 无 display 列，join agents 拿 name）
    rows_all = c.execute(
        "SELECT id, agent_id FROM capability_bindings WHERE capability_id=?",
        (capability_id,),
    ).fetchall()
    by_id = {r["id"]: r["agent_id"] for r in rows_all}
    for b in bindings:
        b["agent_id"] = by_id.get(b["binding_id"])
    cap["active_bindings"] = sum(1 for b in bindings if b["active"])
    return {"capability": cap, "bindings": bindings}


def agent_bindings(state, agent_id: str, *, include_unbound: bool = False) -> dict:
    """某 Agent 绑定清单（spec-05 §2.3/§2.4）：默认仅在绑。"""
    _require_agent(state, agent_id)
    c = state_conn(state)
    cond = "cb.unbound_ts = ''" if not include_unbound else "1=1"
    rows = c.execute(
        f"""
        SELECT cb.id AS bid, cb.version, cb.bound_by, cb.bound_ts, cb.unbound_ts,
               cp.id AS capability_id, cp.name, cp.capability_type,
               cp.description, cp.status AS cap_status, cp.sandbox_status
          FROM capability_bindings cb
          JOIN capabilities cp ON cp.id = cb.capability_id
         WHERE cb.agent_id = ? AND {cond}
         ORDER BY cb.bound_ts DESC
        """,
        (agent_id,),
    ).fetchall()
    items = []
    for r in rows:
        items.append({
            "binding_id": r["bid"],
            "capability_id": r["capability_id"],
            "name": r["name"],
            "type": r["capability_type"],
            "version": r["version"],
            "description": r["description"],
            "capability_status": r["cap_status"],
            "sandbox_status": r["sandbox_status"],
            "bound_by": r["bound_by"],
            "bound_ts": r["bound_ts"],
            "unbound_ts": r["unbound_ts"],
        })
    return {"agent_id": agent_id, "total": len(items), "items": items}


def register(state, *, name: str, capability_type: str, version: str,
             description: str, source_type: str, source_ref: str,
             maintainer: str = _MANAGER, metadata: dict | None = None,
             sandbox_report_ref: str = "",
             sandbox_status: str = _SANDBOX_OK,
             status: str = "active") -> dict:
    """注册能力（受控 seed/未来写入口共用；四门槛齐备才落库，spec-05 §2.2）。

    门槛：①元数据完整（名称/描述/版本/维护者）②沙箱通过 ③来源与许可留痕。
    """
    _require_agent(state, maintainer)  # maintainer 必须是既有 Agent（管理侧）
    for label, value in (
        ("capability_type", capability_type), ("source_type", source_type),
        ("status", status), ("sandbox_status", sandbox_status),
    ):
        if label == "capability_type" and value not in CAP_TYPES:
            raise ValueError(f"能力类型不合法：{value}")
        if label == "source_type" and value not in SOURCE_TYPES:
            raise ValueError(f"来源类型不合法：{value}")
        if label == "status" and value not in _STATUS:
            raise ValueError(f"状态不合法：{value}")
        if label == "sandbox_status" and value not in (
            "pending", "passed", "failed"):
            raise ValueError(f"沙箱状态不合法：{value}")
    for field, value in (("name", name), ("description", description),
                         ("version", version), ("source_ref", source_ref)):
        if not value or not str(value).strip():
            raise ValueError(f"{field} 缺失：元数据不完整（门槛①/③）")
    if sandbox_status != _SANDBOX_OK:
        raise ValueError("sandbox_status 必须为 passed（门槛②）")

    rid = _ulid()
    ts = _now_ts_iso()
    c = state_conn(state)
    from core.db import write_txn  # noqa: PLC0415
    try:
        with write_txn(c) as cw:
            cw.execute(
                "INSERT INTO capabilities"
                " (id, name, capability_type, version, description, maintainer,"
                "  source_type, source_ref, sandbox_status, sandbox_report_ref,"
                "  metadata, status, created_ts, updated_ts)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, name, capability_type, version, description, maintainer,
                 source_type, source_ref, sandbox_status, sandbox_report_ref,
                 json.dumps(metadata or {}, ensure_ascii=False), status, ts, ts),
            )
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"能力已存在或参数不合法：{exc}") from exc
    cap = _require_capability(state, rid)
    assert cap is not None
    return cap


def bind(state, *, capability_id: str, agent_id: str,
         bound_by: str = _MANAGER) -> dict:
    """绑定能力给子 Agent（受控 seed）。在绑同版本幂等；废弃/沙箱未过不可下发。"""
    _require_agent(state, agent_id)
    cap = _require_capability(state, capability_id)
    if cap is None:
        raise LookupError(f"Capability {capability_id} 不存在")
    if cap["status"] == "deprecated":
        raise ValueError("能力已 deprecated，不再下发（存量绑定通知迁移，§2.4）")
    if cap["sandbox_status"] != _SANDBOX_OK:
        raise ValueError("沙箱未通过，不得绑定（门槛②）")

    ts = _now_ts_iso()
    bid = _ulid()
    c = state_conn(state)
    from core.db import write_txn  # noqa: PLC0415
    with write_txn(c) as cw:
        cw.execute(
            "INSERT OR IGNORE INTO capability_bindings"
            " (id, capability_id, version, agent_id, bound_by, bound_ts,"
            "  unbound_ts, created_ts)"
            " VALUES (?,?,?,?,?,?,'',?)",
            (bid, capability_id, cap["version"], agent_id, bound_by, ts, ts),
        )
    return {"capability_id": capability_id, "agent_id": agent_id,
            "version": cap["version"], "bound_ts": ts}


def unbind(state, *, capability_id: str, agent_id: str,
           audit_actor: str = "") -> dict:
    """解绑能力（留痕，spec-05 §2.4 可查可回滚）：置 unbound_ts，不删行。

    audit_actor 传入时同事务写 audit_logs（管理侧经路由解绑；空=脚本/种子不审计）。
    """
    _require_agent(state, agent_id)
    cap = _require_capability(state, capability_id)
    if cap is None:
        raise LookupError(f"Capability {capability_id} 不存在")
    ts = _now_ts_iso()
    c = state_conn(state)
    from core.db import write_txn  # noqa: PLC0415
    with write_txn(c) as cw:
        row = cw.execute(
            "SELECT id FROM capability_bindings"
            " WHERE unbound_ts='' AND capability_id=? AND agent_id=? LIMIT 1",
            (capability_id, agent_id),
        ).fetchone()
        cw.execute(
            "UPDATE capability_bindings SET unbound_ts=? WHERE unbound_ts=''"
            " AND capability_id=? AND agent_id=?",
            (ts, capability_id, agent_id),
        )
        if audit_actor:
            cw.execute(
                "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
                " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
                (ts, audit_actor, "capability.unbind", "capability_bindings",
                 row["id"] if row is not None else capability_id, "unbound",
                 f"能力 {capability_id} 从 Agent {agent_id} 解绑（spec-05 §2.4）", ""),
            )
    return {"capability_id": capability_id, "agent_id": agent_id, "unbound_ts": ts}


def deprecate(state, *, capability_id: str, reason: str = "",
              audit_actor: str = "") -> dict:
    """停用能力目录项（spec-05 §2.2 状态机 → deprecated，管理侧维护）。

    停用后 catalog 可被 status=deprecated 过滤、bind() 拒绝新下发；存量在绑不动
    （§2.4 存量引用留痕/通知迁移语义）。audit_actor 传入时同事务写 audit_logs。
    幂等：已是 deprecated 直接返回现状。
    """
    cap = _require_capability(state, capability_id)
    if cap is None:
        raise LookupError(f"Capability {capability_id} 不存在")
    if cap["status"] == "deprecated":
        return {"capability_id": capability_id, "status": "deprecated",
                "deprecated_ts": cap["updated_ts"], "already": True}
    ts = _now_ts_iso()
    c = state_conn(state)
    from core.db import write_txn  # noqa: PLC0415
    with write_txn(c) as cw:
        cw.execute(
            "UPDATE capabilities SET status='deprecated', updated_ts=? WHERE id=?",
            (ts, capability_id),
        )
        if audit_actor:
            detail_txt = f"能力 {capability_id} 停用（deprecated）"
            if reason and str(reason).strip():
                detail_txt += f"：{str(reason).strip()}"
            detail_txt += "（spec-05 §2.2）"
            cw.execute(
                "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
                " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
                (ts, audit_actor, "capability.deprecate", "capabilities",
                 capability_id, "deprecated", detail_txt, ""),
            )
    return {"capability_id": capability_id, "status": "deprecated",
            "deprecated_ts": ts, "already": False}

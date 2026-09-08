"""策略章程只读聚合（spec-06 §6.4 策略理念区块，spec-05 §4.1 双层结构语义）。

载体 `strategy_charter_versions`（迁移 v20）：每个 Agent 按 version_no 快照的
核心理念与结构层。本片为只读落地（方案 A）：
- 理念文本不落 config、版本化存储；locked=1 = 核心理念锁定，变更需用户授权；
  写入口由策略发布/管理 Agent 后续切片提供，这里不定义作者语义。
- 能力包绑定属 spec-05 §5 能力配置中心（上架/申请-下发闭环），本片仅留空态占位，
  详情页区块在无 charter/能力包时如实呈现空态并标注写入方。

不新建记忆表：spec-02 memory_entries(type=strategy) 的演进记录（前/后/依据/预期/
结果逐条挂 version_no）在 spec-02 §3 记忆双版本切片落地，本片不重复建表。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from core.db import state_conn


def _now_ts_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _to_bool(v) -> bool:
    return bool(v)


def _parse_layers(raw: str) -> dict:
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
        return loaded if isinstance(loaded, dict) else {"raw": loaded}
    except (TypeError, ValueError):
        return {}


def _require_agent(state, agent_id: str) -> None:
    from core.accountstore import accounts_for_agent  # noqa: PLC0415
    if not accounts_for_agent(state, agent_id):
        raise LookupError(f"Agent {agent_id} 不存在或无账户")


def _row_to_summary(row) -> dict:
    return {
        "version_no": row["version_no"],
        "active": _to_bool(row["active"]),
        "locked": _to_bool(row["locked"]),
        "charter_hash": row["charter_hash"],
        "note": row["note"],
        "created_ts": row["created_ts"],
    }


def _full_row(state, agent_id: str, version_no: str) -> dict | None:
    c = state_conn(state)
    row = c.execute(
        "SELECT * FROM strategy_charter_versions"
        " WHERE agent_id=? AND version_no=?",
        (agent_id, version_no),
    ).fetchone()
    if row is None:
        return None
    return {
        "version_no": row["version_no"],
        "active": _to_bool(row["active"]),
        "locked": _to_bool(row["locked"]),
        "core_belief": row["core_belief"],
        "layers": _parse_layers(row["layers"]),
        "charter_hash": row["charter_hash"],
        "note": row["note"],
        "created_ts": row["created_ts"],
    }


def profile(state, agent_id: str) -> dict:
    """当前 Agent 的理念快照：active 章程全量 + 版本摘要列表（升序旧→新）。"""
    _require_agent(state, agent_id)
    c = state_conn(state)
    rows = c.execute(
        "SELECT * FROM strategy_charter_versions WHERE agent_id=? ORDER BY created_ts, version_no",
        (agent_id,),
    ).fetchall()
    versions = [_row_to_summary(r) for r in rows]
    active = next((v for v in versions if v["active"]), None)
    from core import capability_center  # noqa: PLC0415
    bound = capability_center.agent_bindings(state, agent_id)
    capability_packs = [
        {
            "name": b["name"],
            "type": b["type"],
            "version": b["version"],
            "description": b["description"],
            "bound_by": b["bound_by"],
            "bound_ts": b["bound_ts"],
        }
        for b in bound["items"]
        if b["capability_status"] == "active"
    ]
    return {
        "agent_id": agent_id,
        "active": _full_row(state, agent_id, active["version_no"]) if active else None,
        "versions": versions,
        "has_capability_packs": bool(capability_packs),
        "capability_packs": capability_packs,  # spec-05 §2 绑定清单（active 项）
    }


def version_detail(state, agent_id: str, version_no: str) -> dict:
    """单版本章程全量（版本链展开读取）。"""
    _require_agent(state, agent_id)
    full = _full_row(state, agent_id, version_no)
    if full is None:
        raise LookupError(f"Agent {agent_id} 无 charter 版本 {version_no}")
    return {"agent_id": agent_id, "version": full}


def write_seed(state, agent_id: str, *, version_no: str, core_belief: str,
               layers: dict | None = None, locked: bool = True,
               charter_hash: str = "", note: str = "", active: bool = True) -> None:
    """受控写入种子/未来写入口共用（当前仅测试与迁移用，非公开 API）。

    保持"每 Agent 至多一个 active"不变量（db 层部分唯一索引兜底 + 应用层先复位）。
    """
    _require_agent(state, agent_id)
    c = state_conn(state)
    from core.db import write_txn  # noqa: PLC0415
    with write_txn(c) as cw:
        if active:
            cw.execute(
                "UPDATE strategy_charter_versions SET active=0 WHERE agent_id=?",
                (agent_id,),
            )
        cw.execute(
            "INSERT OR REPLACE INTO strategy_charter_versions"
            " (id, agent_id, version_no, active, core_belief, layers, locked,"
            "  charter_hash, note, created_ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"{agent_id}:{version_no}", agent_id, version_no, 1 if active else 0,
             core_belief, json.dumps(layers or {}, ensure_ascii=False),
             1 if locked else 0, charter_hash, note, _now_ts_iso()),
        )

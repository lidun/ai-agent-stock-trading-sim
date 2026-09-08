"""知识与经验库（spec-05 §3：条目模型/状态机/权限软删，管理 Agent 评审由用户执行）。

证据源=signal_registry 客观统计（spec-05 §3.2）——本模块承载条目生命周期与
统计快照读写；状态晋升/降级的"数据结论"后续由日结算后确定性重算写入 kb_stats，
管理 Agent/用户在本模块内完成评审确认（review_gate2_ref 留痕，decided_by=user，
与审批域同一口径：LLM 未接入前由登录用户代行管理 Agent 评审）。

- 入库双闸（§3.4）：闸1=事前可计算性（本模块确定性校验 computable_spec，
  记录 review_gate1_ref）；闸2=管理 Agent 逻辑评审（人工执行，复核留痕于
  review_gate2_ref，启动验证时填写）。
- 软删（§3.7）：仅删除=软删+审计；历史 kb_stats 保留。
- UCB 排序载体 n_i=kb_stats.dispatch_n（§3.9），本模块读写不计算排序。
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone

from core.db import state_conn, write_txn

KB_TYPES = ("positive", "pitfall")
KB_STATUS = ("observing", "validating", "valid", "invalid", "sealed")
KB_SOURCES = ("user", "retrospective", "manager_observation", "market_anomaly")
SEVERITIES = ("high", "mid", "low")

TYPE_LABEL = {"positive": "正向概念", "pitfall": "反向避坑"}
STATUS_LABEL = {
    "observing": "观察中", "validating": "验证中", "valid": "有效",
    "invalid": "已失效", "sealed": "已封存",
}
SOURCE_LABEL = {
    "user": "用户录入", "retrospective": "归档/复盘",
    "manager_observation": "管理观察", "market_anomaly": "市场异动",
}

# §3.1 computable_spec 约定键（事前可计算性：触发识别/计算口径/所需数据）
SPEC_TRIGGER = "trigger_rule"
SPEC_COMPUTE = "computation"
SPEC_DATA = "data_sources"

TRANSITIONS: dict[str, set[str]] = {
    # 状态机（§3.2）：观察 → 验证 → 有效；验证/观察可失效；验证证据不足可封存；
    # 失效/封存可经复核重新进入验证（新证据周期）。
    "start_validation": {"observing", "invalid", "sealed"},
    "approve_valid": {"validating"},
    "invalidate": {"observing", "validating", "valid"},
    "seal": {"validating"},
}

# 允许的状态迁移：action -> (from_status, 需填原因字段)
ACTION_NOTES: dict[str, str] = {
    "start_validation": "review_gate2_ref",
    "approve_valid": "review_gate2_ref",
    "invalidate": "invalid_reason",
    "seal": "sealed_reason",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_dict(r) -> dict:
    def loads(s: str):
        try:
            return json.loads(s or "{}")
        except ValueError:
            return {}

    return {
        "id": r["id"], "type": r["type"], "name": r["name"],
        "description": r["description"],
        "computable_spec": loads(r["computable_spec"]),
        "severity": r["severity"], "env_scope": r["env_scope"],
        "status": r["status"], "invalid_reason": r["invalid_reason"],
        "sealed_reason": r["sealed_reason"], "source": r["source"],
        "created_by": r["created_by"], "origin_agent": r["origin_agent"],
        "review_gate1_ref": loads(r["review_gate1_ref"]),
        "review_gate2_ref": loads(r["review_gate2_ref"]),
        "deleted_ts": r["deleted_ts"], "created_ts": r["created_ts"],
        "updated_ts": r["updated_ts"],
        "type_label": TYPE_LABEL.get(r["type"], r["type"]),
        "status_label": STATUS_LABEL.get(r["status"], r["status"]),
    }


def _audit(c, *, ts: str, actor: str, action: str, object_id: str,
           result: str, detail: str) -> None:
    c.execute(
        "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
        " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
        (ts, actor, action, "kb_entries", object_id, result, detail[:400], ""),
    )


def _next_kb_id(c) -> str:
    row = c.execute("SELECT MAX(CAST(SUBSTR(id, 4) AS INTEGER)) AS m FROM kb_entries"
                    ).fetchone()
    n = int(row["m"] or 0) + 1
    return f"KB-{n:04d}"


def validate_computable_spec(spec) -> list[str]:
    """§3.4 闸1 事前可计算性字段校验（确定性，零 LLM）。

    约定触发识别条件/计算口径/所需数据三要素齐全且为非空文本；data_sources 可为
    列表或字符串。返回缺失项列表（空=通过）。"""
    if not isinstance(spec, dict):
        return ["computable_spec 须为 JSON 对象"]
    missing: list[str] = []
    for key, label in ((SPEC_TRIGGER, "触发识别条件"), (SPEC_COMPUTE, "计算口径"),
                       (SPEC_DATA, "所需数据")):
        val = spec.get(key)
        if isinstance(val, list):
            ok = bool(val) and all(str(x).strip() for x in val)
        else:
            ok = bool(str(val or "").strip())
        if not ok:
            missing.append(f"{label}({key}) 缺失或为空")
    if not isinstance(spec.get(SPEC_DATA), (str, list)):
        missing.append("data_sources 须为字符串或列表")
    return missing


def create_entry(state, *, name: str, type_: str, description: str = "",
                 computable_spec: dict | None = None,
                 severity: str = "", env_scope: str = "all",
                 source: str = "user", origin_agent: str | None = None,
                 created_by: str = "user", allow_missing_positive_spec: bool = True,
                 ) -> dict:
    """新建知识库条目（§3.1/§3.4）。

    状态初始=observing；闸1（computable_spec 完整性）确定性校验，pitfall 与
    positive 选股类必填（positive 纯方法论可缺省，allow_missing_positive_spec）。
    闸2（管理评审）在"启动验证"时留痕 review_gate2_ref。"""
    if type_ not in KB_TYPES:
        raise ValueError(f"未知条目类型：{type_}（positive|pitfall）")
    if source not in KB_SOURCES:
        raise ValueError(f"未知来源：{source}")
    name = (name or "").strip()
    if not name:
        raise ValueError("条目须有名称")
    description = (description or "").strip()
    spec = computable_spec if computable_spec is not None else {}
    checks: list[str] = []
    if type_ == "pitfall":
        if severity not in SEVERITIES:
            raise ValueError(f"避坑条目 severity 须为 {'/'.join(SEVERITIES)} 之一")
        checks = validate_computable_spec(spec)
        if checks:
            raise ValueError("闸1 未通过（事前可计算性）：" + "；".join(checks))
    elif spec:
        checks = validate_computable_spec(spec)
        if checks:
            raise ValueError("闸1 未通过（事前可计算性）：" + "；".join(checks))
        if not allow_missing_positive_spec and spec == {}:
            raise ValueError("选股类概念须提供 computable_spec")
    checks.append("必填字段完整（name/description）")
    env_scope = (env_scope or "all").strip() or "all"
    origin_agent = (origin_agent or "").strip() or None
    now = _now_iso()
    c = state_conn(state)
    with write_txn(c) as cw:
        kb_id = _next_kb_id(cw)
        gate1 = {"passed": True, "checks": checks, "ts": now}
        cw.execute(
            "INSERT INTO kb_entries (id, type, name, description, computable_spec,"
            " severity, env_scope, status, invalid_reason, sealed_reason, source,"
            " created_by, origin_agent, review_gate1_ref, review_gate2_ref,"
            " deleted_ts, created_ts, updated_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (kb_id, type_, name, description, json.dumps(spec, ensure_ascii=False),
             severity, env_scope, "observing", "", "", source, created_by,
             origin_agent, json.dumps(gate1, ensure_ascii=False), "{}",
             "", now, now),
        )
        _audit(cw, ts=now, actor=created_by, action="kb.create",
               object_id=kb_id, result="observing",
               detail=f"{TYPE_LABEL[type_]}·{name}（闸1 通过，等待启动验证）")
    row = c.execute("SELECT * FROM kb_entries WHERE id=?", (kb_id,)).fetchone()
    return _row_to_dict(row)


def get_entry(state, kb_id: str, *, include_deleted: bool = True) -> dict | None:
    c = state_conn(state)
    sql = "SELECT * FROM kb_entries WHERE id=?"
    if not include_deleted:
        sql += " AND deleted_ts=''"
    row = c.execute(sql, (kb_id,)).fetchone()
    if row is None:
        return None
    d = _row_to_dict(row)
    d["stats"] = [stats_row_to_dict(s) for s in c.execute(
        "SELECT * FROM kb_stats WHERE kb_id=? ORDER BY env_bucket", (kb_id,),
    ).fetchall()]
    return d


def stats_row_to_dict(s) -> dict:
    return {
        "id": s["id"], "kb_id": s["kb_id"], "env_bucket": s["env_bucket"],
        "sample_n": s["sample_n"], "win_rate": s["win_rate"],
        "avg_win": s["avg_win"], "avg_loss": s["avg_loss"],
        "expectancy": s["expectancy"], "intercept_n": s["intercept_n"],
        "exception_n": s["exception_n"], "stale_n": s["stale_n"],
        "dispatch_n": s["dispatch_n"], "window_days": s["window_days"],
        "note": s["note"], "updated_ts": s["updated_ts"],
    }


def list_entries(state, *, type_: str | None = None, status: str | None = None,
                 source: str | None = None, kw: str = "",
                 include_deleted: bool = False, limit: int = 200) -> list[dict]:
    c = state_conn(state)
    sql = "SELECT * FROM kb_entries WHERE 1=1"
    params: list = []
    if type_:
        if type_ not in KB_TYPES:
            raise ValueError(f"未知条目类型：{type_}")
        sql += " AND type=?"
        params.append(type_)
    if status:
        if status not in KB_STATUS:
            raise ValueError(f"未知状态：{status}")
        sql += " AND status=?"
        params.append(status)
    if source:
        if source not in KB_SOURCES:
            raise ValueError(f"未知来源：{source}")
        sql += " AND source=?"
        params.append(source)
    if not include_deleted:
        sql += " AND deleted_ts=''"
    if kw:
        sql += " AND (name LIKE ? OR description LIKE ? OR id LIKE ?)"
        like = f"%{kw}%"
        params += [like, like, like]
    sql += " ORDER BY updated_ts DESC, id DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))
    rows = c.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        out.append(d)
    return out


def update_entry(state, kb_id: str, *, actor: str = "user", deleted: bool | None = None,
                 restore: bool = False, note: str = "") -> dict | None:
    """元信息变更与软删/恢复（§3.7：删除仅管理 Agent 软删+审计；恢复=复核重新激活）。"""
    c = state_conn(state)
    row = c.execute("SELECT * FROM kb_entries WHERE id=?", (kb_id,)).fetchone()
    if row is None:
        return None
    now = _now_iso()
    d = _row_to_dict(row)
    if deleted is True and not d["deleted_ts"]:
        with write_txn(c) as cw:
            cw.execute("UPDATE kb_entries SET deleted_ts=?, updated_ts=? WHERE id=?",
                       (now, now, kb_id))
            _audit(cw, ts=now, actor=actor, action="kb.delete", object_id=kb_id,
                   result="deleted", detail=note or "软删（退出下发与 UCB 排序）")
        return get_entry(state, kb_id)
    if restore:
        with write_txn(c) as cw:
            prev = d["status"]
            target = "observing" if prev in ("invalid", "sealed") else prev
            cw.execute(
                "UPDATE kb_entries SET deleted_ts='', updated_ts=? WHERE id=?",
                (now, kb_id))
            _audit(cw, ts=now, actor=actor, action="kb.restore", object_id=kb_id,
                   result=target, detail=note or "管理复核重新激活")
        return get_entry(state, kb_id)
    return d


def transition_kb(state, kb_id: str, *, action: str, note: str = "",
                  actor: str = "user") -> dict:
    """状态机迁移（§3.2）：见 TRANSITIONS。note 落对应理由字段与审计留痕。

    start_validation：闸2（管理评审复核）留痕 → observing/invalid/sealed → validating
    approve_valid：    statistics 驱动结论 + 管理确认 → validating → valid
    invalidate：       observing/validating/valid → invalid（须填失效原因）
    seal：             validating → sealed（证据不足封存，须填原因）
    """
    if action not in ACTION_NOTES:
        raise ValueError(f"未知动作：{action}（{'/'.join(ACTION_NOTES)}）")
    if action == "invalidate":
        reason_field = "invalid_reason"
    elif action == "seal":
        reason_field = "sealed_reason"
    else:
        reason_field = "review_gate2_ref"
    note = (note or "").strip()
    if action in ("invalidate", "seal") and not note:
        raise ValueError("失效/封存须填写原因")
    if action == "start_validation" and not note:
        raise ValueError("启动验证须填写管理评审意见（闸2）")
    if action == "approve_valid" and not note:
        raise ValueError("确认有效须填写结论依据（统计摘要或评审意见）")

    c = state_conn(state)
    row = c.execute("SELECT * FROM kb_entries WHERE id=?", (kb_id,)).fetchone()
    if row is None:
        raise LookupError(f"条目不存在：{kb_id}")
    d = _row_to_dict(row)
    if d["deleted_ts"]:
        raise ValueError("条目已软删，不可迁移（先恢复）")
    allowed_from = TRANSITIONS.get(action, set())
    if d["status"] not in allowed_from:
        raise ValueError(
            f"状态不允许 {d['status']} → {action}（允许来源：{'/'.join(sorted(allowed_from)) or '—'}）")
    now = _now_iso()
    target = {"start_validation": "validating", "approve_valid": "valid",
              "invalidate": "invalid", "seal": "sealed"}[action]

    def _notes(v: str) -> str:
        try:
            obj = json.loads(v or "{}")
            if not isinstance(obj, dict):
                obj = {}
        except ValueError:
            obj = {}
        obj = dict(obj)
        obj["note"] = note
        obj["by"] = actor
        obj["ts"] = now
        return json.dumps(obj, ensure_ascii=False)

    with write_txn(c) as cw:
        cw.execute(
            f"UPDATE kb_entries SET status=?, {reason_field}=?, updated_ts=? WHERE id=?",
            (target, note if reason_field.endswith("reason") else _notes(row[reason_field]),
             now, kb_id),
        )
        _audit(cw, ts=now, actor=actor, action=f"kb.transition.{action}",
               object_id=kb_id, result=target,
               detail=f"{STATUS_LABEL.get(d['status'], d['status'])} → {STATUS_LABEL.get(target, target)}"
                      f"（{note[:160]}）")
    fresh = c.execute("SELECT * FROM kb_entries WHERE id=?", (kb_id,)).fetchone()
    return _row_to_dict(fresh)


def upsert_stats(state, kb_id: str, *, env_bucket: str = "all",
                 sample_n: int | None = None, win_rate: float | None = None,
                 avg_win: float | None = None, avg_loss: float | None = None,
                 expectancy: float | None = None, intercept_n: int | None = None,
                 exception_n: int | None = None, stale_n: int | None = None,
                 dispatch_n: int | None = None, window_days: int | None = None,
                 note: str = "", actor: str = "user") -> dict:
    """统计快照写入（spec-05 §3.3 kb_stats，按条目×环境桶唯一）。

    本接口供引擎侧确定性重算与人工数据维护共用（单用户人工录入限 sample_n 等
    非负校验）；statistics-driven 晋升结论以此表为证据。"""
    c = state_conn(state)
    entry = c.execute("SELECT * FROM kb_entries WHERE id=?", (kb_id,)).fetchone()
    if entry is None:
        raise LookupError(f"条目不存在：{kb_id}")
    env_bucket = (env_bucket or "all").strip() or "all"
    now = _now_iso()
    provided = {
        "sample_n": sample_n, "win_rate": win_rate, "avg_win": avg_win,
        "avg_loss": avg_loss, "expectancy": expectancy, "intercept_n": intercept_n,
        "exception_n": exception_n, "stale_n": stale_n, "dispatch_n": dispatch_n,
        "window_days": window_days,
    }
    for k in ("sample_n", "intercept_n", "exception_n", "stale_n",
              "dispatch_n", "window_days"):
        v = provided[k]
        if v is not None and (isinstance(v, (int, float)) and v < 0):
            raise ValueError(f"{k} 不能为负")
    if win_rate is not None and not 0 <= float(win_rate) <= 1:
        raise ValueError("win_rate 须在 [0,1] 区间")

    with write_txn(c) as cw:
        row = cw.execute(
            "SELECT * FROM kb_stats WHERE kb_id=? AND env_bucket=?", (kb_id, env_bucket),
        ).fetchone()
        base = {
            "sample_n": 0, "win_rate": None, "avg_win": None, "avg_loss": None,
            "expectancy": None, "intercept_n": 0, "exception_n": 0,
            "stale_n": 0, "dispatch_n": 0, "window_days": None,
        }
        for k in base:
            if row is not None and row[k] is not None:
                base[k] = row[k]
        for k, v in provided.items():
            if v is not None:
                base[k] = v
        rid = row["id"] if row is not None else "ks" + secrets.token_hex(8)
        cw.execute(
            "INSERT OR REPLACE INTO kb_stats (id, kb_id, env_bucket, sample_n, win_rate,"
            " avg_win, avg_loss, expectancy, intercept_n, exception_n, stale_n,"
            " dispatch_n, window_days, note, updated_ts)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, kb_id, env_bucket, base["sample_n"], base["win_rate"],
             base["avg_win"], base["avg_loss"], base["expectancy"],
             base["intercept_n"], base["exception_n"], base["stale_n"],
             base["dispatch_n"], base["window_days"], note[:400], now),
        )
        _audit(cw, ts=now, actor=actor, action="kb.stats.upsert", object_id=kb_id,
               result=env_bucket, detail=note or "统计快照写入")
    fresh = c.execute(
        "SELECT * FROM kb_stats WHERE kb_id=? AND env_bucket=?", (kb_id, env_bucket),
    ).fetchone()
    return stats_row_to_dict(fresh)


def list_stats(state, kb_id: str | None = None) -> list[dict]:
    c = state_conn(state)
    if kb_id:
        rows = c.execute("SELECT * FROM kb_stats WHERE kb_id=? ORDER BY env_bucket",
                         (kb_id,)).fetchall()
    else:
        rows = c.execute("SELECT * FROM kb_stats ORDER BY updated_ts DESC LIMIT 500"
                         ).fetchall()
    return [stats_row_to_dict(s) for s in rows]


def timeline(state, kb_id: str) -> list[dict]:
    """条目状态机时间线（spec-06 §6.7）：审计事件按时间正序。

    数据源=audit_logs（kb.create / kb.update / kb.transition.* / kb.delete /
    kb.restore / kb.stats.upsert），detail 已含 旧态→新态 与原因说明（spec-05 §3.2）。
    """
    c = state_conn(state)
    row = c.execute("SELECT id FROM kb_entries WHERE id=?", (kb_id,)).fetchone()
    if row is None:
        raise LookupError(f"条目不存在：{kb_id}")
    rows = c.execute(
        "SELECT ts, actor, action, result, detail FROM audit_logs"
        " WHERE object_type='kb_entries' AND object_id=?"
        " ORDER BY ts ASC, id ASC",
        (kb_id,),
    ).fetchall()
    return [
        {
            "ts": r["ts"],
            "actor": r["actor"],
            "action": r["action"],
            "result": r["result"],
            "detail": r["detail"],
        }
        for r in rows
    ]

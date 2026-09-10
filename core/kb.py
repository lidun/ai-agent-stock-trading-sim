"""知识与经验库（spec-05 §3：条目模型/状态机/权限软删，管理 Agent 评审由用户执行）。

证据源=signal_registry 客观统计（spec-05 §3.2）——本模块承载条目生命周期与
统计快照读写；`recompute_stats` 在日结算后按 (concept_tag × env_bucket) 确定性
重算 kb_stats（零 token，调度器紧随结算回调），管理 Agent/用户在本模块内完成
评审确认（review_gate2_ref 留痕，decided_by=user，与审批域同一口径：LLM 未接入前
由登录用户代行管理 Agent 评审）。

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


def _aggregate(rows) -> dict:
    """按桶聚合前瞻收益：胜率/均盈/均亏（负值）/期望值（含费）。"""
    vals = [float(r["fwd_return_pct"]) for r in rows if r["fwd_return_pct"] is not None]
    n = len(vals)
    stale_n = sum(1 for r in rows if "stale_close" in (r["quality"] or ""))
    if n == 0:
        return {"sample_n": 0, "win_rate": None, "avg_win": None,
                "avg_loss": None, "expectancy": None, "stale_n": stale_n}
    wins = [v for v in vals if v > 0]
    losses = [v for v in vals if v < 0]
    win_rate = len(wins) / n
    avg_win = (sum(wins) / len(wins)) if wins else None
    avg_loss = (sum(losses) / len(losses)) if losses else None
    expectancy = win_rate * (avg_win or 0.0) + (len(losses) / n) * (avg_loss or 0.0)
    return {
        "sample_n": n, "win_rate": round(win_rate, 4),
        "avg_win": round(avg_win, 4) if avg_win is not None else None,
        "avg_loss": round(avg_loss, 4) if avg_loss is not None else None,
        "expectancy": round(expectancy, 4), "stale_n": stale_n,
    }


def recompute_stats(state, *, kb_id: str | None = None, n_days: int = 10,
                    window_days: int | None = None) -> dict:
    """日结算后确定性重算 kb_stats（spec-05 §3.3「统计重算触发」，零 token）。

    口径（§3.2/§3.3）：
    - 证据源=signal_registry 已结清信号（fwd_return_pct 非空），trial_flag=1 排除；
    - 按 (concept_tag × env_bucket) 单桶独立、不跨桶合并：positive 条目以
      concept_tag=条目名 匹配 candidate/buy/sell 信号；pitfall 条目以
      pitfall_id=条目 ID 匹配 pitfall_intercept 信号（#54 凭证），intercept_n /
      exception_n 单列；条目 env_scope 非 all 时仅统计该桶；
    - 胜率=正收益样本/样本数；均盈=正收益均值、均亏=负收益均值（负值）；期望值=
      胜率×均盈 + 败率×均亏（含费）；stale_close 样本计入并 stale_n 单列（#34）；
    - pitfall 的 fwd 统计语义为「被拦截标的的前瞻收益」（负=真避坑、正=误杀），
      晋升方向由上层状态机按类型解读。
    dispatch_n 不在重算范围（UCB 下发计数由参考卡下发侧维护）；window_days 透传。
    """
    c = state_conn(state)
    if kb_id:
        entries = c.execute("SELECT * FROM kb_entries WHERE id=?", (kb_id,)).fetchall()
        if not entries:
            raise LookupError(f"条目不存在：{kb_id}")
    else:
        entries = c.execute("SELECT * FROM kb_entries").fetchall()
    buckets = 0
    for e in entries:
        if e["type"] == "pitfall":
            rows = c.execute(
                "SELECT env_bucket, fwd_return_pct, quality, exception"
                " FROM signal_registry WHERE trial_flag=0"
                " AND sig_type='pitfall_intercept' AND pitfall_id=?", (e["id"],),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT env_bucket, fwd_return_pct, quality, exception"
                " FROM signal_registry WHERE trial_flag=0"
                " AND sig_type IN ('candidate','buy','sell') AND concept_tag=?",
                (e["name"],),
            ).fetchall()
        scope = (e["env_scope"] or "all").strip() or "all"
        grouped: dict[str, list] = {}
        for r in rows:
            b = (r["env_bucket"] or "all").strip() or "all"
            if scope != "all" and b != scope:
                continue
            grouped.setdefault(b, []).append(r)
        if scope != "all":
            grouped.setdefault(scope, [])
        elif not grouped:
            grouped["all"] = []
        for b, rs in grouped.items():
            agg = _aggregate(rs)
            extra = {}
            if e["type"] == "pitfall":
                extra = {
                    "intercept_n": sum(1 for r in rs if not r["exception"]),
                    "exception_n": sum(1 for r in rs if r["exception"]),
                }
            upsert_stats(state, e["id"], env_bucket=b,
                         sample_n=agg["sample_n"], win_rate=agg["win_rate"],
                         avg_win=agg["avg_win"], avg_loss=agg["avg_loss"],
                         expectancy=agg["expectancy"], stale_n=agg["stale_n"],
                         window_days=window_days, actor="engine",
                         note=f"日结算后确定性重算（N={n_days}，单桶不合并）",
                         **extra)
            buckets += 1
    return {"entries": len(entries), "buckets": buckets}


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

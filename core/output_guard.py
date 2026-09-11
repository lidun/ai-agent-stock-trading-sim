"""结构化输出校验-重写（spec-04 §3.5 / #45）。

机器消费的输出（条件单 JSON/审批申请/审批决定/能力注册元数据）必须过 schema 校验：
字段完整、类型合法、数量为正、证券代码存在。非法 → 带错误信息回喂模型重写 ≤2 次；
仍失败 → 跳过该产出并记"输出失败"（审计留痕，供日报复盘读取），不无限重试。

本模块提供与渲染层解耦的通用引擎：调用方给出 `kind` 与可选 `regenerate` 回调
（默认用适配层 LLM 构造重写提示），引擎负责解析/校验/重写循环/失败留痕。
"""
from __future__ import annotations

import json
import logging
from typing import Callable

from core.db import state_conn, write_txn

log = logging.getLogger(__name__)

DEFAULT_MAX_REWRITES = 2
_UNSET = object()


class OutputGuardError(ValueError):
    """结构化输出校验失败。"""


def _now_iso() -> str:
    from datetime import datetime, timezone  # noqa: PLC0415
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _coerce(raw) -> dict:
    """把模型输出（dict 或 JSON 文本，容忍 ```json 代码围栏）归一为 dict。"""
    if isinstance(raw, dict):
        return raw
    if raw is None:
        raise OutputGuardError("输出为空")
    if not isinstance(raw, str):
        raise OutputGuardError(f"输出类型非法：{type(raw).__name__}")
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise OutputGuardError(f"输出不是合法 JSON：{exc}") from exc
    if not isinstance(value, dict):
        raise OutputGuardError("输出须为 JSON 对象")
    return value


# ---------------- 各 kind 的 schema 校验器（复用既有确定性校验） ----------------

def _validate_condition_order(v: dict) -> dict:
    from core import orderstore  # noqa: PLC0415
    for f in ("symbol", "order_type", "direction", "qty", "price_type"):
        if f not in v:
            raise OutputGuardError(f"条件单缺少字段：{f}")
    norm = orderstore.validate_order_payload(
        symbol=str(v["symbol"]), order_type=str(v["order_type"]),
        direction=str(v["direction"]), qty=v["qty"], trigger=v.get("trigger"),
        price_type=str(v["price_type"]),
    )
    return {**v, **norm}


def _validate_approval_request(v: dict) -> dict:
    from core import approval  # noqa: PLC0415
    if v.get("type") not in approval.APPROVAL_TYPES:
        raise OutputGuardError(f"审批类型非法：{v.get('type')}")
    if not isinstance(v.get("payload"), dict):
        raise OutputGuardError("审批申请 payload 须为对象")
    if not str(v.get("reason") or "").strip():
        raise OutputGuardError("审批申请缺少理由（策略依据）")
    if not str(v.get("agent_id") or "").strip():
        raise OutputGuardError("审批申请缺少 agent_id")
    return dict(v)


def _validate_approval_decision(v: dict) -> dict:
    if v.get("decision") not in ("approved", "rejected"):
        raise OutputGuardError("审批决定须为 approved|rejected")
    if not str(v.get("approval_id") or "").strip():
        raise OutputGuardError("审批决定缺少 approval_id")
    return dict(v)


def _validate_capability_registration(v: dict) -> dict:
    from core import capability_center as cc  # noqa: PLC0415
    raw = v.get("metadata", _UNSET)
    for field in ("name", "capability_type", "version", "description",
                  "source_type", "source_ref"):
        if not str(v.get(field) or "").strip():
            raise OutputGuardError(f"能力注册缺少字段：{field}")
    if v["capability_type"] not in cc.CAP_TYPES:
        raise OutputGuardError(f"能力类型非法：{v['capability_type']}")
    if v["source_type"] not in cc.SOURCE_TYPES:
        raise OutputGuardError(f"来源类型非法：{v['source_type']}")
    if raw is not _UNSET and raw is not None and not isinstance(raw, dict):
        raise OutputGuardError("能力注册 metadata 须为对象")
    return dict(v)


_VALIDATORS: dict[str, Callable[[dict], dict]] = {
    "条件单": _validate_condition_order,
    "审批申请": _validate_approval_request,
    "审批决定": _validate_approval_decision,
    "能力注册": _validate_capability_registration,
}

KINDS = tuple(_VALIDATORS)


def register_validator(kind: str, fn: Callable[[dict], dict]) -> None:
    _VALIDATORS[kind] = fn


def record_output_failure(state, *, kind: str, errors: list[str], agent_id: str = "",
                          trade_date: str = "", actor: str = "agent") -> str:
    """"输出失败"留痕（审计，供日报复盘读取）；返回审计时间戳。"""
    ts = _now_iso()
    detail = json.dumps({"kind": kind, "errors": errors[:5]}, ensure_ascii=False)
    with write_txn(state_conn(state)) as c:
        c.execute(
            "INSERT INTO audit_logs (ts, actor, action, object_type, object_id,"
            " result, detail, ip) VALUES (?,?,?,?,?,?,?,?)",
            (ts, actor, "llm.output_failed", "output_guard", agent_id, "failed",
             detail[:400], ""),
        )
    return ts


def _repair_prompt(kind: str, previous, error: str) -> list[dict]:
    return [
        {"role": "system",
         "content": "你是结构化输出修复器。只输出修正后的单个 JSON 对象，"
                    "不要解释、不要 Markdown 围栏。"},
        {"role": "user",
         "content": f"目标类型：{kind}\n校验错误：{error}\n"
                    f"待修复输出：{json.dumps(previous, ensure_ascii=False)}"},
    ]


def make_llm_regenerator(state, kind: str, *, timeout_s: float = 60.0
                         ) -> Callable[[dict, str], dict]:
    """默认重写回调：把校验错误回喂适配层 LLM 重写（spec-04 §3.5）。"""
    def _regen(previous, error):  # noqa: ANN001
        from core import llm  # noqa: PLC0415
        out = llm.chat(state, _repair_prompt(kind, previous, error),
                       timeout_s=timeout_s)
        return (out.get("content") or "").strip()
    return _regen


def validate(kind: str, raw, *, regenerate: Callable[[dict, str], object] | None = None,
             max_rewrites: int = DEFAULT_MAX_REWRITES,
             on_failure: Callable[[object, list[str]], None] | None = None
             ) -> dict:
    """校验并（可选）重写结构化输出。

    返回 {ok, value, raw, attempts, errors}；attempts=已发生的校验次数（≥1）。
    regenerate(previous, last_error) -> 新的原始输出（str/dict）；None 则不重写。
    on_failure(value, errors) 在校验最终失败时调用（best-effort）。
    """
    checker = _VALIDATORS.get(kind)
    if checker is None:
        raise ValueError(f"未注册的输出类型：{kind}")
    errors: list[str] = []
    value: object = raw
    for attempt in range(max(0, int(max_rewrites)) + 1):
        try:
            value = _coerce(value)
            norm = checker(value)
            return {"ok": True, "value": norm, "raw": value,
                    "attempts": attempt + 1, "errors": errors}
        except Exception as exc:  # noqa: BLE001 - 校验/解析失败均进入重写
            errors.append(f"{type(exc).__name__}: {exc}")
            if attempt >= max_rewrites or regenerate is None:
                break
            try:
                value = regenerate(value, errors[-1])
            except Exception as exc2:  # noqa: BLE001 - 重写调用失败即终止
                errors.append(f"重写调用失败：{type(exc2).__name__}: {exc2}")
                break
    if on_failure is not None:
        try:
            on_failure(value, errors)
        except Exception:  # noqa: BLE001
            log.exception("输出失败回执处理异常")
    return {"ok": False, "value": None, "raw": value,
            "attempts": len(errors), "errors": errors}

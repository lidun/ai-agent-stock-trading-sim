"""结构化输出校验-重写路由（spec-04 §3.5）。

- GET  /api/output-guard/kinds             已注册输出类型
- POST /api/output-guard/validate          校验（可选 LLM 重写；失败留痕「输出失败」）

写请求经 CSRF 校验；失败留痕 action=llm.output_failed，供日报复盘读取。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from core import output_guard
from core.auth import require_session

router = APIRouter(prefix="/api", tags=["output-guard"])

SessionDep = Annotated[dict, Depends(require_session)]


@router.get("/output-guard/kinds")
def output_guard_kinds(request: Request, session: SessionDep):
    return {"kinds": list(output_guard.KINDS)}


@router.post("/output-guard/validate")
def output_guard_validate(request: Request, session: SessionDep,
                          payload: dict | None = Body(default=None)):
    actor = session["session"]["username"]
    body = payload or {}
    kind = str(body.get("kind") or "")
    if kind not in output_guard.KINDS:
        raise HTTPException(status_code=400, detail=f"未注册的输出类型：{kind}")
    use_llm = bool(body.get("use_llm", False))
    agent_id = str(body.get("agent_id") or "")
    trade_date = str(body.get("trade_date") or "")
    regen = (output_guard.make_llm_regenerator(request.app.state, kind)
             if use_llm else None)

    def _on_failure(value, errors):  # noqa: ANN001
        output_guard.record_output_failure(
            request.app.state, kind=kind, errors=errors, agent_id=agent_id,
            trade_date=trade_date, actor=actor)

    result = output_guard.validate(
        kind, body.get("raw"), regenerate=regen,
        max_rewrites=int(body.get("max_rewrites", output_guard.DEFAULT_MAX_REWRITES)),
        on_failure=_on_failure)
    return {k: v for k, v in result.items() if k != "raw"}

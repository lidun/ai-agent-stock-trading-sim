"""模型服务设置路由（spec-06 §6.13：API Key 掩码 + core 加密存储；spec-04 §8 适配层接入点）。

- GET    /api/settings/llm-provider        掩码配置视图（无密钥明文）
- PATCH  /api/settings/llm-provider        保存 provider（api_key 省略=保留原密钥）
- DELETE /api/settings/llm-provider        移除配置（清除密钥+端点+模型）
- POST   /api/settings/llm-provider/test   连通性自检（写 last_test 留痕）
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from core import llm
from core.auth import require_session

router = APIRouter(prefix="/api/settings", tags=["settings"])

SessionDep = Annotated[dict, Depends(require_session)]


@router.get("/llm-provider")
def llm_provider_get(request: Request, session: SessionDep):
    return llm.provider_get(request.app.state)


@router.patch("/llm-provider")
def llm_provider_patch(request: Request, session: SessionDep,
                       payload: dict | None = Body(default=None)):
    actor = session["session"]["username"]
    body = payload or {}
    try:
        cfg = llm.provider_upsert(
            request.app.state,
            actor=actor,
            preset=body.get("preset", ""),
            base_url=body.get("base_url", ""),
            model=body.get("model", ""),
            api_key=body.get("api_key"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "provider": cfg}


@router.delete("/llm-provider")
def llm_provider_delete(request: Request, session: SessionDep):
    actor = session["session"]["username"]
    return {"ok": True, "provider": llm.provider_clear(request.app.state, actor=actor)}


@router.post("/llm-provider/test")
def llm_provider_test(request: Request, session: SessionDep):
    actor = session["session"]["username"]
    return llm.provider_test(request.app.state, actor=actor)

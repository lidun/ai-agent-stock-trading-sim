"""spec-03 数据服务路由：观察集合 / 采集清单 / 采集状态 / 覆盖 / 消费接口调试。

- GET/PUT/DELETE /api/market/observation    观察集合（管理 Agent 配置，≤20，次日生效）
- GET  /api/market/watchlist                当日采集清单快照（collector_watchlist）
- GET  /api/market/watchlist/derive         清单计算预览（不落库；口径同采集器）
- PUT  /api/market/watchlist/refresh        立即按当前口径重建当日清单（幂等）
- GET  /api/market/collector/status         采集器运行状态（未启用时返回 enabled=false）
- GET  /api/market/coverage                 票级覆盖/就绪查询
- GET  /api/market/replay/{symbol}          回放供给接口调试（L0/L1/L2 选档结果）
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request

from core import collector, l0store, market_data
from core.auth import require_session

router = APIRouter(prefix="/api/market", tags=["market"])

SessionDep = Annotated[dict, Depends(require_session)]
_BJT = timezone(timedelta(hours=8))
_SYMBOL_OK = 6


def _today() -> str:
    return datetime.now(_BJT).replace(tzinfo=None).date().isoformat()


def _parse_date(value: str, default: str | None = None) -> str:
    value = value or default or _today()
    try:
        return datetime.fromisoformat(value).date().isoformat()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"非法日期: {value}") from exc


def _check_symbol(symbol: str) -> str:
    symbol = (symbol or "").strip()
    if len(symbol) == _SYMBOL_OK and symbol.isdigit():
        return symbol
    if len(symbol) == 8 and symbol[:2] in ("sh", "sz") and symbol[2:].isdigit():
        return symbol
    raise HTTPException(status_code=400, detail=f"非法证券代码: {symbol}")


@router.get("/observation")
def observation_get(request: Request, session: SessionDep):
    return {"rows": l0store.observation_list(request.app.state),
            "limit": l0store.DEFAULT_OBS_LIMIT}


@router.put("/observation")
def observation_put(request: Request, session: SessionDep,
                    payload: dict | None = Body(default=None)):
    body = payload or {}
    symbol = _check_symbol(body.get("symbol", ""))
    try:
        obs = l0store.observation_add(request.app.state, symbol,
                                      reason=body.get("reason", ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "observation": obs}


@router.delete("/observation/{symbol}")
def observation_delete(request: Request, session: SessionDep,
                       symbol: str = Path(...)):
    symbol = _check_symbol(symbol)
    removed = l0store.observation_remove(request.app.state, symbol)
    return {"ok": True, "removed": removed}


@router.get("/watchlist")
def watchlist_get(request: Request, session: SessionDep, trade_date: str = ""):
    d = _parse_date(trade_date)
    return {"trade_date": d, "rows": l0store.watchlist_for(request.app.state, d)}


@router.get("/watchlist/derive")
def watchlist_derive(request: Request, session: SessionDep, trade_date: str = ""):
    d = _parse_date(trade_date)
    rows = collector.derive_watchlist(request.app.state, d)
    return {"trade_date": d, "count": len(rows), "rows": rows}


@router.put("/watchlist/refresh")
def watchlist_refresh(request: Request, session: SessionDep,
                      trade_date: str = ""):
    d = _parse_date(trade_date)
    rows = collector.derive_watchlist(request.app.state, d)
    n = l0store.replace_watchlist(request.app.state, d, rows)
    return {"ok": True, "trade_date": d, "count": n}


@router.get("/collector/status")
def collector_status(request: Request, session: SessionDep):
    settings = request.app.state.settings
    c = getattr(request.app.state, "collector", None)
    if not settings.market_data_enabled or c is None:
        return {"enabled": False,
                "reason": "市场数据服务未启用（CORE_MARKET_DATA_ENABLED=1 开启）"}
    return {"enabled": True, **c.status()}


@router.get("/coverage")
def coverage_get(request: Request, session: SessionDep, symbol: str = "",
                 trade_date: str = ""):
    state = request.app.state
    d = _parse_date(trade_date)
    if symbol:
        s = _check_symbol(symbol)
        cov = l0store.get_coverage(state, s, d)
        return {"trade_date": d, "symbol": s, "coverage": cov,
                "ready": market_data.is_ready(state, s, d)}
    return {"trade_date": d, "coverage": l0store.coverage_for_day(state, d)}


@router.get("/replay/{symbol}")
def replay_get(request: Request, session: SessionDep,
               symbol: str = Path(...), trade_date: str = ""):
    state = request.app.state
    s = _check_symbol(symbol)
    d = _parse_date(trade_date)
    try:
        series = market_data.get_replay_series(state, s, d)
    except Exception as exc:  # noqa: BLE001 供给缺口以 409 报缺口原因（引擎侧视为 gap）
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"symbol": s, "trade_date": d,
            "level": series["level"], "samples": len(series["series"]),
            "close_candidates": series["close_candidates"],
            "official_close": str(series["official_close"]),
            "official_close_source": series["official_close_source"],
            "source": series["source"], "quality": series["quality"],
            "notes": series["notes"]}


@router.post("/pull-minute")
def pull_minute(request: Request, session: SessionDep,
                payload: dict | None = Body(default=None)):
    body = payload or {}
    s = _check_symbol(body.get("symbol", ""))
    d = _parse_date(body.get("trade_date", ""))
    try:
        pm = market_data.pull_minute(request.app.state, s, d)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if pm is None:
        raise HTTPException(status_code=409, detail="L1 分钟不可供给（缺档，请降 L2）")
    return {"ok": True, "symbol": s, "trade_date": d, "cached": pm["cached"],
            "minutes": pm["minutes"], "source": pm["source"]}

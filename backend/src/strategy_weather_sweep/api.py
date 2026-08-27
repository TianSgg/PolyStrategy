"""策略配置管理 + 执行记录查询 API。"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from framework.auth import AuthUser, get_current_user
from strategy_weather_sweep.dao import WeatherSweepConfigDAO, WeatherSweepEventDAO, WeatherSweepTradeDAO
from strategy_weather_sweep.type import CreateConfigRequest, UpdateConfigRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/strategy", tags=["strategy"])

_config_dao = WeatherSweepConfigDAO()
_event_dao = WeatherSweepEventDAO()
_trade_dao = WeatherSweepTradeDAO()


# ─── Configs CRUD ───


@router.get("/configs")
async def list_configs(current_user: AuthUser = Depends(get_current_user)):
    configs = _config_dao.list_all(owner_user_ids=current_user.visible_user_ids())
    return {"configs": configs}


@router.get("/configs/{config_id}")
async def get_config(config_id: int, current_user: AuthUser = Depends(get_current_user)):
    cfg = _config_dao.get_by_id(config_id)
    if not cfg or not current_user.can_view(cfg["owner_user_id"]):
        raise HTTPException(status_code=404, detail="Config not found")
    return {"config": cfg}


@router.post("/configs")
async def create_config(data: CreateConfigRequest, request: Request, current_user: AuthUser = Depends(get_current_user)):
    config_id = _config_dao.create({
        "owner_user_id": current_user.id,
        "account_id": data.account_id,
        "name": data.name,
        "enabled": int(data.enabled),
        "fixed_entry_shares": data.fixed_entry_shares,
        "entry_wait_ms": data.entry_wait_ms,
        "sweep_outcome_filter": data.sweep_outcome_filter,
        "signal_source_filter": data.signal_source_filter,
        "signal_threshold_filter": data.signal_threshold_filter,
        "stop_loss_ratio": data.stop_loss_ratio,
        "exit_wait_ms": data.exit_wait_ms,
        "tick_verify_retries": data.tick_verify_retries,
        "tick_verify_backoff_ms": data.tick_verify_backoff_ms,
    })
    await _reload(request)
    return {"config_id": config_id}


@router.put("/configs/{config_id}")
async def update_config(
    config_id: int,
    data: UpdateConfigRequest,
    request: Request,
    current_user: AuthUser = Depends(get_current_user),
):
    cfg = _config_dao.get_by_id(config_id)
    if not cfg or not current_user.can_view(cfg["owner_user_id"]):
        raise HTTPException(status_code=404, detail="Config not found")

    updates = data.model_dump(exclude_none=True)
    if "enabled" in updates:
        updates["enabled"] = int(updates["enabled"])
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    _config_dao.update(config_id, updates)
    await _reload(request)
    return {"status": "ok"}


@router.delete("/configs/{config_id}")
async def delete_config(config_id: int, request: Request, current_user: AuthUser = Depends(get_current_user)):
    cfg = _config_dao.get_by_id(config_id)
    if not cfg or not current_user.can_view(cfg["owner_user_id"]):
        raise HTTPException(status_code=404, detail="Config not found")
    _config_dao.soft_delete(config_id)
    await _reload(request)
    return {"status": "ok"}


# ─── Events (执行记录) ───


@router.get("/events")
async def list_events(
    config_id: Optional[int] = Query(default=None),
    search: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: AuthUser = Depends(get_current_user),
):
    summaries = _event_dao.list_event_summaries(
        owner_user_ids=current_user.visible_user_ids(),
        config_id=config_id,
        search=search,
        limit=limit,
        offset=offset,
    )
    return {"events": summaries}


@router.get("/events/{event_id}")
async def get_event_steps(event_id: str, current_user: AuthUser = Depends(get_current_user)):
    steps = _event_dao.list_by_event_id(event_id)
    if not steps:
        raise HTTPException(status_code=404, detail="Event not found")
    if not current_user.can_view(steps[0]["owner_user_id"]):
        raise HTTPException(status_code=404, detail="Event not found")
    return {"event_id": event_id, "steps": steps}


# ─── Trades (摘要) ───


@router.get("/trades")
async def list_trades(
    status: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
    proxy_wallet: Optional[str] = Query(default=None),
    limit: int = Query(default=30, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: AuthUser = Depends(get_current_user),
):
    owner_ids = current_user.visible_user_ids()
    trades = _trade_dao.list_trades(
        owner_user_ids=owner_ids,
        status=status,
        search=search,
        proxy_wallet=proxy_wallet,
        limit=limit,
        offset=offset,
    )
    total = _trade_dao.count_trades(
        owner_user_ids=owner_ids,
        status=status,
        search=search,
        proxy_wallet=proxy_wallet,
    )
    return {"trades": trades, "total": total}


@router.get("/trades/{event_id}")
async def get_trade(event_id: str, current_user: AuthUser = Depends(get_current_user)):
    trade = _trade_dao.get_by_event_id(event_id)
    if not trade or not current_user.can_view(trade["owner_user_id"]):
        raise HTTPException(status_code=404, detail="Trade not found")
    return {"trade": trade}


# ─── Internal helpers ───


async def _reload(request: Request) -> None:
    """Directly reload the instance pool (same process)."""
    pool = request.app.state.pool
    await pool.reload()

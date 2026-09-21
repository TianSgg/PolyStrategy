"""策略配置管理 + 执行记录查询 API。"""
from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from framework.auth import AuthUser, get_current_user
from strategy_follow_weather_sweeper.dao import (
    FollowWeatherSweeperConfigDAO,
    FollowWeatherSweeperEventDAO,
    FollowWeatherSweeperTradeDAO,
)
from strategy_follow_weather_sweeper.type import CreateConfigRequest, UpdateConfigRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/follow-weather", tags=["strategy"])

_config_dao = FollowWeatherSweeperConfigDAO()
_event_dao = FollowWeatherSweeperEventDAO()
_trade_dao = FollowWeatherSweeperTradeDAO()


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
    try:
        config_id = _config_dao.create({
            "owner_user_id": current_user.id,
            "account_id": data.account_id,
            "name": data.name,
            "enabled": int(data.enabled),
            "fixed_entry_shares": data.fixed_entry_shares,
            "entry_wait_ms": data.entry_wait_ms,
            "stop_loss_ratio": data.stop_loss_ratio,
            "exit_wait_ms": data.exit_wait_ms,
            "leader_wallets": data.leader_wallets,
        })
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
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
    trade = _trade_dao.get_by_event_id(event_id)
    if not trade or not current_user.can_view(trade["owner_user_id"]):
        raise HTTPException(status_code=404, detail="Event not found")
    return {"event_id": event_id, "trade": trade, "steps": steps}


# ─── Trades (摘要) ───


@router.get("/trades")
async def list_trades(
    phase: Optional[str] = Query(default=None),
    close_reason: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
    proxy_wallet: Optional[str] = Query(default=None),
    direction: Optional[str] = Query(default=None),
    since: Optional[str] = Query(default=None),
    limit: int = Query(default=30, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: AuthUser = Depends(get_current_user),
):
    owner_ids = current_user.visible_user_ids()
    trades = _trade_dao.list_trades(
        owner_user_ids=owner_ids,
        phase=phase,
        close_reason=close_reason,
        search=search,
        proxy_wallet=proxy_wallet,
        direction=direction,
        since=since,
        limit=limit,
        offset=offset,
    )
    total = _trade_dao.count_trades(
        owner_user_ids=owner_ids,
        phase=phase,
        close_reason=close_reason,
        search=search,
        proxy_wallet=proxy_wallet,
        direction=direction,
        since=since,
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
    pool = request.app.state.pool
    await pool.reload()
    predexon = getattr(request.app.state, "predexon", None)
    if predexon:
        current_leaders: set[str] = set()
        for strategy in pool.all_instances():
            current_leaders.update(strategy._leader_wallets)
        predexon.sync_leaders(current_leaders)

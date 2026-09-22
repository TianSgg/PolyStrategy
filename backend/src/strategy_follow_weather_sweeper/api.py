"""策略配置管理 + 执行记录查询 API。"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

import requests as http_requests

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from framework.auth import AuthUser, get_current_user
from framework.slug_script import SlugProgram, SlugScriptError, validate_slug_script
from strategy_follow_weather_sweeper.dao import (
    FollowWeatherSweeperConfigDAO,
    FollowWeatherSweeperEventDAO,
    FollowWeatherSweeperSignalDAO,
    FollowWeatherSweeperTradeDAO,
)
from strategy_follow_weather_sweeper.type import CreateConfigRequest, UpdateConfigRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/follow-weather", tags=["strategy"])

_config_dao = FollowWeatherSweeperConfigDAO()
_event_dao = FollowWeatherSweeperEventDAO()
_trade_dao = FollowWeatherSweeperTradeDAO()
_signal_dao = FollowWeatherSweeperSignalDAO()


def _validate_params_slug_script(params_dict: dict) -> None:
    script = params_dict.get("slug_script")
    if script and script.strip():
        try:
            SlugProgram.compile(script)
        except SlugScriptError as exc:
            raise HTTPException(status_code=400, detail=exc.as_dict())


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
    params_dict = data.params.model_dump()
    _validate_params_slug_script(params_dict)
    try:
        config_id = _config_dao.create({
            "owner_user_id": current_user.id,
            "account_id": data.account_id,
            "name": data.name,
            "enabled": int(data.enabled),
            "params": params_dict,
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

    updates: dict = {}
    if data.name is not None:
        updates["name"] = data.name
    if data.enabled is not None:
        updates["enabled"] = int(data.enabled)
    if data.params is not None:
        params_dict = data.params.model_dump()
        _validate_params_slug_script(params_dict)
        updates["params"] = params_dict
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


# ─── SlugScript 校验 ───


class SlugScriptValidateRequest(BaseModel):
    slug_script: str


@router.post("/validate-slug-script")
async def validate_slug_script_endpoint(
    data: SlugScriptValidateRequest,
    current_user: AuthUser = Depends(get_current_user),
):
    return validate_slug_script(data.slug_script)


class SlugScriptTestRequest(BaseModel):
    slug_script: str
    test_slug: str


@router.post("/test-slug-script")
async def test_slug_script_endpoint(
    data: SlugScriptTestRequest,
    current_user: AuthUser = Depends(get_current_user),
):
    if not data.slug_script.strip():
        return {"result": True, "message": "脚本为空，默认通过"}
    try:
        program = SlugProgram.compile(data.slug_script)
    except SlugScriptError as exc:
        return {"error": True, "message": f"语法错误: {exc.message}"}
    try:
        result = program.evaluate(data.test_slug)
    except Exception as exc:
        return {"error": True, "message": f"执行错误: {exc}"}
    return {"result": bool(result)}


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


# ─── Signals (leader 信号记录) ───


@router.get("/signals")
async def list_signals(
    leader_wallet: Optional[str] = Query(default=None),
    market_slug: Optional[str] = Query(default=None),
    side: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: AuthUser = Depends(get_current_user),
):
    signals = _signal_dao.list_signals(
        leader_wallet=leader_wallet,
        market_slug=market_slug,
        side=side,
        limit=limit,
        offset=offset,
    )
    return {"signals": signals}


# ─── Profile (leader 用户名查询) ───

GAMMA_API_URL = "https://gamma-api.polymarket.com"
_profile_cache: dict[str, tuple[dict, float]] = {}
_PROFILE_CACHE_TTL = 3600


def _fetch_profile(address: str) -> dict:
    addr = address.lower().strip()
    cached = _profile_cache.get(addr)
    if cached and time.time() - cached[1] < _PROFILE_CACHE_TTL:
        return cached[0]
    try:
        resp = http_requests.get(
            f"{GAMMA_API_URL}/public-profile",
            params={"address": addr},
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            name = (data.get("name") or "").strip()
            pseudonym = (data.get("pseudonym") or "").strip()
            display_name = name if name and not name.lower().startswith("0x") else pseudonym or name or ""
            profile = {
                "name": display_name,
                "proxy_wallet": (data.get("proxyWallet") or "").lower().strip(),
            }
            _profile_cache[addr] = (profile, time.time())
            return profile
    except Exception:
        logger.debug("Failed to fetch profile for %s", addr[:10])
    return {"name": "", "proxy_wallet": ""}


@router.get("/profile")
async def get_profile(
    address: str = Query(...),
    current_user: AuthUser = Depends(get_current_user),
):
    import asyncio
    profile = await asyncio.to_thread(_fetch_profile, address)
    return {"address": address, "name": profile["name"]}


@router.get("/wallet-value")
async def get_wallet_value(
    address: str = Query(...),
    current_user: AuthUser = Depends(get_current_user),
):
    import asyncio
    from framework.balance import fetch_public_address_value
    profile = await asyncio.to_thread(_fetch_profile, address)
    proxy = profile.get("proxy_wallet") or address
    balance, position_value = await fetch_public_address_value(proxy)
    return {
        "address": address,
        "balance": round(balance, 2),
        "position_value": round(position_value, 2),
        "total_value": round(balance + position_value, 2),
    }


# ─── Internal helpers ───


async def _reload(request: Request) -> None:
    pool = request.app.state.pool
    await pool.reload()
    predexon = getattr(request.app.state, "predexon", None)
    if predexon:
        current_leaders: set[str] = set()
        for strategy in pool.all_instances():
            if strategy._leader_wallet:
                current_leaders.add(strategy._leader_wallet)
        predexon.sync_leaders(current_leaders)

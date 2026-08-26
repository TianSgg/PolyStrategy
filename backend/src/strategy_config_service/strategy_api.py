"""策略配置管理 + 执行记录查询 API。"""
from __future__ import annotations

import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from framework.auth import AuthUser, get_current_user
from strategy_config_service.strategy_dao import WeatherSweepConfigDAO, WeatherSweepEventDAO

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/strategy", tags=["strategy"])

_config_dao = WeatherSweepConfigDAO()
_event_dao = WeatherSweepEventDAO()

STRATEGY_SWEEP_URL = "http://127.0.0.1:8003"


# ─── Request / Response Models ───


class CreateConfigRequest(BaseModel):
    account_id: int
    name: str
    enabled: bool = False
    fixed_entry_shares: float = 100.0
    entry_wait_ms: int = 30000
    sweep_outcome_filter: str = "no"
    stop_loss_ratio: float = 0.60
    exit_wait_ms: int = 5000
    tick_verify_retries: int = 3
    tick_verify_backoff_ms: int = 1000


class UpdateConfigRequest(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    fixed_entry_shares: Optional[float] = None
    entry_wait_ms: Optional[int] = None
    sweep_outcome_filter: Optional[str] = None
    stop_loss_ratio: Optional[float] = None
    exit_wait_ms: Optional[int] = None
    tick_verify_retries: Optional[int] = None
    tick_verify_backoff_ms: Optional[int] = None


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
async def create_config(data: CreateConfigRequest, current_user: AuthUser = Depends(get_current_user)):
    config_id = _config_dao.create({
        "owner_user_id": current_user.id,
        "account_id": data.account_id,
        "name": data.name,
        "enabled": int(data.enabled),
        "fixed_entry_shares": data.fixed_entry_shares,
        "entry_wait_ms": data.entry_wait_ms,
        "sweep_outcome_filter": data.sweep_outcome_filter,
        "stop_loss_ratio": data.stop_loss_ratio,
        "exit_wait_ms": data.exit_wait_ms,
        "tick_verify_retries": data.tick_verify_retries,
        "tick_verify_backoff_ms": data.tick_verify_backoff_ms,
    })
    await _notify_reload()
    return {"config_id": config_id}


@router.put("/configs/{config_id}")
async def update_config(
    config_id: int,
    data: UpdateConfigRequest,
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
    await _notify_reload()
    return {"status": "ok"}


@router.delete("/configs/{config_id}")
async def delete_config(config_id: int, current_user: AuthUser = Depends(get_current_user)):
    cfg = _config_dao.get_by_id(config_id)
    if not cfg or not current_user.can_view(cfg["owner_user_id"]):
        raise HTTPException(status_code=404, detail="Config not found")
    _config_dao.soft_delete(config_id)
    await _notify_reload()
    return {"status": "ok"}


# ─── Events (执行记录) ───


@router.get("/events")
async def list_events(
    config_id: Optional[int] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: AuthUser = Depends(get_current_user),
):
    summaries = _event_dao.list_event_summaries(
        owner_user_ids=current_user.visible_user_ids(),
        config_id=config_id,
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


# ─── Internal helpers ───


async def _notify_reload() -> None:
    """通知策略进程重新加载配置。"""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(f"{STRATEGY_SWEEP_URL}/internal/reload")
            if resp.status_code != 200:
                logger.warning("Reload notify failed: %d", resp.status_code)
    except Exception:
        logger.warning("Failed to notify strategy process for reload", exc_info=True)

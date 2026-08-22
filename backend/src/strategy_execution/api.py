"""策略执行 REST API — 配置 CRUD、运行查询、信号回放。"""
from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import AuthUser, get_current_user
from account.service import get_account_service
from strategy_execution.config import validate_strategy_params, STRATEGY_PARAMS_MAP
from strategy_execution.enums import StrategyType
from strategy_execution.repository import (
    StrategyConfigRepository,
    StrategyOrderRepository,
    StrategyRunEventRepository,
    StrategyRunRepository,
)
from signal_weather_orderbook.signal_repository import WeatherSignalRepository
from signal_leader_activity.signal_repository import LeaderSignalRepository

router = APIRouter(prefix="/api/strategy-execution", tags=["strategy-execution"])

_config_repo = StrategyConfigRepository()
_run_repo = StrategyRunRepository()
_order_repo = StrategyOrderRepository()
_event_repo = StrategyRunEventRepository()
_weather_signal_repo = WeatherSignalRepository()
_leader_signal_repo = LeaderSignalRepository()


# ─── 配置 CRUD ────────────────────────────────────────────────────────────────


@router.get("/configs")
async def list_configs(
    strategy_type: Optional[str] = Query(None),
    current_user: AuthUser = Depends(get_current_user),
):
    """列出当前用户可见的策略配置。"""
    results = []
    types_to_query = [StrategyType(strategy_type)] if strategy_type else list(StrategyType)
    visible_ids = current_user.visible_user_ids()

    for st in types_to_query:
        if visible_ids is None:
            configs = _config_repo.list_all_enabled(st)
            configs.extend(c for c in _config_repo.list_by_owner(st, 0, include_disabled=True)
                           if c not in configs)
        else:
            for uid in visible_ids:
                configs = _config_repo.list_by_owner(st, uid, include_disabled=True)
                results.extend(configs)
            continue
        results.extend(configs)
    return results


@router.post("/configs")
async def create_config(
    data: dict,
    current_user: AuthUser = Depends(get_current_user),
):
    """创建新的策略配置。"""
    strategy_type_str = data.get("strategy_type")
    if not strategy_type_str:
        raise HTTPException(status_code=400, detail="strategy_type is required")
    try:
        strategy_type = StrategyType(strategy_type_str)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid strategy_type: {strategy_type_str}")

    account_id = data.get("account_id")
    if not account_id:
        raise HTTPException(status_code=400, detail="account_id is required")

    account = get_account_service().get_account(int(account_id))
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    if not current_user.can_view(account.get("owner_user_id", 0)):
        raise HTTPException(status_code=403, detail="No permission on this account")

    name = data.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")

    params = data.get("params", {})
    try:
        validated = validate_strategy_params(strategy_type, params)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid params: {e}")

    config_data = {
        "owner_user_id": account.get("owner_user_id", current_user.id),
        "account_id": int(account_id),
        "name": name,
        "enabled": 0,
        **validated.model_dump(exclude={"strategy_type", "params_version"}),
        "params_version": validated.params_version if hasattr(validated, "params_version") else 1,
    }

    try:
        config_id = await asyncio.to_thread(_config_repo.create, strategy_type, config_data)
    except Exception as e:
        if "Duplicate" in str(e):
            raise HTTPException(status_code=409, detail="Config name already exists for this user")
        raise HTTPException(status_code=500, detail=str(e))

    return {"id": config_id, "strategy_type": strategy_type.value}


@router.get("/configs/{strategy_type}/{config_id}")
async def get_config(
    strategy_type: str,
    config_id: int,
    current_user: AuthUser = Depends(get_current_user),
):
    """获取指定配置详情。"""
    st = _parse_strategy_type(strategy_type)
    config = await asyncio.to_thread(_config_repo.find_by_id, st, config_id)
    if not config:
        raise HTTPException(status_code=404, detail="Config not found")
    if not current_user.can_view(config.get("owner_user_id", 0)):
        raise HTTPException(status_code=403, detail="Forbidden")
    return config


@router.put("/configs/{strategy_type}/{config_id}")
async def update_config(
    strategy_type: str,
    config_id: int,
    data: dict,
    current_user: AuthUser = Depends(get_current_user),
):
    """更新配置参数。"""
    st = _parse_strategy_type(strategy_type)
    config = await asyncio.to_thread(_config_repo.find_by_id, st, config_id)
    if not config:
        raise HTTPException(status_code=404, detail="Config not found")
    if not current_user.can_view(config.get("owner_user_id", 0)):
        raise HTTPException(status_code=403, detail="Forbidden")

    params = data.get("params")
    update_fields: dict = {}
    if params:
        validated = validate_strategy_params(st, params)
        update_fields.update(validated.model_dump(exclude={"strategy_type", "params_version"}))
        update_fields["params_version"] = validated.params_version

    if "enabled" in data:
        update_fields["enabled"] = 1 if data["enabled"] else 0
    if "name" in data:
        update_fields["name"] = data["name"].strip()

    if not update_fields:
        raise HTTPException(status_code=400, detail="Nothing to update")

    ok = await asyncio.to_thread(_config_repo.update, st, config_id, update_fields)
    if not ok:
        raise HTTPException(status_code=409, detail="Update failed")
    return {"status": "ok"}


@router.delete("/configs/{strategy_type}/{config_id}")
async def delete_config(
    strategy_type: str,
    config_id: int,
    current_user: AuthUser = Depends(get_current_user),
):
    """软删除配置。"""
    st = _parse_strategy_type(strategy_type)
    config = await asyncio.to_thread(_config_repo.find_by_id, st, config_id)
    if not config:
        raise HTTPException(status_code=404, detail="Config not found")
    if not current_user.can_view(config.get("owner_user_id", 0)):
        raise HTTPException(status_code=403, detail="Forbidden")

    ok = await asyncio.to_thread(_config_repo.soft_delete, st, config_id)
    if not ok:
        raise HTTPException(status_code=409, detail="Delete failed")
    return {"status": "ok"}


# ─── 运行查询 ─────────────────────────────────────────────────────────────────


@router.get("/runs")
async def list_runs(
    strategy_type: Optional[str] = Query(None),
    config_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: AuthUser = Depends(get_current_user),
):
    """查询策略运行列表。"""
    if strategy_type and config_id:
        runs = await asyncio.to_thread(
            _run_repo.list_by_config, strategy_type, config_id, status, limit, offset
        )
    elif status == "ACTIVE":
        runs = await asyncio.to_thread(_run_repo.list_active)
    else:
        raise HTTPException(status_code=400, detail="Provide strategy_type+config_id or status=ACTIVE")
    return runs


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    current_user: AuthUser = Depends(get_current_user),
):
    """获取运行详情（含订单和时间线）。"""
    run = await asyncio.to_thread(_run_repo.find_by_id, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    orders = await asyncio.to_thread(_order_repo.find_by_run, run_id)
    events = await asyncio.to_thread(_event_repo.list_by_run, run_id)

    return {
        "run": run,
        "orders": orders,
        "events": events,
    }


# ─── 信号查询 ─────────────────────────────────────────────────────────────────


@router.get("/signals")
async def list_signals(
    source_type: Optional[str] = Query(None),
    token_id: Optional[str] = Query(None),
    leader_proxy_wallet: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    current_user: AuthUser = Depends(get_current_user),
):
    """查询信号列表（按 source_type 路由到对应仓库）。"""
    if source_type == "leader":
        results = await asyncio.to_thread(
            _leader_signal_repo.query,
            token_id=token_id,
            leader_proxy_wallet=leader_proxy_wallet,
            limit=limit,
            offset=offset,
        )
    elif source_type == "weather":
        results = await asyncio.to_thread(
            _weather_signal_repo.query,
            token_id=token_id,
            limit=limit,
            offset=offset,
        )
    else:
        weather = await asyncio.to_thread(
            _weather_signal_repo.query, token_id=token_id, limit=limit // 2, offset=offset,
        )
        leader = await asyncio.to_thread(
            _leader_signal_repo.query, token_id=token_id, limit=limit // 2, offset=offset,
        )
        results = sorted(weather + leader, key=lambda r: r.get("received_at", ""), reverse=True)[:limit]
    return results


@router.get("/signals/{notification_key:path}")
async def get_signal(
    notification_key: str,
    current_user: AuthUser = Depends(get_current_user),
):
    """获取单个信号详情（通过 notification_key 查询）。"""
    result = await asyncio.to_thread(_weather_signal_repo.find_by_id, notification_key)
    if not result:
        result = await asyncio.to_thread(_leader_signal_repo.find_by_id, notification_key)
    if not result:
        raise HTTPException(status_code=404, detail="Signal not found")
    return result


# ─── 辅助 ─────────────────────────────────────────────────────────────────────


def _parse_strategy_type(raw: str) -> StrategyType:
    try:
        return StrategyType(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid strategy_type: {raw}")

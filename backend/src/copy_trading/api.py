"""跟单模块 FastAPI 路由"""
import asyncio
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, List

from croniter import croniter

from auth import AuthUser, get_current_user
from account.service import get_account_service
from .service import get_copy_trading_service
from .models import get_all_schedules, get_schedule_by_config_id, upsert_schedule, delete_schedule
from leader.service import get_leader_service

router = APIRouter(prefix="/api/copy-trading", tags=["copy-trading"])


class CreateConfigRequest(BaseModel):
    leader_proxy_wallet: str
    follower_proxy_wallet: str


class UpdateConfigRequest(BaseModel):
    enabled: Optional[bool] = None
    gtd_expiration_sec: Optional[int] = None


def _assert_config_access(config, current_user: AuthUser):
    if not config or not current_user.can_view(config.owner_user_id):
        raise HTTPException(status_code=404, detail="Config not found")


@router.post("/configs")
async def create_config(data: CreateConfigRequest, current_user: AuthUser = Depends(get_current_user)):
    """创建跟单配置"""
    if not data.leader_proxy_wallet or not data.follower_proxy_wallet:
        raise HTTPException(status_code=400, detail="Missing required fields")

    # 验证 follower_proxy_wallet 存在
    account = get_account_service().get_account_by_proxy_wallet(data.follower_proxy_wallet)
    if not account or not current_user.can_view(account.get("owner_user_id", 0)):
        raise HTTPException(status_code=400, detail=f"Account {data.follower_proxy_wallet} does not exist")

    service = get_copy_trading_service()
    try:
        config_id = await service.create_config(
            data.leader_proxy_wallet,
            data.follower_proxy_wallet,
            owner_user_id=current_user.id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"config_id": config_id}


@router.get("/configs")
async def list_configs(current_user: AuthUser = Depends(get_current_user)):
    """获取所有跟单配置"""
    service = get_copy_trading_service()
    leader_svc = get_leader_service()
    account_svc = get_account_service()
    all_configs = [
        c
        for configs in service._leader_addr_to_configs.values()
        for c in configs
        if current_user.can_view(c.owner_user_id)
    ]
    configs = [
        {
            "id": c.id,
            "leader_proxy_wallet": c.leader_proxy_wallet,
            "leader_name": leader_svc.get_leader_name(c.leader_proxy_wallet),
            "follower_proxy_wallet": c.follower_proxy_wallet,
            "follower_name": account_svc.get_acc_name(c.follower_proxy_wallet),
            "enabled": c.enabled,
            "owner_user_id": c.owner_user_id,
            "gtd_expiration_sec": c.gtd_expiration_sec,
        }
        for c in all_configs
    ]
    return {"configs": configs}


@router.get("/configs/{config_id}")
async def get_config(config_id: int, current_user: AuthUser = Depends(get_current_user)):
    """获取单个配置详情"""
    service = get_copy_trading_service()
    config = service.get_config_by_id(config_id)
    _assert_config_access(config, current_user)
    return {
        "id": config.id,
        "leader_proxy_wallet": config.leader_proxy_wallet,
        "follower_proxy_wallet": config.follower_proxy_wallet,
        "enabled": config.enabled,
        "owner_user_id": config.owner_user_id,
        "gtd_expiration_sec": config.gtd_expiration_sec,
    }


@router.put("/configs/{config_id}")
async def update_config(config_id: int, data: UpdateConfigRequest, current_user: AuthUser = Depends(get_current_user)):
    """更新配置"""
    service = get_copy_trading_service()
    _assert_config_access(service.get_config_by_id(config_id), current_user)
    kwargs = {}
    if data.enabled is not None:
        kwargs["enabled"] = data.enabled
    if data.gtd_expiration_sec is not None:
        if not (60 <= data.gtd_expiration_sec <= 86400):
            raise HTTPException(status_code=400, detail="gtd_expiration_sec must be between 60 and 86400")
        kwargs["gtd_expiration_sec"] = data.gtd_expiration_sec

    if not kwargs:
        raise HTTPException(status_code=400, detail="No fields to update")

    success = service.update_config(config_id, **kwargs)
    if not success:
        raise HTTPException(status_code=404, detail="Config not found")
    return {"status": "ok"}


@router.delete("/configs/{config_id}")
async def delete_config(config_id: int, current_user: AuthUser = Depends(get_current_user)):
    """删除配置"""
    service = get_copy_trading_service()
    _assert_config_access(service.get_config_by_id(config_id), current_user)
    success = service.delete_config(config_id)
    if not success:
        raise HTTPException(status_code=404, detail="Config not found")
    return {"status": "ok"}


@router.get("/configs/{config_id}/positions")
async def get_positions(config_id: int, current_user: AuthUser = Depends(get_current_user)):
    """获取跟单持仓（leader & follower ）"""
    service = get_copy_trading_service()
    config = service.get_config_by_id(config_id)
    _assert_config_access(config, current_user)
    positions = service.get_positions(config_id)
    return positions


@router.get("/configs/{config_id}/position-assets")
async def get_position_assets(config_id: int, since: Optional[str] = None, current_user: AuthUser = Depends(get_current_user)):
    """获取指定配置可用于仓位历史查询的 asset 列表。since 可按 last_seen_at 过滤。"""
    service = get_copy_trading_service()
    config = service.get_config_by_id(config_id)
    _assert_config_access(config, current_user)
    return {"assets": service.get_assets_belongs_to_cfg(config_id, since=since)}


@router.get("/configs/{config_id}/orders")
async def get_orders(config_id: int, limit: int = 100, current_user: AuthUser = Depends(get_current_user)):
    """获取跟单订单历史"""
    service = get_copy_trading_service()
    config = service.get_config_by_id(config_id)
    _assert_config_access(config, current_user)
    orders = service.get_order_history(config_id, limit)
    return {"orders": orders}


@router.get("/configs/{config_id}/trade-scatter")
async def get_trade_scatter(
    config_id: int,
    asset_id: str = Query(..., min_length=1),
    limit: int = Query(500, ge=1, le=2000),
    start: Optional[str] = None,
    end: Optional[str] = None,
    current_user: AuthUser = Depends(get_current_user),
):
    """获取某 asset 的交易散点图数据"""
    from .models import get_orders_by_config_and_asset
    service = get_copy_trading_service()
    config = service.get_config_by_id(config_id)
    _assert_config_access(config, current_user)
    trades = await asyncio.to_thread(get_orders_by_config_and_asset, config_id, asset_id, limit, start=start, end=end)
    return {"trades": trades}


@router.get("/configs/{config_id}/position-history")
async def get_config_position_history(
    config_id: int,
    asset_id: str = Query(..., min_length=1),
    start: Optional[str] = None,
    end: Optional[str] = None,
    normalized: bool = False,
    limit: int = Query(2000, ge=1, le=5000),
    current_user: AuthUser = Depends(get_current_user),
):
    """获取指定 config+asset 的 leader/follower 仓位历史曲线。"""
    service = get_copy_trading_service()
    config = service.get_config_by_id(config_id)
    _assert_config_access(config, current_user)
    points = service.get_position_history(
        config_id,
        asset_id,
        start=start,
        end=end,
        normalized=normalized,
        limit=limit,
    )
    return {"points": points}


@router.post("/configs/{config_id}/sync")
async def sync_config_positions(config_id: int, current_user: AuthUser = Depends(get_current_user)):
    """立即同步 leader 仓位、follower 仓位、follower pending"""
    service = get_copy_trading_service()
    config = service.get_config_by_id(config_id)
    _assert_config_access(config, current_user)

    follower_addr = config.follower_proxy_wallet.lower()

    results = await asyncio.gather(
        service._sync_follower_positions_from_poly(follower_addr),
        service._sync_pending_orders_from_poly(follower_addr),
        return_exceptions=True
    )

    follower_synced, pending_synced = results

    def fmt(v):
        return str(v) if isinstance(v, Exception) else v

    return {
        "follower_synced": fmt(follower_synced),
        "pending_synced": "ok" if not isinstance(pending_synced, Exception) else str(pending_synced),
    }


# ==================== 定时调度 ====================

class UpsertScheduleRequest(BaseModel):
    start_cron: Optional[str] = None
    stop_cron: Optional[str] = None
    enabled: bool = True


def _validate_cron(expr: Optional[str], field_name: str):
    if expr is None:
        return
    if not croniter.is_valid(expr):
        raise HTTPException(status_code=400, detail=f"{field_name} is not a valid cron expression")


@router.get("/schedules")
async def list_schedules(current_user: AuthUser = Depends(get_current_user)):
    """批量获取当前用户所有配置的定时调度"""
    service = get_copy_trading_service()
    all_schedules = get_all_schedules(enabled_only=False)
    result = {}
    for sched in all_schedules:
        config = service.get_config_by_id(sched["config_id"])
        if config and current_user.can_view(config.owner_user_id):
            result[sched["config_id"]] = sched
    return {"schedules": result}


@router.get("/configs/{config_id}/schedule")
async def get_schedule(config_id: int, current_user: AuthUser = Depends(get_current_user)):
    """获取配置的定时调度"""
    service = get_copy_trading_service()
    _assert_config_access(service.get_config_by_id(config_id), current_user)
    sched = get_schedule_by_config_id(config_id)
    if not sched:
        return {"schedule": None}
    return {"schedule": sched}


@router.put("/configs/{config_id}/schedule")
async def set_schedule(config_id: int, data: UpsertScheduleRequest, current_user: AuthUser = Depends(get_current_user)):
    """创建或更新定时调度"""
    service = get_copy_trading_service()
    _assert_config_access(service.get_config_by_id(config_id), current_user)
    if not data.start_cron and not data.stop_cron:
        raise HTTPException(status_code=400, detail="start_cron and stop_cron cannot both be empty")
    _validate_cron(data.start_cron, "start_cron")
    _validate_cron(data.stop_cron, "stop_cron")
    upsert_schedule(config_id, data.start_cron, data.stop_cron, data.enabled)
    return {"status": "ok"}


@router.delete("/configs/{config_id}/schedule")
async def remove_schedule(config_id: int, current_user: AuthUser = Depends(get_current_user)):
    """删除定时调度"""
    service = get_copy_trading_service()
    _assert_config_access(service.get_config_by_id(config_id), current_user)
    delete_schedule(config_id)
    return {"status": "ok"}



"""Leader 管理 FastAPI 路由"""
import asyncio
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from auth import AuthUser, get_current_user
from framework.balance import fetch_address_value
from .service import get_leader_service

router = APIRouter(prefix="/api/leaders", tags=["leaders"])


class CreateLeaderRequest(BaseModel):
    proxy_wallet: str


class UpdateLeaderRequest(BaseModel):
    name: str


@router.get("/balances")
async def get_leader_balances(leader_address: Optional[str] = None, current_user: AuthUser = Depends(get_current_user)):
    """
    获取 Leader 余额。
    - leader_address 有值时：返回该地址的余额
    - leader_address 为空时：返回所有 leader 地址列表
    """
    service = get_leader_service()

    if leader_address:
        leader = service.get_leader_by_address(leader_address.lower())
        if not leader or not current_user.can_view(leader.get("owner_user_id", 0)):
            raise HTTPException(status_code=404, detail="Leader not found")
        available_balance, position_value = await fetch_address_value(leader_address.lower())
        b = available_balance or 0.0
        pv = position_value or 0.0
        balance = {
            "position_value": round(pv, 2),
            "available_balance": round(b, 2),
            "total_balance": round(pv + b, 2),
        }
        return {"balances": {leader_address.lower(): balance}}

    # 无参数时返回地址列表，由前端自行轮询
    leaders = service.get_all_leaders(owner_user_ids=current_user.visible_user_ids())
    return {"addresses": [leader["proxy_wallet"] for leader in leaders]}


@router.get("")
async def list_leaders(current_user: AuthUser = Depends(get_current_user)):
    """获取所有 Leader"""
    service = get_leader_service()
    return {"leaders": service.get_all_leaders(owner_user_ids=current_user.visible_user_ids())}


@router.post("")
async def add_leader(data: CreateLeaderRequest, current_user: AuthUser = Depends(get_current_user)):
    """添加 Leader（名字和 profile 信息自动从 Polymarket API 查询）"""
    if not data.proxy_wallet:
        raise HTTPException(status_code=400, detail="Missing proxy_wallet")
    if not data.proxy_wallet.startswith("0x") or len(data.proxy_wallet) != 42:
        raise HTTPException(status_code=400, detail="Invalid wallet address")

    service = get_leader_service()
    existing = service.get_leader_by_address(data.proxy_wallet, current_user.id)
    if existing:
        raise HTTPException(status_code=409, detail="Leader already exists")

    try:
        leader_id, name = await asyncio.to_thread(service.add_leader, data.proxy_wallet, owner_user_id=current_user.id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"leader_id": leader_id, "name": name}


@router.put("/{leader_id}")
async def update_leader(leader_id: int, data: UpdateLeaderRequest, current_user: AuthUser = Depends(get_current_user)):
    """更新 Leader"""
    service = get_leader_service()
    leader = service.get_leader_by_id(leader_id)
    if not leader or (not current_user.can_view(leader.get("owner_user_id", 0))):
        raise HTTPException(status_code=404, detail="Leader not found")
    success = service.update_leader(leader_id, data.name)
    return {"status": "ok"}


@router.delete("/{leader_id}")
async def delete_leader(leader_id: int, current_user: AuthUser = Depends(get_current_user)):
    """删除 Leader"""
    service = get_leader_service()
    leader = service.get_leader_by_id(leader_id)
    if not leader or (not current_user.can_view(leader.get("owner_user_id", 0))):
        raise HTTPException(status_code=404, detail="Leader not found")
    success = service.delete_leader(leader_id)
    return {"status": "ok"}


@router.post("/{leader_id}/refresh")
async def refresh_leader_profile(leader_id: int, current_user: AuthUser = Depends(get_current_user)):
    """从 Polymarket 刷新 Leader profile 信息"""
    service = get_leader_service()
    leader = service.get_leader_by_id(leader_id)
    if not leader or (not current_user.can_view(leader.get("owner_user_id", 0))):
        raise HTTPException(status_code=404, detail="Leader not found")

    try:
        success = await asyncio.to_thread(service.refresh_leader_profile, leader_id, leader["proxy_wallet"])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not success:
        raise HTTPException(status_code=404, detail="Leader not found")
    return {"status": "ok"}

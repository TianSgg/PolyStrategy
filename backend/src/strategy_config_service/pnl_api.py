"""PnL 模块路由：REST 历史查询 + 余额调整 CRUD"""
import asyncio
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from framework.auth import AuthUser, get_current_user
from account_service.service import get_account_service
from framework.time_utils import now_utc8_dt
from strategy_config_service.pnl_dao import get_balance_history, get_all_adjustments, create_adjustment, update_adjustment, delete_adjustment, delete_balance_record
from strategy_config_service.pnl_service import get_pnl_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/pnl", tags=["pnl"])

RANGE_DELTAS = {
    "1D": timedelta(days=1),
    "1W": timedelta(weeks=1),
    "1M": timedelta(days=30),
    "1Y": timedelta(days=365),
}


@router.get("/history")
async def get_history(
    range: str = Query("1D", regex="^(1D|1W|1M|1Y)$"),
    current_user: AuthUser = Depends(get_current_user),
):
    """获取指定时间范围的余额历史（按角色过滤可见账户）"""
    delta = RANGE_DELTAS[range]
    since = now_utc8_dt() - delta
    visible_ids = current_user.visible_user_ids()
    accounts = get_account_service().get_all_accounts(owner_user_ids=visible_ids)
    wallets = [a["proxy_wallet"] for a in accounts]
    records = await asyncio.to_thread(get_balance_history, since, wallets) if wallets else []
    return {"records": records}


# --- 删除数据点 ---

class BalanceDeleteRequest(BaseModel):
    proxy_wallet: str
    created_at: str


@router.post("/history/delete")
async def delete_history_point(data: BalanceDeleteRequest, current_user: AuthUser = Depends(get_current_user)):
    """删除指定余额数据点"""
    try:
        created_at = datetime.fromisoformat(data.created_at)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid created_at format")
    deleted = await asyncio.to_thread(delete_balance_record, data.proxy_wallet, created_at)
    if not deleted:
        raise HTTPException(status_code=404, detail="Record not found")
    return {"status": "ok", "deleted": deleted}


# --- 余额调整 CRUD ---

class AdjustmentCreate(BaseModel):
    proxy_wallet: str
    delta: float
    applied_at: str
    note: str = ""


class AdjustmentUpdate(BaseModel):
    delta: float
    note: str = ""


@router.get("/adjustments")
async def list_adjustments(current_user: AuthUser = Depends(get_current_user)):
    """获取余额调整记录（按角色过滤可见账户）"""
    records = await asyncio.to_thread(get_all_adjustments)
    visible_ids = current_user.visible_user_ids()
    if visible_ids is not None:
        accounts = get_account_service().get_all_accounts(owner_user_ids=visible_ids)
        visible_wallets = {a["proxy_wallet"] for a in accounts}
        records = [r for r in records if r["proxy_wallet"] in visible_wallets]
    return {"adjustments": records}


@router.post("/adjustments")
async def add_adjustment(data: AdjustmentCreate, current_user: AuthUser = Depends(get_current_user)):
    """创建余额调整"""
    try:
        applied_at = datetime.fromisoformat(data.applied_at)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid applied_at format")
    adj_id = await asyncio.to_thread(create_adjustment, data.proxy_wallet, data.delta, applied_at, data.note)
    return {"id": adj_id}


@router.put("/adjustments/{adj_id}")
async def edit_adjustment(adj_id: int, data: AdjustmentUpdate, current_user: AuthUser = Depends(get_current_user)):
    """更新余额调整"""
    success = await asyncio.to_thread(update_adjustment, adj_id, data.delta, data.note)
    if not success:
        raise HTTPException(status_code=404, detail="Adjustment not found")
    return {"status": "ok"}


@router.delete("/adjustments/{adj_id}")
async def remove_adjustment(adj_id: int, current_user: AuthUser = Depends(get_current_user)):
    """删除余额调整"""
    success = await asyncio.to_thread(delete_adjustment, adj_id)
    if not success:
        raise HTTPException(status_code=404, detail="Adjustment not found")
    return {"status": "ok"}

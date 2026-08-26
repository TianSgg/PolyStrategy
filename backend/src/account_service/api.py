"""Account 管理 FastAPI 路由"""
import asyncio
from fastapi import APIRouter, Depends, HTTPException

from fastapi import Query

from framework.auth import AuthUser, get_current_user
from framework.trading import get_account_service, get_market_service
from framework.balance import fetch_address_value

router = APIRouter(prefix="/api/account", tags=["accounts"])


@router.post("/add")
async def add_account(data: dict, current_user: AuthUser = Depends(get_current_user)):
    """添加账户（签名类型自动检测，名字从 Polymarket API 自动查询）"""
    private_key = data.get("private_key", "")
    if not private_key:
        raise HTTPException(status_code=400, detail="private_key is required")
    builder_code = data.get("builder_code") or None
    signature_type = int(data["signature_type"]) if "signature_type" in data else None
    try:
        return await asyncio.to_thread(get_account_service().add_account, private_key, owner_user_id=current_user.id, signature_type=signature_type, builder_code=builder_code)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/list")
async def list_accounts(current_user: AuthUser = Depends(get_current_user)):
    """获取账户列表"""
    visible_ids = current_user.visible_user_ids()
    return get_account_service().get_all_accounts(owner_user_ids=visible_ids)


@router.put("/{account_id}/builder-code")
async def update_builder_code(account_id: int, data: dict, current_user: AuthUser = Depends(get_current_user)):
    """为已有账户设置 builder_code"""
    builder_code = data.get("builder_code", "").strip()
    if not builder_code:
        raise HTTPException(status_code=400, detail="builder_code is required")
    account = get_account_service().get_account(account_id)
    if not account or (not current_user.can_view(account.get("owner_user_id", 0))):
        raise HTTPException(status_code=404, detail="Account not found")
    ok = get_account_service().update_builder_code(account_id, builder_code)
    if not ok:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"status": "ok"}


@router.put("/{account_id}/relayer-api-key")
async def update_relayer_api_key(account_id: int, data: dict, current_user: AuthUser = Depends(get_current_user)):
    """为已有账户设置 relayer_api_key"""
    relayer_api_key = data.get("relayer_api_key", "").strip()
    if not relayer_api_key:
        raise HTTPException(status_code=400, detail="relayer_api_key is required")
    account = get_account_service().get_account(account_id)
    if not account or (not current_user.can_view(account.get("owner_user_id", 0))):
        raise HTTPException(status_code=404, detail="Account not found")
    ok = get_account_service().update_relayer_api_key(account_id, relayer_api_key)
    if not ok:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"status": "ok"}


@router.delete("/{proxy_wallet}")
async def delete_account(proxy_wallet: str, current_user: AuthUser = Depends(get_current_user)):
    """删除账户"""
    account = get_account_service().get_account_by_proxy_wallet(proxy_wallet)
    if not account or (not current_user.can_view(account.get("owner_user_id", 0))):
        raise HTTPException(status_code=404, detail="Account not found")
    get_account_service().delete_account(proxy_wallet)
    return {"status": "ok"}


@router.get("/{proxy_wallet}/balance")
async def get_account_balance(proxy_wallet: str, current_user: AuthUser = Depends(get_current_user)):
    """获取账户余额+持仓"""
    account = get_account_service().get_account_by_proxy_wallet(proxy_wallet)
    if not account or (not current_user.can_view(account.get("owner_user_id", 0))):
        raise HTTPException(status_code=404, detail="Account not found")
    balance, position_value = await fetch_address_value(proxy_wallet)
    b = balance or 0.0
    pv = position_value or 0.0
    return {
        "balance": b,
        "total_position_value": pv,
        "total_value": b + pv,
    }


@router.get("/market/price")
async def get_price(asset_id: str = Query(...)):
    """获取资产当前价格"""
    price = await get_market_service().get_price(asset_id)
    return {"price": price}



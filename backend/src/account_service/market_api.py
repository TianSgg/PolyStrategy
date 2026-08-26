"""Market API 路由"""
from fastapi import APIRouter, Query

from framework.trading import get_market_service

router = APIRouter(prefix="/api/market", tags=["market"])


@router.get("/price")
async def get_price(asset_id: str = Query(...)):
    """获取资产当前价格（活跃市场返回 midprice，已结算市场返回 outcome price）"""
    price = await get_market_service().get_price(asset_id)
    return {"price": price}

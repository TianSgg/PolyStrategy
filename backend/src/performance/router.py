from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import AuthUser, get_current_user, require_admin
from .service import get_performance_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/performance", tags=["performance"])


@router.post("/latency")
async def check_performance(_: AuthUser = Depends(get_current_user)):
    """手动触发全量延迟检测"""
    service = get_performance_service()
    await service.check_all()
    return {"success": True, "data": service.get_all_latency()}


@router.get("/cache/summary")
async def get_cache_summary(_: AuthUser = Depends(require_admin)):
    """获取运行时内存缓存概览。"""
    return {"success": True, "data": get_performance_service().get_cache_summary()}


@router.get("/cache/{service_name}/{cache_name}")
async def get_cache_detail(
    service_name: str,
    cache_name: str,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    asset_id: Optional[str] = None,
    _: AuthUser = Depends(require_admin),
):
    """获取指定缓存的分页详情。"""
    try:
        data = get_performance_service().get_cache_detail(
            service_name,
            cache_name,
            limit=limit,
            offset=offset,
            asset_id=asset_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Cache not found")
    return {"success": True, "data": data}

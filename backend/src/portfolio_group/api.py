"""Portfolio Group API 路由"""
from fastapi import APIRouter, Depends, HTTPException

from auth import AuthUser, get_current_user
from portfolio_group.dao import PortfolioGroupDao
from shared.db import get_db_connection

router = APIRouter(prefix="/api/portfolio-group", tags=["portfolio-groups"])


@router.get("/list")
async def list_groups(current_user: AuthUser = Depends(get_current_user)):
    """获取当前用户的所有分组（含 account_ids）"""
    return PortfolioGroupDao.get_all(current_user.id)


@router.post("/create")
async def create_group(data: dict, current_user: AuthUser = Depends(get_current_user)):
    """创建分组"""
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    account_ids = data.get("account_ids", [])
    group_id = PortfolioGroupDao.insert(name, current_user.id)
    if account_ids:
        PortfolioGroupDao.set_account_ids(group_id, account_ids)
    return {"id": group_id, "name": name, "account_ids": account_ids}


@router.get("/pin")
async def get_pinned(current_user: AuthUser = Depends(get_current_user)):
    """获取当前用户置顶的分组 ID"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT pinned_portfolio_group_id FROM users WHERE id = %s", (current_user.id,))
        row = cursor.fetchone()
        return {"pinned_group_id": row[0] if row else None}
    finally:
        conn.close()


@router.put("/pin")
async def set_pinned(data: dict, current_user: AuthUser = Depends(get_current_user)):
    """设置/取消置顶分组"""
    group_id = data.get("group_id")
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE users SET pinned_portfolio_group_id = %s WHERE id = %s",
            (group_id, current_user.id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}


@router.put("/{group_id}")
async def update_group(group_id: int, data: dict, current_user: AuthUser = Depends(get_current_user)):
    """更新分组（名称和账户列表）"""
    group = PortfolioGroupDao.get_by_id(group_id)
    if not group or group["owner_user_id"] != current_user.id:
        raise HTTPException(status_code=404, detail="Group not found")
    name = (data.get("name") or "").strip()
    if name:
        PortfolioGroupDao.update(group_id, name)
    if "account_ids" in data:
        PortfolioGroupDao.set_account_ids(group_id, data["account_ids"])
    return {"status": "ok"}


@router.delete("/{group_id}")
async def delete_group(group_id: int, current_user: AuthUser = Depends(get_current_user)):
    """删除分组"""
    group = PortfolioGroupDao.get_by_id(group_id)
    if not group or group["owner_user_id"] != current_user.id:
        raise HTTPException(status_code=404, detail="Group not found")
    PortfolioGroupDao.delete(group_id)
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE users SET pinned_portfolio_group_id = NULL WHERE id = %s AND pinned_portfolio_group_id = %s",
            (current_user.id, group_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok"}

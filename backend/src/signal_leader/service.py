"""Leader 信息服务 — DB 持久化 + Polymarket API 查询"""
import asyncio
import logging
from typing import Dict, List, Optional

import requests

from framework.db import get_db_connection
from framework.time_utils import UTC8_DB_NOW_SQL, format_utc8, to_utc8_dt


def _format_leader_times(leader: dict) -> dict:
    for key in ("poly_created_at", "created_at", "updated_at"):
        leader[key] = format_utc8(leader.get(key))
    return leader

logger = logging.getLogger(__name__)

GAMMA_API_URL = "https://gamma-api.polymarket.com"

# Leaders table columns (static profile fields from Polymarket)
LEADER_COLUMNS = [
    "id", "proxy_wallet", "name", "profile_image", "bio", "pseudonym",
    "x_username", "verified_badge", "display_username_public", "poly_created_at",
    "owner_user_id", "created_at", "updated_at"
]


class LeaderService:
    """管理 leader 账户信息：DB 持久化 + Polymarket API 回退"""

    def __init__(self):
        self._cache: Dict[str, str] = {}  # address.lower() → name
        self._build_cache()

    def _build_cache(self):
        """从数据库加载所有 leader 的 proxy_wallet → name"""
        leaders = self.get_all_leaders()
        for leader in leaders:
            pw = leader.get("proxy_wallet", "")
            name = leader.get("name", "")
            if pw:
                self._cache[pw.lower()] = name

        logger.info(f"[Leader] loaded {len(leaders)} leader names")

    # ==================== DB CRUD ====================

    def get_all_leaders(self, owner_user_id: Optional[int] = None, owner_user_ids: Optional[List[int]] = None) -> List[dict]:
        """返回 Leader 列表。owner_user_ids 优先；都为 None 时返回全部"""
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            sql = f"SELECT {', '.join(LEADER_COLUMNS)} FROM leaders"
            params: tuple = ()
            if owner_user_ids is not None:
                placeholders = ",".join(["%s"] * len(owner_user_ids))
                sql += f" WHERE owner_user_id IN ({placeholders})"
                params = tuple(owner_user_ids)
            elif owner_user_id is not None:
                sql += " WHERE owner_user_id = %s"
                params = (owner_user_id,)
            sql += " ORDER BY id DESC"
            cursor.execute(sql, params)
            columns = [col[0] for col in cursor.description]
            return [_format_leader_times(dict(zip(columns, row))) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_leader_by_id(self, leader_id: int) -> Optional[dict]:
        """根据 ID 查单个 Leader"""
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"SELECT {', '.join(LEADER_COLUMNS)} FROM leaders WHERE id = %s",
                (leader_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            columns = [col[0] for col in cursor.description]
            return _format_leader_times(dict(zip(columns, row)))
        finally:
            conn.close()

    def _update_leader_profile(self, leader_id: int, profile: dict) -> bool:
        """按 leader_id 更新 profile，避免跨 owner upsert 出无归属记录。"""
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(f"""
                UPDATE leaders SET
                    name = %s,
                    profile_image = %s,
                    bio = %s,
                    pseudonym = %s,
                    x_username = %s,
                    verified_badge = %s,
                    display_username_public = %s,
                    updated_at = {UTC8_DB_NOW_SQL}
                WHERE id = %s
            """, (
                profile.get("name") or "",
                profile.get("profileImage") or "",
                profile.get("bio") or "",
                profile.get("pseudonym") or "",
                profile.get("xUsername") or "",
                bool(profile.get("verifiedBadge", False)),
                bool(profile.get("displayUsernamePublic", False)),
                leader_id,
            ))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def get_leader_by_address(self, proxy_wallet: str, owner_user_id: Optional[int] = None) -> Optional[dict]:
        """根据地址查 Leader"""
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            sql = f"SELECT {', '.join(LEADER_COLUMNS)} FROM leaders WHERE proxy_wallet = %s"
            params = [proxy_wallet.lower()]
            if owner_user_id is not None:
                sql += " AND owner_user_id = %s"
                params.append(owner_user_id)
            sql += " ORDER BY id ASC LIMIT 1"
            cursor.execute(sql, tuple(params))
            row = cursor.fetchone()
            if not row:
                return None
            columns = [col[0] for col in cursor.description]
            return _format_leader_times(dict(zip(columns, row)))
        finally:
            conn.close()

    def add_leader(self, proxy_wallet: str, owner_user_id: Optional[int] = None) -> tuple[int, str]:
        """添加 Leader（自动从 Polymarket API 查 profile），返回 (leader_id, name)"""
        profile = self._fetch_profile_from_poly(proxy_wallet)
        name = profile.get("name") or profile.get("pseudonym") or ""
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO leaders (
                    proxy_wallet, name, profile_image, bio, pseudonym,
                x_username, verified_badge, display_username_public, poly_created_at, owner_user_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                proxy_wallet.lower(),
                name,
                profile.get("profileImage") or "",
                profile.get("bio") or "",
                profile.get("pseudonym") or "",
                profile.get("xUsername") or "",
                bool(profile.get("verifiedBadge", False)),
                bool(profile.get("displayUsernamePublic", False)),
                to_utc8_dt(profile.get("createdAt")),
                owner_user_id,
            ))
            conn.commit()
            leader_id = cursor.lastrowid
            self._cache[proxy_wallet.lower()] = name
            logger.info(f"[Leader] Added leader: id={leader_id}, name='{name}', address={proxy_wallet[:8]}...")
            return leader_id, name
        finally:
            conn.close()

    def refresh_leader_profile(self, leader_id: int, proxy_wallet: str) -> bool:
        """从 Polymarket 刷新 leader profile 信息"""
        profile = self._fetch_profile_from_poly(proxy_wallet)
        if not self._update_leader_profile(leader_id, profile):
            return False
        name = profile.get("name") or profile.get("pseudonym") or ""
        self._cache[proxy_wallet.lower()] = name
        logger.info(f"[Leader] Refreshed profile for {proxy_wallet[:8]}..., name='{name}'")
        return True

    def update_leader(self, leader_id: int, name: str) -> bool:
        """更新 Leader 名字"""
        leader = self.get_leader_by_id(leader_id)
        if not leader:
            return False
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"UPDATE leaders SET name = %s, updated_at = {UTC8_DB_NOW_SQL} WHERE id = %s",
                (name, leader_id)
            )
            conn.commit()
            self._cache[leader["proxy_wallet"].lower()] = name
            return cursor.rowcount > 0
        finally:
            conn.close()

    def delete_leader(self, leader_id: int) -> bool:
        """删除 Leader"""
        leader = self.get_leader_by_id(leader_id)
        if not leader:
            return False
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("DELETE FROM leaders WHERE id = %s", (leader_id,))
            conn.commit()
            self._cache.pop(leader["proxy_wallet"].lower(), None)
            return cursor.rowcount > 0
        finally:
            conn.close()

    # ==================== 名字查询（本地优先） ====================

    def get_leader_name(self, leader_address: str) -> str:
        """同步快速查找：缓存优先，未命中返回截断地址，同时异步构建缓存"""
        addr = leader_address.lower()
        if addr not in self._cache:
            asyncio.create_task(self._warm_leader_cache(addr))
            return leader_address[:8]
        cached = self._cache[addr]
        # loading marker → 当作未命中处理
        if cached == "__loading__":
            return leader_address[:8]
        return cached

    async def _warm_leader_cache(self, addr):
        """异步填充 leader 名字缓存：DB → Polymarket API → 落库"""
        marker = "__loading__"
        if self._cache.get(addr) == marker:
            return
        self._cache[addr] = marker

        leader = await asyncio.to_thread(self.get_leader_by_address, addr)
        if leader and leader.get("name"):
            self._cache[addr] = leader["name"]
            return

        # DB 没有，查 Polymarket API（同步 HTTP，丢到线程池）
        profile = await asyncio.to_thread(self._fetch_profile_from_poly, addr)
        name = profile.get("name") or profile.get("pseudonym") or ""
        if name:
            self._cache[addr] = name
        else:
            self._cache.pop(addr, None)

    def _fetch_profile_from_poly(self, leader_address: str) -> dict:
        """从 Polymarket API 获取 leader profile"""
        try:
            resp = requests.get(
                f"{GAMMA_API_URL}/public-profile?address={leader_address}",
                timeout=5
            )
            if resp.status_code == 200:
                data = resp.json()
                logger.debug(f"[Leader] Polymarket profile for {leader_address[:8]}...: name='{data.get('name')}', pseudonym='{data.get('pseudonym')}'")
                return data
            else:
                logger.warning(
                    f"[Leader] Polymarket API returned {resp.status_code} for "
                    f"{leader_address[:8]}..."
                )
        except Exception as e:
            logger.warning(f"[Leader] Failed to fetch Polymarket profile for {leader_address[:8]}...: {e}")
        return {}


# 全局单例
_leader_service: Optional['LeaderService'] = None


def get_leader_service() -> LeaderService:
    """获取或创建 LeaderService 单例"""
    global _leader_service
    if _leader_service is None:
        _leader_service = LeaderService()
    return _leader_service

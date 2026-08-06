"""跟单模块数据库操作"""
import json
from typing import Any, Optional, List, Dict
import logging

from shared.db import get_db_connection
from shared.time_utils import UTC8_DB_NOW_SQL, format_utc8, now_utc8_dt, to_utc8_dt
from .types import CopyTradingConfig, CopyTradingOrder, INF, DEFAULT_TAKER_SPREAD_THRESHOLD, DEFAULT_EXCEED_THR, DEFAULT_BUY_PRICE_MIN, DEFAULT_BUY_PRICE_MAX, DEFAULT_SELL_PRICE_MIN, DEFAULT_SELL_PRICE_MAX, DEFAULT_BUY_PRICE_FILTER_MIN, DEFAULT_BUY_PRICE_FILTER_MAX

logger = logging.getLogger(__name__)


# ==================== 配置操作 ====================

def get_copy_trading_configs(enabled_only: bool = False) -> List[CopyTradingConfig]:
    """获取跟单配置列表"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if enabled_only:
            cursor.execute("""
                SELECT id, leader_proxy_wallet, follower_proxy_wallet, threshold, allowance,
                       share_ratio, enabled, owner_user_id, gtd_expiration_sec, buy_spread_thr, sell_spread_thr,
                       buy_exceed_thr, sell_exceed_thr, buy_price_min, buy_price_max, sell_price_min, sell_price_max,
                       buy_follow_taker, sell_follow_taker, buy_only,
                       buy_price_filter_min, buy_price_filter_max,
                       created_at, updated_at
                FROM copy_trading_configs WHERE enabled = 1
            """)
        else:
            cursor.execute("""
                SELECT id, leader_proxy_wallet, follower_proxy_wallet, threshold, allowance,
                       share_ratio, enabled, owner_user_id, gtd_expiration_sec, buy_spread_thr, sell_spread_thr,
                       buy_exceed_thr, sell_exceed_thr, buy_price_min, buy_price_max, sell_price_min, sell_price_max,
                       buy_follow_taker, sell_follow_taker, buy_only,
                       buy_price_filter_min, buy_price_filter_max,
                       created_at, updated_at
                FROM copy_trading_configs
            """)
        return [CopyTradingConfig(
            id=row[0],
            leader_proxy_wallet=row[1],
            follower_proxy_wallet=row[2],
            threshold=float(row[3]) if row[3] else INF,
            allowance=INF if row[4] >= INF else float(row[4]),
            share_ratio=float(row[5]),
            enabled=bool(row[6]),
            owner_user_id=int(row[7] or 0),
            gtd_expiration_sec=int(row[8] or 1800),
            buy_spread_thr=float(row[9]) if row[9] is not None else DEFAULT_TAKER_SPREAD_THRESHOLD,
            sell_spread_thr=float(row[10]) if row[10] is not None else DEFAULT_TAKER_SPREAD_THRESHOLD,
            buy_exceed_thr=bool(row[11]) if row[11] is not None else DEFAULT_EXCEED_THR,
            sell_exceed_thr=bool(row[12]) if row[12] is not None else DEFAULT_EXCEED_THR,
            buy_price_min=float(row[13]) if row[13] is not None else DEFAULT_BUY_PRICE_MIN,
            buy_price_max=float(row[14]) if row[14] is not None else DEFAULT_BUY_PRICE_MAX,
            sell_price_min=float(row[15]) if row[15] is not None else DEFAULT_SELL_PRICE_MIN,
            sell_price_max=float(row[16]) if row[16] is not None else DEFAULT_SELL_PRICE_MAX,
            buy_follow_taker=bool(row[17]) if row[17] is not None else True,
            sell_follow_taker=bool(row[18]) if row[18] is not None else True,
            buy_only=bool(row[19]) if row[19] is not None else False,
            buy_price_filter_min=float(row[20]) if row[20] is not None else DEFAULT_BUY_PRICE_FILTER_MIN,
            buy_price_filter_max=float(row[21]) if row[21] is not None else DEFAULT_BUY_PRICE_FILTER_MAX,
        ) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_copy_trading_config_by_id(config_id: int) -> Optional[CopyTradingConfig]:
    """获取单个跟单配置"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT id, leader_proxy_wallet, follower_proxy_wallet, threshold, allowance,
                   share_ratio, enabled, owner_user_id, gtd_expiration_sec, buy_spread_thr, sell_spread_thr,
                   buy_exceed_thr, sell_exceed_thr, buy_price_min, buy_price_max, sell_price_min, sell_price_max,
                   buy_follow_taker, sell_follow_taker, buy_only,
                   buy_price_filter_min, buy_price_filter_max,
                   created_at, updated_at
            FROM copy_trading_configs WHERE id = %s
        """, (config_id,))
        row = cursor.fetchone()
        if row:
            return CopyTradingConfig(
                id=row[0],
                leader_proxy_wallet=row[1],
                follower_proxy_wallet=row[2],
                threshold=float(row[3]) if row[3] else INF,
                allowance=INF if row[4] >= INF else float(row[4]),
                share_ratio=float(row[5]),
                enabled=bool(row[6]),
                owner_user_id=int(row[7] or 0),
                gtd_expiration_sec=int(row[8] or 1800),
                buy_spread_thr=float(row[9]) if row[9] is not None else DEFAULT_TAKER_SPREAD_THRESHOLD,
                sell_spread_thr=float(row[10]) if row[10] is not None else DEFAULT_TAKER_SPREAD_THRESHOLD,
                buy_exceed_thr=bool(row[11]) if row[11] is not None else DEFAULT_EXCEED_THR,
                sell_exceed_thr=bool(row[12]) if row[12] is not None else DEFAULT_EXCEED_THR,
                buy_price_min=float(row[13]) if row[13] is not None else DEFAULT_BUY_PRICE_MIN,
                buy_price_max=float(row[14]) if row[14] is not None else DEFAULT_BUY_PRICE_MAX,
                sell_price_min=float(row[15]) if row[15] is not None else DEFAULT_SELL_PRICE_MIN,
                sell_price_max=float(row[16]) if row[16] is not None else DEFAULT_SELL_PRICE_MAX,
                buy_follow_taker=bool(row[17]) if row[17] is not None else True,
                sell_follow_taker=bool(row[18]) if row[18] is not None else True,
                buy_only=bool(row[19]) if row[19] is not None else False,
                buy_price_filter_min=float(row[20]) if row[20] is not None else DEFAULT_BUY_PRICE_FILTER_MIN,
                buy_price_filter_max=float(row[21]) if row[21] is not None else DEFAULT_BUY_PRICE_FILTER_MAX,
            )
        return None
    finally:
        conn.close()


def create_copy_trading_config(
    leader_proxy_wallet: str,
    follower_proxy_wallet: str,
    share_ratio: float,
    threshold: float,
    owner_user_id: int = 0,
    buy_spread_thr: float = DEFAULT_TAKER_SPREAD_THRESHOLD,
    sell_spread_thr: float = DEFAULT_TAKER_SPREAD_THRESHOLD,
    buy_exceed_thr: bool = DEFAULT_EXCEED_THR,
    sell_exceed_thr: bool = DEFAULT_EXCEED_THR,
    buy_follow_taker: bool = True,
    sell_follow_taker: bool = True,
    buy_price_min: float = DEFAULT_BUY_PRICE_MIN,
    buy_price_max: float = DEFAULT_BUY_PRICE_MAX,
    sell_price_min: float = DEFAULT_SELL_PRICE_MIN,
    sell_price_max: float = DEFAULT_SELL_PRICE_MAX,
    buy_price_filter_min: float = DEFAULT_BUY_PRICE_FILTER_MIN,
    buy_price_filter_max: float = DEFAULT_BUY_PRICE_FILTER_MAX,
) -> int:
    """创建跟单配置，返回配置 ID"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO copy_trading_configs
            (leader_proxy_wallet, follower_proxy_wallet, share_ratio, threshold, allowance, owner_user_id,
             buy_spread_thr, sell_spread_thr, buy_exceed_thr, sell_exceed_thr,
             buy_follow_taker, sell_follow_taker,
             buy_price_min, buy_price_max, sell_price_min, sell_price_max,
             buy_price_filter_min, buy_price_filter_max)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (leader_proxy_wallet.lower(), follower_proxy_wallet.lower(), share_ratio, threshold, threshold, owner_user_id,
              buy_spread_thr, sell_spread_thr, int(buy_exceed_thr), int(sell_exceed_thr),
              int(buy_follow_taker), int(sell_follow_taker),
              buy_price_min, buy_price_max, sell_price_min, sell_price_max,
              buy_price_filter_min, buy_price_filter_max))
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def update_config_allowance(config_id: int, allowance: float) -> bool:
    """更新 config 的 allowance"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(f"""
            UPDATE copy_trading_configs SET allowance = %s, updated_at = {UTC8_DB_NOW_SQL}
            WHERE id = %s
        """, (allowance, config_id))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def update_copy_trading_config(config_id: int, **kwargs) -> bool:
    """更新跟单配置"""
    allowed_fields = {"share_ratio", "enabled", "leader_proxy_wallet", "follower_proxy_wallet", "threshold", "allowance", "gtd_expiration_sec", "buy_spread_thr", "sell_spread_thr", "buy_exceed_thr", "sell_exceed_thr", "buy_price_min", "buy_price_max", "sell_price_min", "sell_price_max", "buy_follow_taker", "sell_follow_taker", "buy_only", "buy_price_filter_min", "buy_price_filter_max"}
    update_fields = {}
    for k, v in kwargs.items():
        if k in allowed_fields:
            update_fields[k] = v.lower() if isinstance(v, str) and "wallet" in k else v
    if not update_fields:
        return False

    set_clause = ", ".join([f"{k} = %s" for k in update_fields.keys()])
    values = list(update_fields.values()) + [config_id]

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(f"""
            UPDATE copy_trading_configs SET {set_clause}, updated_at = {UTC8_DB_NOW_SQL}
            WHERE id = %s
        """, values)
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def delete_copy_trading_config(config_id: int) -> bool:
    """删除跟单配置"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM copy_trading_configs WHERE id = %s", (config_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


# ==================== Leader 仓位操作 ====================

def get_leader_position(leader_proxy_wallet: str, asset_id: str) -> Optional[float]:
    """获取某 leader 某资产的累计持仓"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT size FROM copy_trading_leader_positions
            WHERE leader_proxy_wallet = %s AND asset_id = %s
        """, (leader_proxy_wallet.lower(), asset_id))
        row = cursor.fetchone()
        return float(row[0]) if row else None
    finally:
        conn.close()


def get_all_leader_positions(leader_proxy_wallet: str) -> Dict[str, float]:
    """获取某 leader 所有资产累计仓位"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT asset_id, size FROM copy_trading_leader_positions
            WHERE leader_proxy_wallet = %s
        """, (leader_proxy_wallet.lower(),))
        return {row[0]: float(row[1]) for row in cursor.fetchall()}
    finally:
        conn.close()


def upsert_leader_position(leader_proxy_wallet: str, asset_id: str, size: float) -> None:
    """更新 leader 累计仓位"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if size <= 0.001:
            cursor.execute("""
                DELETE FROM copy_trading_leader_positions
                WHERE leader_proxy_wallet = %s AND asset_id = %s
            """, (leader_proxy_wallet.lower(), asset_id))
        else:
            cursor.execute(f"""
                INSERT INTO copy_trading_leader_positions (leader_proxy_wallet, asset_id, size)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE size = VALUES(size), updated_at = {UTC8_DB_NOW_SQL}
            """, (leader_proxy_wallet.lower(), asset_id, size))
        conn.commit()
    finally:
        conn.close()


def batch_upsert_leader_positions(leader_proxy_wallet: str, positions: List[dict], full_sync: bool = False):
    """增量 upsert leader 持仓到 DB，full_sync=True 时额外删除不在 positions 中的旧记录"""
    if not leader_proxy_wallet:
        return
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        wallet = leader_proxy_wallet.lower()
        if positions:
            cursor.executemany("""
                INSERT INTO copy_trading_leader_positions (leader_proxy_wallet, asset_id, size)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE size = VALUES(size), updated_at = (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR)
            """, [(wallet, p["asset_id"], p["size"]) for p in positions])
        if full_sync:
            asset_ids = [p["asset_id"] for p in positions]
            if asset_ids:
                placeholders = ",".join(["%s"] * len(asset_ids))
                cursor.execute(f"""
                    DELETE FROM copy_trading_leader_positions
                    WHERE leader_proxy_wallet = %s AND asset_id NOT IN ({placeholders})
                """, [wallet] + asset_ids)
            else:
                cursor.execute("""
                    DELETE FROM copy_trading_leader_positions
                    WHERE leader_proxy_wallet = %s
                """, (wallet,))
        conn.commit()
    finally:
        conn.close()


def delete_leader_positions(leader_proxy_wallet: str):
    """删除某 leader 的所有持仓记录"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            DELETE FROM copy_trading_leader_positions
            WHERE leader_proxy_wallet = %s
        """, (leader_proxy_wallet,))
        conn.commit()
    finally:
        conn.close()


def delete_follower_positions(follower_proxy_wallet: str):
    """删除某 follower 的所有持仓记录"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            DELETE FROM copy_trading_follower_positions
            WHERE follower_proxy_wallet = %s
        """, (follower_proxy_wallet,))
        conn.commit()
    finally:
        conn.close()


# ==================== 仓位历史快照 ====================

def upsert_config_asset(config_id: int, asset_id: str, last_seen_at: Optional[Any] = None) -> None:
    """有交易事件时更新 last_seen_at（取较新值）。"""
    batch_upsert_config_asset([config_id], asset_id, last_seen_at)


def batch_upsert_config_asset(config_ids: List[int], asset_id: str, last_seen_at: Optional[Any] = None) -> None:
    """批量更新多个 config 的 asset last_seen_at。"""
    if not config_ids:
        return
    seen_at = to_utc8_dt(last_seen_at) or now_utc8_dt()
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.executemany("""
            INSERT INTO copy_trading_config_assets (config_id, asset_id, last_seen_at)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE
              last_seen_at = GREATEST(last_seen_at, VALUES(last_seen_at))
        """, [(cid, asset_id, seen_at) for cid in config_ids])
        conn.commit()
    finally:
        conn.close()


def record_position_history(
    config_id: int,
    asset_id: str,
    leader_proxy_wallet: str,
    follower_proxy_wallet: str,
    share_ratio: float,
    leader_position: float,
    follower_position: float,
    follower_pending_buy: float,
    follower_pending_sell: float,
    source: str,
    side: Optional[str] = None,
    event_size: Optional[float] = None,
    event_price: Optional[float] = None,
    order_id: Optional[str] = None,
    leader_tx_hash: Optional[str] = None,
    raw_context: Optional[Any] = None,
) -> int:
    """记录某个跟单配置在某个 asset 上的 leader/follower 仓位快照。"""
    raw_context_text = None
    if raw_context is not None:
        raw_context_text = raw_context if isinstance(raw_context, str) else json.dumps(raw_context, ensure_ascii=False, default=str)

    created_at = now_utc8_dt()
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO copy_trading_position_history (
                config_id, asset_id, leader_proxy_wallet, follower_proxy_wallet, share_ratio,
                leader_position, follower_position, follower_pending_buy, follower_pending_sell,
                source, side, event_size, event_price, order_id, leader_tx_hash, raw_context, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            config_id,
            asset_id,
            leader_proxy_wallet.lower(),
            follower_proxy_wallet.lower(),
            share_ratio,
            leader_position,
            follower_position,
            follower_pending_buy,
            follower_pending_sell,
            source,
            side,
            event_size,
            event_price,
            order_id,
            leader_tx_hash,
            raw_context_text,
            created_at,
        ))
        history_id = cursor.lastrowid
        conn.commit()
        return history_id
    finally:
        conn.close()


def get_position_history_from_db(
    config_id: int,
    asset_id: str,
    start: Optional[Any] = None,
    end: Optional[Any] = None,
    normalized: bool = False,
    limit: int = 2000,
) -> List[dict]:
    """查询某配置某 asset 的仓位历史曲线点。"""
    limit = max(1, min(int(limit or 2000), 5000))
    start_dt = to_utc8_dt(start)
    end_dt = to_utc8_dt(end)

    where = ["config_id = %s", "asset_id = %s"]
    params: List[Any] = [config_id, asset_id]
    if start_dt:
        where.append("created_at >= %s")
        params.append(start_dt)
    if end_dt:
        where.append("created_at <= %s")
        params.append(end_dt)
    params.append(limit)

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(f"""
            SELECT id, config_id, asset_id, leader_proxy_wallet, follower_proxy_wallet, share_ratio,
                   leader_position, follower_position, follower_pending_buy, follower_pending_sell,
                   source, side, event_size, event_price, order_id, leader_tx_hash, raw_context, created_at
            FROM copy_trading_position_history
            WHERE {' AND '.join(where)}
            ORDER BY created_at ASC
            LIMIT %s
        """, tuple(params))
        rows = []
        for row in cursor.fetchall():
            share_ratio = float(row[5])
            leader_position = float(row[6])
            follower_position = float(row[7])
            rows.append({
                "id": row[0],
                "config_id": row[1],
                "asset_id": row[2],
                "leader_proxy_wallet": row[3],
                "follower_proxy_wallet": row[4],
                "share_ratio": share_ratio,
                "leader_position": leader_position,
                "follower_position": follower_position,
                "follower_pending_buy": float(row[8]),
                "follower_pending_sell": float(row[9]),
                "source": row[10],
                "side": row[11],
                "event_size": float(row[12]) if row[12] is not None else None,
                "event_price": float(row[13]) if row[13] is not None else None,
                "order_id": row[14],
                "leader_tx_hash": row[15],
                "raw_context": row[16],
                "created_at": format_utc8(row[17]),
                "leader_value": leader_position * share_ratio if normalized else leader_position,
                "follower_value": follower_position,
            })
        return rows
    finally:
        conn.close()


def get_assets_of_cfg_from_db(config_id: int, since: Optional[Any] = None, limit: int = 1000) -> List[dict]:
    """查询某配置已有仓位历史的 asset 列表。since 可筛选 last_seen_at >= since 的 asset。"""
    limit = max(1, min(int(limit or 1000), 5000))
    since_dt = to_utc8_dt(since)
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        where = ["ca.config_id = %s"]
        params: List[Any] = [config_id]
        if since_dt:
            where.append("ca.last_seen_at >= %s")
            params.append(since_dt)
        params.append(limit)
        cursor.execute(f"""
            SELECT ca.asset_id,
                   COALESCE(aq.question, '') AS question,
                   COALESCE(aq.outcome, '') AS outcome,
                   ca.last_seen_at
            FROM copy_trading_config_assets ca
            LEFT JOIN copy_trading_asset_questions aq ON aq.asset_id = ca.asset_id
            WHERE {' AND '.join(where)}
            ORDER BY ca.last_seen_at DESC
            LIMIT %s
        """, tuple(params))
        return [
            {
                "asset_id": row[0],
                "question": row[1] or "",
                "outcome": row[2] or "",
                "last_seen_at": format_utc8(row[3]),
            }
            for row in cursor.fetchall()
        ]
    finally:
        conn.close()


# ==================== 跟单订单操作 ====================

def record_copy_trading_order(
    order_id: str,
    config_id: int,
    leader: str,
    follower: str,
    leader_tx_hash: str,
    asset_id: str,
    side: str,
    leader_size: float,
    leader_price: float,
    follow_size: float,
    follow_price: float,
    size_matched: float,
    status: str,
    err_msg: Optional[str] = None,
    leader_role: Optional[str] = None,
    follower_role: Optional[str] = None,
    created_at: Optional[Any] = None,
    updated_at: Optional[Any] = None
) -> str:
    """记录跟单订单（INSERT，id 为主键）"""
    now = now_utc8_dt()
    created_dt = to_utc8_dt(created_at) or now
    updated_dt = to_utc8_dt(updated_at) or now
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO copy_trading_orders
            (id, config_id, leader, follower, leader_tx_hash, asset_id, side,
             leader_size, leader_price, follow_size, follow_price, size_matched, status, err_msg,
             leader_role, follower_role, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (order_id, config_id, leader.lower(), follower.lower(), leader_tx_hash, asset_id, side,
              leader_size, leader_price, follow_size, follow_price, size_matched, status, err_msg,
              leader_role, follower_role,
              created_dt, updated_dt))
        conn.commit()
        return order_id
    finally:
        conn.close()


def update_copy_trading_order(
    order_id: str,
    size_matched: float,
    status: str,
    err_msg: Optional[str] = None,
) -> bool:
    """更新 size_matched / status / updated_at，可选更新 err_msg"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if err_msg is not None:
            cursor.execute("""
                UPDATE copy_trading_orders
                SET size_matched = %s, status = %s, err_msg = %s, updated_at = %s
                WHERE id = %s
            """, (size_matched, status, err_msg, now_utc8_dt(), order_id))
        else:
            cursor.execute("""
                UPDATE copy_trading_orders
                SET size_matched = %s, status = %s, updated_at = %s
                WHERE id = %s
            """, (size_matched, status, now_utc8_dt(), order_id))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def get_orders_by_config_id(config_id: int, limit: int = 100, offset: int = 0) -> List[CopyTradingOrder]:
    """获取跟单订单历史"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT id, config_id, leader, follower, leader_tx_hash, asset_id, side,
                   leader_size, leader_price, follow_size, follow_price, size_matched, status,
                   leader_role, follower_role, created_at, updated_at
            FROM copy_trading_orders
            WHERE config_id = %s
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """, (config_id, limit, offset))
        return [CopyTradingOrder(
            id=row[0], config_id=row[1], leader=row[2], follower=row[3],
            leader_tx_hash=row[4], asset_id=row[5], side=row[6],
            leader_size=float(row[7]), leader_price=float(row[8]),
            follow_size=float(row[9]), follow_price=float(row[10]),
            size_matched=float(row[11]), status=row[12],
            leader_role=row[13], follower_role=row[14],
            created_at=format_utc8(row[15]), updated_at=format_utc8(row[16])
        ) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_orders_by_config_and_asset(config_id: int, asset_id: str, limit: int = 500, start: str = None, end: str = None) -> List[dict]:
    """获取某 config 某 asset 的有效订单（用于散点图）"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        sql = """
            SELECT side, follow_size, follow_price, size_matched, status, created_at, leader_size, leader_price,
                   leader_role, follower_role, err_msg
            FROM copy_trading_orders
            WHERE config_id = %s AND asset_id = %s
        """
        params: list = [config_id, asset_id]
        if start:
            sql += " AND created_at >= %s"
            params.append(start)
        if end:
            sql += " AND created_at <= %s"
            params.append(end)
        sql += " ORDER BY created_at ASC LIMIT %s"
        params.append(limit)
        cursor.execute(sql, params)
        return [
            {
                "side": row[0],
                "size": float(row[1]),
                "price": float(row[2]),
                "size_matched": float(row[3]),
                "status": row[4],
                "created_at": format_utc8(row[5]),
                "leader_size": float(row[6]),
                "leader_price": float(row[7]),
                "leader_role": row[8],
                "follower_role": row[9],
                "err_msg": row[10],
            }
            for row in cursor.fetchall()
        ]
    finally:
        conn.close()


def get_order_by_id(order_id: str) -> Optional[CopyTradingOrder]:
    """根据 order_id 查订单"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT id, config_id, leader, follower, leader_tx_hash, asset_id, side,
                   leader_size, leader_price, follow_size, follow_price, size_matched, status,
                   leader_role, follower_role, created_at, updated_at
            FROM copy_trading_orders
            WHERE id = %s
        """, (order_id,))
        row = cursor.fetchone()
        if row:
            return CopyTradingOrder(
                id=row[0], config_id=row[1], leader=row[2], follower=row[3],
                leader_tx_hash=row[4], asset_id=row[5], side=row[6],
                leader_size=float(row[7]), leader_price=float(row[8]),
                follow_size=float(row[9]), follow_price=float(row[10]),
                size_matched=float(row[11]), status=row[12],
                leader_role=row[13], follower_role=row[14],
                created_at=format_utc8(row[15]), updated_at=format_utc8(row[16])
            )
        return None
    finally:
        conn.close()


def get_live_buy_order_ids_by_asset(config_id: int, asset_id: str) -> List[str]:
    """查询指定 config 在指定 asset 上状态为 LIVE 的 BUY order_id 列表"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT id FROM copy_trading_orders WHERE config_id = %s AND asset_id = %s AND status = 'LIVE' AND side = 'BUY'",
            (config_id, asset_id)
        )
        return [row[0] for row in cursor.fetchall()]
    finally:
        conn.close()


# ==================== Asset Question 缓存 ====================

def get_asset_question_and_outcome(asset_id: str) -> Optional[tuple]:
    """从 DB 获取 asset 对应的 question 和 outcome"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT question, outcome FROM copy_trading_asset_questions WHERE asset_id = %s",
            (asset_id,)
        )
        row = cursor.fetchone()
        return (row[0], row[1]) if row else None
    finally:
        conn.close()


def upsert_asset_question(asset_id: str, question: str, outcome: str = ""):
    """写入/更新 asset question 到 DB"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(f"""
            INSERT INTO copy_trading_asset_questions (asset_id, question, outcome)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE question = VALUES(question),
                outcome = COALESCE(NULLIF(VALUES(outcome), ''), outcome),
                updated_at = {UTC8_DB_NOW_SQL}
        """, (asset_id, question, outcome or ""))
        conn.commit()
    finally:
        conn.close()


def batch_upsert_asset_questions(assets: List[dict]):
    """批量写入 asset question 到 DB，assets = [{asset_id, question, outcome}, ...]"""
    if not assets:
        return
    rows = [
        (a["asset_id"], a["question"], a.get("outcome", "") or "")
        for a in assets
        if a.get("asset_id") and a.get("question")
    ]
    if not rows:
        return
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.executemany(f"""
            INSERT INTO copy_trading_asset_questions (asset_id, question, outcome)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE question = VALUES(question),
                outcome = COALESCE(NULLIF(VALUES(outcome), ''), outcome),
                updated_at = {UTC8_DB_NOW_SQL}
        """, rows)
        conn.commit()
    finally:
        conn.close()


def upsert_follower_position(follower_proxy_wallet: str, asset_id: str, size: float):
    """写入/更新 follower 持仓到 DB（调试用）"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if size <= 0.01:
            cursor.execute("""
                DELETE FROM copy_trading_follower_positions
                WHERE follower_proxy_wallet = %s AND asset_id = %s
            """, (follower_proxy_wallet.lower(), asset_id))
        else:
            cursor.execute(f"""
                INSERT INTO copy_trading_follower_positions (follower_proxy_wallet, asset_id, size)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE size = VALUES(size), updated_at = {UTC8_DB_NOW_SQL}
            """, (follower_proxy_wallet.lower(), asset_id, size))
        conn.commit()
    finally:
        conn.close()


def batch_upsert_follower_positions(follower_proxy_wallet: str, positions: List[dict], full_sync: bool = False):
    """增量 upsert follower 持仓到 DB，full_sync=True 时额外删除不在 positions 中的旧记录"""
    if not follower_proxy_wallet:
        return
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        wallet = follower_proxy_wallet.lower()
        if positions:
            cursor.executemany("""
                INSERT INTO copy_trading_follower_positions (follower_proxy_wallet, asset_id, size)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE size = VALUES(size), updated_at = (UTC_TIMESTAMP(3) + INTERVAL 8 HOUR)
            """, [(wallet, p["asset_id"], p["size"]) for p in positions])
        if full_sync:
            asset_ids = [p["asset_id"] for p in positions]
            if asset_ids:
                placeholders = ",".join(["%s"] * len(asset_ids))
                cursor.execute(f"""
                    DELETE FROM copy_trading_follower_positions
                    WHERE follower_proxy_wallet = %s AND asset_id NOT IN ({placeholders})
                """, [wallet] + asset_ids)
            else:
                cursor.execute("""
                    DELETE FROM copy_trading_follower_positions
                    WHERE follower_proxy_wallet = %s
                """, (wallet,))
        conn.commit()
    finally:
        conn.close()


def upsert_follower_pending_sell(follower_proxy_wallet: str, asset_id: str, pending: float, question: str = ""):
    """写入/更新 follower 待成交锁单到 DB（调试用）"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if pending <= 0.01:
            cursor.execute("""
                DELETE FROM copy_trading_follower_pending_sell
                WHERE follower_proxy_wallet = %s AND asset_id = %s
            """, (follower_proxy_wallet.lower(), asset_id))
        else:
            cursor.execute(f"""
                INSERT INTO copy_trading_follower_pending_sell (follower_proxy_wallet, asset_id, pending, question)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE pending = VALUES(pending),
                    question = COALESCE(NULLIF(VALUES(question), ''), question),
                    updated_at = {UTC8_DB_NOW_SQL}
            """, (follower_proxy_wallet.lower(), asset_id, pending, question))
        conn.commit()
    finally:
        conn.close()


def upsert_follower_pending_buy(follower_proxy_wallet: str, asset_id: str, pending: float, question: str = ""):
    """写入/更新 follower 待成交 BUY 锁单到 DB（调试用）"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if pending <= 0.01:
            cursor.execute("""
                DELETE FROM copy_trading_follower_pending_buy
                WHERE follower_proxy_wallet = %s AND asset_id = %s
            """, (follower_proxy_wallet.lower(), asset_id))
        else:
            cursor.execute(f"""
                INSERT INTO copy_trading_follower_pending_buy (follower_proxy_wallet, asset_id, pending, question)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE pending = VALUES(pending),
                    question = COALESCE(NULLIF(VALUES(question), ''), question),
                    updated_at = {UTC8_DB_NOW_SQL}
            """, (follower_proxy_wallet.lower(), asset_id, pending, question))
        conn.commit()
    finally:
        conn.close()


def batch_upsert_follower_pending_sell(follower_proxy_wallet: str, pending_by_asset: dict):
    """批量写入/更新 follower 待成交 SELL 锁单到 DB（全量替换）"""
    f_addr = follower_proxy_wallet.lower()
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # 先删除该地址所有旧记录，再批量插入新数据（全量替换）
        cursor.execute("""
            DELETE FROM copy_trading_follower_pending_sell
            WHERE follower_proxy_wallet = %s
        """, (f_addr,))
        if pending_by_asset:
            rows = [(f_addr, asset_id, pending) for asset_id, pending in pending_by_asset.items()]
            cursor.executemany(f"""
                INSERT INTO copy_trading_follower_pending_sell (follower_proxy_wallet, asset_id, pending)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE pending = VALUES(pending),
                    updated_at = {UTC8_DB_NOW_SQL}
            """, rows)
        conn.commit()
    finally:
        conn.close()


def batch_upsert_follower_pending_buy(follower_proxy_wallet: str, pending_buy_asset: dict):
    """批量写入/更新 follower 待成交 BUY 锁单到 DB（全量替换）"""
    f_addr = follower_proxy_wallet.lower()
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            DELETE FROM copy_trading_follower_pending_buy
            WHERE follower_proxy_wallet = %s
        """, (f_addr,))
        if pending_buy_asset:
            rows = [(f_addr, asset_id, pending) for asset_id, pending in pending_buy_asset.items()]
            cursor.executemany(f"""
                INSERT INTO copy_trading_follower_pending_buy (follower_proxy_wallet, asset_id, pending)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE pending = VALUES(pending),
                    updated_at = {UTC8_DB_NOW_SQL}
            """, rows)
        conn.commit()
    finally:
        conn.close()


# ==================== Share Debt（份额债务缓冲）====================

def upsert_share_debt(follower_proxy_wallet: str, asset_id: str, side: str, debt: float):
    """写入/更新 share_debt 到 DB"""
    f_addr = follower_proxy_wallet.lower()
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if -0.01 <= debt <= 0.01:
            cursor.execute("""
                DELETE FROM copy_trading_share_debt
                WHERE follower_proxy_wallet = %s AND asset_id = %s AND side = %s
            """, (f_addr, asset_id, side))
        else:
            cursor.execute(f"""
                INSERT INTO copy_trading_share_debt (follower_proxy_wallet, asset_id, side, debt)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE debt = VALUES(debt),
                    updated_at = {UTC8_DB_NOW_SQL}
            """, (f_addr, asset_id, side, debt))
        conn.commit()
    finally:
        conn.close()


def get_share_debt(follower_proxy_wallet: str, asset_id: str, side: str) -> float:
    """从 DB 获取单条 share_debt"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT debt FROM copy_trading_share_debt
            WHERE follower_proxy_wallet = %s AND asset_id = %s AND side = %s
        """, (follower_proxy_wallet.lower(), asset_id, side))
        row = cursor.fetchone()
        return float(row[0]) if row else 0.0
    finally:
        conn.close()


def get_all_share_debts(follower_proxy_wallet: str, side: str) -> Dict[str, float]:
    """从 DB 获取某 follower 某 side 的所有债务"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT asset_id, debt FROM copy_trading_share_debt
            WHERE follower_proxy_wallet = %s AND side = %s
        """, (follower_proxy_wallet.lower(), side))
        return {row[0]: float(row[1]) for row in cursor.fetchall()}
    finally:
        conn.close()


def batch_upsert_share_debt(follower_proxy_wallet: str, side: str, debts: Dict[str, float]):
    """批量写入/更新 share_debt 到 DB（全量替换）"""
    f_addr = follower_proxy_wallet.lower()
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            DELETE FROM copy_trading_share_debt
            WHERE follower_proxy_wallet = %s AND side = %s
        """, (f_addr, side))
        rows = [(f_addr, asset_id, side, debt) for asset_id, debt in debts.items() if not (-0.01 <= debt <= 0.01)]
        if rows:
            cursor.executemany(f"""
                INSERT INTO copy_trading_share_debt (follower_proxy_wallet, asset_id, side, debt)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE debt = VALUES(debt),
                    updated_at = {UTC8_DB_NOW_SQL}
            """, rows)
        conn.commit()
    finally:
        conn.close()


# ==================== 定时调度操作 ====================

def get_all_schedules(enabled_only: bool = True) -> List[Dict]:
    """获取所有调度规则"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        sql = """
            SELECT id, config_id, start_cron, stop_cron, enabled, last_triggered_at, created_at, updated_at
            FROM copy_trading_schedules
        """
        if enabled_only:
            sql += " WHERE enabled = 1"
        cursor.execute(sql)
        return [
            {
                "id": row[0],
                "config_id": row[1],
                "start_cron": row[2],
                "stop_cron": row[3],
                "enabled": bool(row[4]),
                "last_triggered_at": row[5],
                "created_at": row[6],
                "updated_at": row[7],
            }
            for row in cursor.fetchall()
        ]
    finally:
        conn.close()


def get_schedule_by_config_id(config_id: int) -> Optional[Dict]:
    """获取指定 config 的调度规则"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT id, config_id, start_cron, stop_cron, enabled, last_triggered_at, created_at, updated_at
            FROM copy_trading_schedules WHERE config_id = %s
        """, (config_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "config_id": row[1],
            "start_cron": row[2],
            "stop_cron": row[3],
            "enabled": bool(row[4]),
            "last_triggered_at": row[5],
            "created_at": row[6],
            "updated_at": row[7],
        }
    finally:
        conn.close()


def upsert_schedule(config_id: int, start_cron: Optional[str], stop_cron: Optional[str], enabled: bool = True):
    """创建或更新调度规则（每个 config 仅一条）"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(f"""
            INSERT INTO copy_trading_schedules (config_id, start_cron, stop_cron, enabled)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                start_cron = VALUES(start_cron),
                stop_cron = VALUES(stop_cron),
                enabled = VALUES(enabled),
                updated_at = {UTC8_DB_NOW_SQL}
        """, (config_id, start_cron, stop_cron, int(enabled)))
        conn.commit()
    finally:
        conn.close()


def delete_schedule(config_id: int) -> bool:
    """删除调度规则"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM copy_trading_schedules WHERE config_id = %s", (config_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def update_schedule_last_triggered(schedule_id: int):
    """更新调度规则的最近触发时间"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(f"""
            UPDATE copy_trading_schedules SET last_triggered_at = {UTC8_DB_NOW_SQL} WHERE id = %s
        """, (schedule_id,))
        conn.commit()
    finally:
        conn.close()


# ==================== Slug 过滤操作 ====================

def get_slug_filter_by_config_id(config_id: int) -> Optional[Dict]:
    """获取指定 config 的 slug 过滤规则"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT id, config_id, mode, slugs, created_at, updated_at
            FROM copy_trading_slug_filters WHERE config_id = %s
        """, (config_id,))
        row = cursor.fetchone()
        if not row:
            return None
        slugs_raw = row[3]
        if isinstance(slugs_raw, str):
            slugs_raw = json.loads(slugs_raw)
        return {
            "id": row[0],
            "config_id": row[1],
            "mode": row[2],
            "slugs": slugs_raw,
            "created_at": row[4],
            "updated_at": row[5],
        }
    finally:
        conn.close()


def get_all_slug_filters() -> List[Dict]:
    """获取所有 slug 过滤规则"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT id, config_id, mode, slugs, created_at, updated_at
            FROM copy_trading_slug_filters
        """)
        results = []
        for row in cursor.fetchall():
            slugs_raw = row[3]
            if isinstance(slugs_raw, str):
                slugs_raw = json.loads(slugs_raw)
            results.append({
                "id": row[0],
                "config_id": row[1],
                "mode": row[2],
                "slugs": slugs_raw,
                "created_at": row[4],
                "updated_at": row[5],
            })
        return results
    finally:
        conn.close()


def upsert_slug_filter(config_id: int, mode: str, slugs: List[str]):
    """创建或更新 slug 过滤规则"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(f"""
            INSERT INTO copy_trading_slug_filters (config_id, mode, slugs)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE
                mode = VALUES(mode),
                slugs = VALUES(slugs),
                updated_at = {UTC8_DB_NOW_SQL}
        """, (config_id, mode, json.dumps(slugs)))
        conn.commit()
    finally:
        conn.close()


def delete_slug_filter(config_id: int) -> bool:
    """删除 slug 过滤规则"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM copy_trading_slug_filters WHERE config_id = %s", (config_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()

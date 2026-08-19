"""跟单模块数据库操作"""
from typing import Any, Optional, List, Dict
import logging

from shared.db import get_db_connection
from shared.time_utils import UTC8_DB_NOW_SQL, format_utc8, now_utc8_dt, to_utc8_dt
from .types import CopyTradingConfig, CopyTradingOrder

logger = logging.getLogger(__name__)


# ==================== 配置操作 ====================

def get_copy_trading_configs(enabled_only: bool = False) -> List[CopyTradingConfig]:
    """获取跟单配置列表"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if enabled_only:
            cursor.execute("""
                SELECT id, leader_proxy_wallet, follower_proxy_wallet,
                       enabled, owner_user_id, gtd_expiration_sec, buy_size,
                       sweep_confirm_window_ms
                FROM copy_trading_configs WHERE enabled = 1
            """)
        else:
            cursor.execute("""
                SELECT id, leader_proxy_wallet, follower_proxy_wallet,
                       enabled, owner_user_id, gtd_expiration_sec, buy_size,
                       sweep_confirm_window_ms
                FROM copy_trading_configs
            """)
        return [CopyTradingConfig(
            id=row[0],
            leader_proxy_wallet=row[1],
            follower_proxy_wallet=row[2],
            enabled=bool(row[3]),
            owner_user_id=int(row[4] or 0),
            gtd_expiration_sec=int(row[5] or 1800),
            buy_size=float(row[6] or 100),
            sweep_confirm_window_ms=int(row[7] or 0),
        ) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_copy_trading_config_by_id(config_id: int) -> Optional[CopyTradingConfig]:
    """获取单个跟单配置"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT id, leader_proxy_wallet, follower_proxy_wallet,
                   enabled, owner_user_id, gtd_expiration_sec, buy_size,
                   sweep_confirm_window_ms
            FROM copy_trading_configs WHERE id = %s
        """, (config_id,))
        row = cursor.fetchone()
        if row:
            return CopyTradingConfig(
                id=row[0],
                leader_proxy_wallet=row[1],
                follower_proxy_wallet=row[2],
                enabled=bool(row[3]),
                owner_user_id=int(row[4] or 0),
                gtd_expiration_sec=int(row[5] or 1800),
                buy_size=float(row[6] or 100),
                sweep_confirm_window_ms=int(row[7] or 0),
            )
        return None
    finally:
        conn.close()


def create_copy_trading_config(
    leader_proxy_wallet: str,
    follower_proxy_wallet: str,
    owner_user_id: int = 0,
) -> int:
    """创建跟单配置，返回配置 ID"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO copy_trading_configs
            (leader_proxy_wallet, follower_proxy_wallet, owner_user_id)
            VALUES (%s, %s, %s)
        """, (leader_proxy_wallet.lower(), follower_proxy_wallet.lower(), owner_user_id))
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def update_copy_trading_config(config_id: int, **kwargs) -> bool:
    """更新跟单配置"""
    allowed_fields = {"enabled", "leader_proxy_wallet", "follower_proxy_wallet", "gtd_expiration_sec", "buy_size", "sweep_confirm_window_ms"}
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
    signal_latency_ms: Optional[int] = None,
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
             signal_latency_ms, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (order_id, config_id, leader.lower(), follower.lower(), leader_tx_hash, asset_id, side,
              leader_size, leader_price, follow_size, follow_price, size_matched, status, err_msg,
              signal_latency_ms, created_dt, updated_dt))
        conn.commit()
        return order_id
    finally:
        conn.close()


def update_order_sweep_to_leader(order_id: str, sweep_to_leader_ms: int) -> bool:
    """更新 sweep 订单的 sweep_to_leader_ms（sweep入场→leader信号确认延迟）"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE copy_trading_orders
            SET sweep_to_leader_ms = %s, updated_at = %s
            WHERE id = %s
        """, (sweep_to_leader_ms, now_utc8_dt(), order_id))
        conn.commit()
        return cursor.rowcount > 0
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
                   created_at, updated_at
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
            created_at=format_utc8(row[13]), updated_at=format_utc8(row[14])
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
                   err_msg, signal_latency_ms, sweep_to_leader_ms
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
                "err_msg": row[8],
                "signal_latency_ms": int(row[9]) if row[9] is not None else None,
                "sweep_to_leader_ms": int(row[10]) if row[10] is not None else None,
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
                   created_at, updated_at
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
                created_at=format_utc8(row[13]), updated_at=format_utc8(row[14])
            )
        return None
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


# ==================== Dashboard 统计 ====================

def get_strategy_stats(days: int = 7) -> dict:
    """策略运行统计：信号质量 + 执行效率"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # sweep 信号数
        cursor.execute("""
            SELECT COUNT(*) FROM weather_notifications
            WHERE event_type = 'sweep' AND outcome = 'no'
              AND occurred_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
        """, (days,))
        sweep_signal_count = cursor.fetchone()[0]

        # sweep 入场订单数 + 成交量
        cursor.execute("""
            SELECT COUNT(*), COALESCE(SUM(size_matched), 0)
            FROM copy_trading_orders
            WHERE leader_tx_hash = 'WEATHER_SWEEP' AND side = 'BUY' AND status != 'ERROR'
              AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
        """, (days,))
        row = cursor.fetchone()
        sweep_entry_count = row[0]
        sweep_filled_total = float(row[1])

        # leader 信号数（真实 tx hash）
        cursor.execute("""
            SELECT COUNT(*)
            FROM copy_trading_orders
            WHERE side = 'BUY' AND leader_tx_hash LIKE '0x%%' AND LENGTH(leader_tx_hash) > 20
              AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
        """, (days,))
        leader_signal_count = cursor.fetchone()[0]

        # leader 信号后成功入场数（size_matched > 0）
        cursor.execute("""
            SELECT COUNT(*)
            FROM copy_trading_orders
            WHERE side = 'BUY' AND leader_tx_hash LIKE '0x%%' AND LENGTH(leader_tx_hash) > 20
              AND size_matched > 0
              AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
        """, (days,))
        leader_filled_count = cursor.fetchone()[0]

        # sweep 确认数（sweep BUY 之后同 asset 没有 SWEEP_TIMEOUT_EXIT）
        cursor.execute("""
            SELECT COUNT(DISTINCT o.asset_id)
            FROM copy_trading_orders o
            WHERE o.leader_tx_hash = 'WEATHER_SWEEP' AND o.side = 'BUY' AND o.status != 'ERROR'
              AND o.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
              AND NOT EXISTS (
                SELECT 1 FROM copy_trading_orders e
                WHERE e.asset_id = o.asset_id AND e.config_id = o.config_id
                  AND e.leader_tx_hash = 'SWEEP_TIMEOUT_EXIT' AND e.side = 'SELL'
                  AND e.created_at >= o.created_at
                  AND e.created_at <= DATE_ADD(o.created_at, INTERVAL 10 SECOND)
              )
        """, (days,))
        sweep_confirmed_count = cursor.fetchone()[0]

        # sweep 超时退出数
        cursor.execute("""
            SELECT COUNT(*)
            FROM copy_trading_orders
            WHERE leader_tx_hash = 'SWEEP_TIMEOUT_EXIT' AND side = 'SELL'
              AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
        """, (days,))
        sweep_timeout_count = cursor.fetchone()[0]

        # 平均入场延迟
        cursor.execute("""
            SELECT AVG(signal_latency_ms)
            FROM copy_trading_orders
            WHERE side = 'BUY' AND signal_latency_ms IS NOT NULL AND signal_latency_ms > 0
              AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
        """, (days,))
        avg_latency = cursor.fetchone()[0]

        # BUY 整体成交率
        cursor.execute("""
            SELECT COALESCE(SUM(size_matched), 0), COALESCE(SUM(follow_size), 0)
            FROM copy_trading_orders
            WHERE side = 'BUY' AND status != 'ERROR'
              AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
        """, (days,))
        row = cursor.fetchone()
        total_matched = float(row[0])
        total_ordered = float(row[1])

        # sweep→leader 确认延迟分布
        cursor.execute("""
            SELECT sweep_to_leader_ms
            FROM copy_trading_orders
            WHERE leader_tx_hash = 'WEATHER_SWEEP' AND side = 'BUY'
              AND sweep_to_leader_ms IS NOT NULL AND sweep_to_leader_ms > 0
              AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
            ORDER BY sweep_to_leader_ms
        """, (days,))
        latency_rows = [int(r[0]) for r in cursor.fetchall()]
        sweep_to_leader = None
        if latency_rows:
            n = len(latency_rows)
            sweep_to_leader = {
                "count": n,
                "avg_ms": round(sum(latency_rows) / n),
                "min_ms": latency_rows[0],
                "max_ms": latency_rows[-1],
                "p50_ms": latency_rows[max(0, n // 2 - 1)],
                "p90_ms": latency_rows[max(0, int(n * 0.9) - 1)],
            }

        return {
            "days": days,
            "signal_quality": {
                "sweep_signal_count": sweep_signal_count,
                "sweep_entry_count": sweep_entry_count,
                "sweep_confirmed_count": sweep_confirmed_count,
                "sweep_timeout_count": sweep_timeout_count,
                "confirmation_rate": round(sweep_confirmed_count / max(1, sweep_entry_count), 4),
                "sweep_to_leader": sweep_to_leader,
            },
            "execution": {
                "leader_signal_count": leader_signal_count,
                "leader_filled_count": leader_filled_count,
                "leader_fill_rate": round(leader_filled_count / max(1, leader_signal_count), 4),
                "buy_fill_rate": round(total_matched / max(1, total_ordered), 4),
                "avg_latency_ms": round(float(avg_latency), 1) if avg_latency else None,
                "sweep_filled_total": round(sweep_filled_total, 2),
            },
        }
    finally:
        conn.close()



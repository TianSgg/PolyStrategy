"""PnL 模块数据库操作"""
import logging
from typing import List
from datetime import datetime

from shared.db import get_db_connection
from shared.time_utils import now_utc8_dt

logger = logging.getLogger(__name__)


def batch_insert_balance_history(records: List[dict]):
    """批量插入余额快照 records: [{proxy_wallet, total_value, created_at}]"""
    if not records:
        return
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.executemany(
            "INSERT INTO copy_trading_account_balance_history (proxy_wallet, total_value, created_at) VALUES (%s, %s, %s)",
            [(r["proxy_wallet"], r["total_value"], r["created_at"]) for r in records]
        )
        conn.commit()
    finally:
        conn.close()


def get_all_adjustments() -> List[dict]:
    """获取所有余额调整记录"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT id, proxy_wallet, delta, applied_at, note, created_at
            FROM copy_trading_balance_adjustments
            ORDER BY applied_at ASC
        """)
        return [
            {
                "id": row[0],
                "proxy_wallet": row[1],
                "delta": float(row[2]),
                "applied_at": row[3].isoformat(),
                "note": row[4] or "",
                "created_at": row[5].isoformat(),
            }
            for row in cursor.fetchall()
        ]
    finally:
        conn.close()


def create_adjustment(proxy_wallet: str, delta: float, applied_at: datetime, note: str = "") -> int:
    """创建余额调整记录，返回 id"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO copy_trading_balance_adjustments (proxy_wallet, delta, applied_at, note) VALUES (%s, %s, %s, %s)",
            (proxy_wallet, delta, applied_at, note)
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def update_adjustment(adj_id: int, delta: float, note: str) -> bool:
    """更新余额调整记录"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE copy_trading_balance_adjustments SET delta = %s, note = %s WHERE id = %s",
            (delta, note, adj_id)
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def delete_adjustment(adj_id: int) -> bool:
    """删除余额调整记录"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM copy_trading_balance_adjustments WHERE id = %s", (adj_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def delete_balance_record(proxy_wallet: str, created_at: datetime) -> int:
    """删除指定 wallet + 时间点的余额记录，返回删除行数"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "DELETE FROM copy_trading_account_balance_history WHERE proxy_wallet = %s AND created_at = %s",
            (proxy_wallet, created_at)
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def get_balance_history(since: datetime, proxy_wallets: List[str] = None) -> List[dict]:
    """获取指定时间之后的余额历史"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if proxy_wallets:
            placeholders = ",".join(["%s"] * len(proxy_wallets))
            cursor.execute(f"""
                SELECT proxy_wallet, total_value, created_at
                FROM copy_trading_account_balance_history
                WHERE created_at >= %s AND proxy_wallet IN ({placeholders})
                ORDER BY created_at ASC
            """, [since] + proxy_wallets)
        else:
            cursor.execute("""
                SELECT proxy_wallet, total_value, created_at
                FROM copy_trading_account_balance_history
                WHERE created_at >= %s
                ORDER BY created_at ASC
            """, (since,))
        return [
            {"proxy_wallet": row[0], "total_value": float(row[1]), "created_at": row[2].isoformat()}
            for row in cursor.fetchall()
        ]
    finally:
        conn.close()

"""Idempotent auth schema bootstrap and ownership migration."""
import logging
import os
import secrets
from typing import Optional

from shared.db import get_db_connection
from shared.time_utils import UTC8_DB_NOW_SQL
from .service import hash_password

logger = logging.getLogger(__name__)


def _column_exists(cursor, table: str, column: str) -> bool:
    cursor.execute(
        """SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s""",
        (table, column),
    )
    return cursor.fetchone()[0] > 0


def _add_owner_column(cursor, table: str):
    if not _column_exists(cursor, table, "owner_user_id"):
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN owner_user_id INT NULL")
    index_name = f"idx_{table}_owner_user_id"
    cursor.execute(
        """SELECT COUNT(*) FROM information_schema.STATISTICS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = %s""",
        (table, index_name),
    )
    if cursor.fetchone()[0] == 0:
        cursor.execute(f"CREATE INDEX {index_name} ON {table} (owner_user_id)")


def _ensure_leader_owner_unique(cursor):
    cursor.execute(
        """SELECT COUNT(*) FROM information_schema.STATISTICS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'leaders' AND INDEX_NAME = 'idx_proxy_wallet'"""
    )
    if cursor.fetchone()[0] > 0:
        cursor.execute("DROP INDEX idx_proxy_wallet ON leaders")
    cursor.execute(
        """SELECT COUNT(*) FROM information_schema.STATISTICS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'leaders' AND INDEX_NAME = 'idx_leaders_owner_proxy'"""
    )
    if cursor.fetchone()[0] == 0:
        cursor.execute("CREATE UNIQUE INDEX idx_leaders_owner_proxy ON leaders (owner_user_id, proxy_wallet)")


def _ensure_global_unique_index(cursor, table: str, index_name: str, columns: str):
    cursor.execute(
        """SELECT COUNT(*) FROM information_schema.STATISTICS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = %s""",
        (table, index_name),
    )
    if cursor.fetchone()[0] == 0:
        cursor.execute(f"CREATE UNIQUE INDEX {index_name} ON {table} ({columns})")


def _get_first_root_id(cursor) -> Optional[int]:
    cursor.execute("SELECT id FROM users WHERE role = 'root' ORDER BY id ASC LIMIT 1")
    row = cursor.fetchone()
    if row:
        return int(row[0])
    cursor.execute("SELECT id FROM users WHERE role = 'admin' ORDER BY id ASC LIMIT 1")
    row = cursor.fetchone()
    if row:
        admin_id = int(row[0])
        cursor.execute("UPDATE users SET role = 'root' WHERE id = %s", (admin_id,))
        logger.info("[Auth] Upgraded first admin (id=%s) to root", admin_id)
        return admin_id
    return None


def _ensure_root(cursor) -> int:
    root_id = _get_first_root_id(cursor)
    if root_id:
        return root_id

    username = os.getenv("ADMIN_USERNAME", "admin")
    password = os.getenv("ADMIN_PASSWORD")
    if not password:
        password = secrets.token_urlsafe(18)
        logger.warning("[Auth] ADMIN_PASSWORD not set; generated one-time root password: %s", password)

    cursor.execute(
        """INSERT INTO users (username, password_hash, role, enabled)
           VALUES (%s, %s, 'root', 1)""",
        (username, hash_password(password)),
    )
    return cursor.lastrowid


def run_auth_migrations() -> int:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS users (
              id INT AUTO_INCREMENT PRIMARY KEY,
              username VARCHAR(128) NOT NULL,
              password_hash VARCHAR(255) NOT NULL,
              role VARCHAR(16) NOT NULL DEFAULT 'user',
              enabled TINYINT(1) NOT NULL DEFAULT 1,
              created_at DATETIME(3) DEFAULT ({UTC8_DB_NOW_SQL}),
              updated_at DATETIME(3) DEFAULT ({UTC8_DB_NOW_SQL}),
              UNIQUE KEY idx_username (username)
            )
        """)
        root_id = _ensure_root(cursor)
        for table in ("accounts", "leaders", "copy_trading_configs"):
            _add_owner_column(cursor, table)
            cursor.execute(f"UPDATE {table} SET owner_user_id = %s WHERE owner_user_id IS NULL", (root_id,))
        _ensure_leader_owner_unique(cursor)
        _ensure_global_unique_index(cursor, "accounts", "idx_wallet_address", "wallet_address")
        _ensure_global_unique_index(
            cursor,
            "copy_trading_configs",
            "idx_leader_follower",
            "leader_proxy_wallet, follower_proxy_wallet",
        )
        conn.commit()
        logger.info("[Auth] Auth migrations complete; bootstrap root id=%s", root_id)
        return root_id
    finally:
        conn.close()

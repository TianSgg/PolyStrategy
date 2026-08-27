"""Auth service database operations."""
import logging
import os
import secrets
from typing import List, Optional

from framework.db import get_db_connection
from framework.time_utils import UTC8_DB_NOW_SQL, format_utc8
from auth_service.types import UserRecord

logger = logging.getLogger(__name__)


class AuthDao:
    @staticmethod
    def get_user_by_id(user_id: int) -> Optional[UserRecord]:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """SELECT id, username, role, enabled, created_at, updated_at
                   FROM users WHERE id = %s""",
                (user_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return UserRecord(
                id=row[0],
                username=row[1],
                role=row[2],
                enabled=bool(row[3]),
                created_at=format_utc8(row[4]),
                updated_at=format_utc8(row[5]),
            )
        finally:
            conn.close()

    @staticmethod
    def get_user_full_by_username(username: str) -> Optional[dict]:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """SELECT id, username, password_hash, role, enabled, created_at, updated_at
                   FROM users WHERE username = %s""",
                (username.strip(),),
            )
            row = cursor.fetchone()
            if not row:
                return None
            columns = [col[0] for col in cursor.description]
            return dict(zip(columns, row))
        finally:
            conn.close()

    @staticmethod
    def get_user_by_username(username: str) -> Optional[UserRecord]:
        full = AuthDao.get_user_full_by_username(username)
        if not full:
            return None
        return UserRecord(
            id=full["id"],
            username=full["username"],
            role=full["role"],
            enabled=bool(full["enabled"]),
            created_at=format_utc8(full.get("created_at")),
            updated_at=format_utc8(full.get("updated_at")),
        )

    @staticmethod
    def list_users() -> List[dict]:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """SELECT id, username, role, enabled, created_at, updated_at
                   FROM users ORDER BY id ASC"""
            )
            columns = [col[0] for col in cursor.description]
            users = [dict(zip(columns, row)) for row in cursor.fetchall()]
            for user in users:
                user["created_at"] = format_utc8(user.get("created_at"))
                user["updated_at"] = format_utc8(user.get("updated_at"))
            return users
        finally:
            conn.close()

    @staticmethod
    def insert_user(username: str, password_hash: str, role: str) -> int:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """INSERT INTO users (username, password_hash, role, enabled)
                   VALUES (%s, %s, %s, 1)""",
                (username, password_hash, role),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    @staticmethod
    def set_user_enabled(user_id: int, enabled: bool) -> bool:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"""UPDATE users SET enabled = %s, updated_at = {UTC8_DB_NOW_SQL}
                   WHERE id = %s""",
                (1 if enabled else 0, user_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    @staticmethod
    def update_password(user_id: int, password_hash: str) -> bool:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"""UPDATE users SET password_hash = %s, updated_at = {UTC8_DB_NOW_SQL}
                   WHERE id = %s""",
                (password_hash, user_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    @staticmethod
    def update_username(user_id: int, username: str) -> bool:
        import pymysql.err
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"""UPDATE users SET username = %s, updated_at = {UTC8_DB_NOW_SQL}
                   WHERE id = %s""",
                (username, user_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        except pymysql.err.IntegrityError:
            raise ValueError("用户名已存在")
        finally:
            conn.close()

    @staticmethod
    def delete_user_and_transfer(user_id: int, to_user_id: int) -> bool:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            for table in ("accounts",):
                cursor.execute(
                    f"UPDATE {table} SET owner_user_id = %s WHERE owner_user_id = %s",
                    (to_user_id, user_id),
                )
            cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
            deleted = cursor.rowcount > 0
            conn.commit()
            return deleted
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def is_owned_by(proxy_wallet: str, owner_user_id: int) -> bool:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT COUNT(*) FROM accounts WHERE proxy_wallet = %s AND owner_user_id = %s",
                (proxy_wallet, owner_user_id),
            )
            return cursor.fetchone()[0] > 0
        finally:
            conn.close()


# ==================== Migrations ====================

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



def _ensure_global_unique_index(cursor, table: str, index_name: str, columns: str):
    cursor.execute(
        """SELECT COUNT(*) FROM information_schema.STATISTICS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = %s""",
        (table, index_name),
    )
    if cursor.fetchone()[0] == 0:
        cursor.execute(f"CREATE UNIQUE INDEX {index_name} ON {table} ({columns})")


def _get_first_root_id(cursor) -> Optional[int]:
    # Upgrade all admins to root (admin role removed)
    cursor.execute("UPDATE users SET role = 'root' WHERE role = 'admin'")
    if cursor.rowcount > 0:
        logger.info("[Auth] Upgraded %d admin(s) to root", cursor.rowcount)

    cursor.execute("SELECT id FROM users WHERE role = 'root' ORDER BY id ASC LIMIT 1")
    row = cursor.fetchone()
    if row:
        return int(row[0])
    return None


def _ensure_root(cursor, hash_fn) -> int:
    root_id = _get_first_root_id(cursor)
    if root_id:
        return root_id

    username = os.getenv("ROOT_USERNAME", "root")
    password = os.getenv("ROOT_PASSWORD")
    if not password:
        password = secrets.token_urlsafe(18)
        logger.warning("[Auth] ROOT_PASSWORD not set; generated one-time root password: %s", password)

    cursor.execute(
        """INSERT INTO users (username, password_hash, role, enabled)
           VALUES (%s, %s, 'root', 1)""",
        (username, hash_fn(password)),
    )
    return cursor.lastrowid


def run_auth_migrations(hash_fn) -> int:
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
        root_id = _ensure_root(cursor, hash_fn)
        _add_owner_column(cursor, "accounts")
        cursor.execute("UPDATE accounts SET owner_user_id = %s WHERE owner_user_id IS NULL", (root_id,))
        _ensure_global_unique_index(cursor, "accounts", "idx_wallet_address", "wallet_address")
        conn.commit()
        logger.info("[Auth] Auth migrations complete; bootstrap root id=%s", root_id)
        return root_id
    finally:
        conn.close()

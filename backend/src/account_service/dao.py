"""Account 数据访问层（类似 Spring DAO 模式）"""
from typing import List, Optional

from framework.db import get_db_connection
from framework.time_utils import UTC8_DB_NOW_SQL, format_utc8


class AccountDao:
    """accounts 表的 CRUD 操作"""

    @staticmethod
    def get_all(owner_user_id: Optional[int] = None, owner_user_ids: Optional[List[int]] = None) -> List[dict]:
        """返回账户列表。owner_user_ids 优先于 owner_user_id；都为 None 时返回全部"""
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            sql = """SELECT id, owner_user_id, name, wallet_address, proxy_wallet,
                            builder_api_key, builder_code, signature_type,
                            relayer_api_key, created_at
                     FROM accounts WHERE deleted_at IS NULL"""
            params: tuple = ()
            if owner_user_ids is not None:
                placeholders = ",".join(["%s"] * len(owner_user_ids))
                sql += f" AND owner_user_id IN ({placeholders})"
                params = tuple(owner_user_ids)
            elif owner_user_id is not None:
                sql += " AND owner_user_id = %s"
                params = (owner_user_id,)
            cursor.execute(sql, params)
            columns = [col[0] for col in cursor.description]
            accounts = [dict(zip(columns, row)) for row in cursor.fetchall()]
            for account in accounts:
                account["created_at"] = format_utc8(account.get("created_at"))
            return accounts
        finally:
            conn.close()

    @staticmethod
    def get_by_id(account_id: int) -> Optional[dict]:
        """根据 ID 查单个账户（含加密字段）"""
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """SELECT id, owner_user_id, name, wallet_address, proxy_wallet,
                          encrypted_private_key, builder_api_key,
                          encrypted_builder_secret, encrypted_builder_passphrase,
                          builder_code, signature_type, relayer_api_key
                   FROM accounts WHERE id = %s AND deleted_at IS NULL""",
                (account_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            columns = [col[0] for col in cursor.description]
            return dict(zip(columns, row))
        finally:
            conn.close()

    @staticmethod
    def insert(
        name: str,
        wallet_address: str,
        proxy_wallet: str,
        encrypted_private_key: str,
        builder_api_key: str,
        encrypted_builder_secret: str,
        encrypted_builder_passphrase: str,
        builder_code: Optional[str] = None,
        owner_user_id: Optional[int] = None,
        signature_type: int = 2,
    ) -> int:
        """插入新账户，返回新账户 ID"""
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """INSERT INTO accounts
                   (owner_user_id, name, wallet_address, proxy_wallet, encrypted_private_key,
                    builder_api_key, encrypted_builder_secret, encrypted_builder_passphrase,
                    builder_code, signature_type)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (owner_user_id, name, wallet_address, proxy_wallet, encrypted_private_key,
                 builder_api_key, encrypted_builder_secret, encrypted_builder_passphrase,
                 builder_code, signature_type)
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    @staticmethod
    def update_builder_code(account_id: int, builder_code: str) -> bool:
        """更新账户的 builder_code，返回是否影响行"""
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"""UPDATE accounts
                   SET builder_code = %s, updated_at = {UTC8_DB_NOW_SQL}
                   WHERE id = %s""",
                (builder_code, account_id)
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    @staticmethod
    def update_relayer_api_key(account_id: int, relayer_api_key: str) -> bool:
        """更新账户的 relayer_api_key"""
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"""UPDATE accounts
                   SET relayer_api_key = %s, updated_at = {UTC8_DB_NOW_SQL}
                   WHERE id = %s""",
                (relayer_api_key, account_id)
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    @staticmethod
    def update_name(proxy_wallet: str, name: str) -> bool:
        """更新账户名"""
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"UPDATE accounts SET name = %s, updated_at = {UTC8_DB_NOW_SQL} WHERE proxy_wallet = %s",
                (name, proxy_wallet)
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    @staticmethod
    def get_by_proxy_wallet(proxy_wallet: str) -> Optional[dict]:
        """根据 proxy_wallet 查单个账户（含加密字段）"""
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """SELECT id, owner_user_id, name, wallet_address, proxy_wallet,
                          encrypted_private_key, builder_api_key,
                          encrypted_builder_secret, encrypted_builder_passphrase,
                          builder_code, signature_type, relayer_api_key
                   FROM accounts WHERE proxy_wallet = %s AND deleted_at IS NULL""",
                (proxy_wallet.lower(),)
            )
            row = cursor.fetchone()
            if not row:
                return None
            columns = [col[0] for col in cursor.description]
            return dict(zip(columns, row))
        finally:
            conn.close()

    @staticmethod
    def is_owned_by(proxy_wallet: str, owner_user_id: int) -> bool:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT COUNT(*) FROM accounts WHERE proxy_wallet = %s AND owner_user_id = %s AND deleted_at IS NULL",
                (proxy_wallet.lower(), owner_user_id),
            )
            return cursor.fetchone()[0] > 0
        finally:
            conn.close()

    @staticmethod
    def delete(proxy_wallet: str) -> bool:
        """软删除账户（保留交易记录可查）"""
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"UPDATE accounts SET deleted_at = {UTC8_DB_NOW_SQL} WHERE proxy_wallet = %s AND deleted_at IS NULL",
                (proxy_wallet.lower(),)
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

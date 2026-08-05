"""Portfolio Group 数据访问层"""
from typing import List, Optional

from shared.db import get_db_connection
from shared.time_utils import UTC8_DB_NOW_SQL, format_utc8


class PortfolioGroupDao:

    @staticmethod
    def get_all(owner_user_id: int) -> List[dict]:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT id, name, owner_user_id, created_at FROM copy_trading_portfolio_groups WHERE owner_user_id = %s ORDER BY id",
                (owner_user_id,),
            )
            columns = [col[0] for col in cursor.description]
            groups = [dict(zip(columns, row)) for row in cursor.fetchall()]
            if not groups:
                return groups
            for g in groups:
                g["created_at"] = format_utc8(g.get("created_at"))
            group_ids = [g["id"] for g in groups]
            placeholders = ",".join(["%s"] * len(group_ids))
            cursor.execute(
                f"SELECT group_id, account_id FROM copy_trading_portfolio_group_accounts WHERE group_id IN ({placeholders})",
                group_ids,
            )
            account_map: dict = {gid: [] for gid in group_ids}
            for row in cursor.fetchall():
                account_map[row[0]].append(row[1])
            for g in groups:
                g["account_ids"] = account_map[g["id"]]
            return groups
        finally:
            conn.close()

    @staticmethod
    def get_by_id(group_id: int) -> Optional[dict]:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT id, name, owner_user_id, created_at FROM copy_trading_portfolio_groups WHERE id = %s",
                (group_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            columns = [col[0] for col in cursor.description]
            return dict(zip(columns, row))
        finally:
            conn.close()

    @staticmethod
    def insert(name: str, owner_user_id: int) -> int:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO copy_trading_portfolio_groups (name, owner_user_id) VALUES (%s, %s)",
                (name, owner_user_id),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    @staticmethod
    def update(group_id: int, name: str) -> bool:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"UPDATE copy_trading_portfolio_groups SET name = %s, updated_at = {UTC8_DB_NOW_SQL} WHERE id = %s",
                (name, group_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    @staticmethod
    def delete(group_id: int) -> bool:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("DELETE FROM copy_trading_portfolio_group_accounts WHERE group_id = %s", (group_id,))
            cursor.execute("DELETE FROM copy_trading_portfolio_groups WHERE id = %s", (group_id,))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    @staticmethod
    def get_account_ids(group_id: int) -> List[int]:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT account_id FROM copy_trading_portfolio_group_accounts WHERE group_id = %s",
                (group_id,),
            )
            return [row[0] for row in cursor.fetchall()]
        finally:
            conn.close()

    @staticmethod
    def set_account_ids(group_id: int, account_ids: List[int]) -> None:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("DELETE FROM copy_trading_portfolio_group_accounts WHERE group_id = %s", (group_id,))
            if account_ids:
                values = [(group_id, aid) for aid in account_ids]
                cursor.executemany(
                    "INSERT INTO copy_trading_portfolio_group_accounts (group_id, account_id) VALUES (%s, %s)",
                    values,
                )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def get_groups_for_account(account_id: int) -> List[int]:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT group_id FROM copy_trading_portfolio_group_accounts WHERE account_id = %s",
                (account_id,),
            )
            return [row[0] for row in cursor.fetchall()]
        finally:
            conn.close()

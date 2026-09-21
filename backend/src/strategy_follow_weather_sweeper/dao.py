"""策略配置 + 事件记录 DAO。"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from framework.db import get_db

logger = logging.getLogger(__name__)


class FollowWeatherSweeperConfigDAO:
    """strategy_follow_weather_sweeper_configs CRUD。"""

    TABLE = "strategy_follow_weather_sweeper_configs"

    def list_all_enabled(self) -> List[Dict[str, Any]]:
        sql = f"""
            SELECT c.id, c.owner_user_id, c.account_id, c.name, c.params_version,
                   c.fixed_entry_shares, c.entry_wait_ms,
                   c.stop_loss_ratio, c.exit_wait_ms,
                   c.leader_wallets,
                   a.proxy_wallet
            FROM {self.TABLE} c
            JOIN accounts a ON a.id = c.account_id
            WHERE c.enabled = 1 AND c.deleted_at IS NULL
            ORDER BY c.id
        """
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                columns = [desc[0] for desc in cur.description]
                rows = cur.fetchall()

        result = []
        for row in rows:
            d = dict(zip(columns, row))
            if isinstance(d.get("leader_wallets"), str):
                d["leader_wallets"] = json.loads(d["leader_wallets"])
            d["params"] = {
                "fixed_entry_shares": str(d["fixed_entry_shares"]),
                "entry_wait_ms": int(d["entry_wait_ms"]),
                "stop_loss_ratio": str(d["stop_loss_ratio"]),
                "exit_wait_ms": int(d["exit_wait_ms"]),
            }
            result.append(d)
        return result

    def list_by_owner(self, owner_user_id: int) -> List[Dict[str, Any]]:
        sql = f"""
            SELECT c.*, a.proxy_wallet, a.name AS account_name
            FROM {self.TABLE} c
            JOIN accounts a ON a.id = c.account_id
            WHERE c.owner_user_id = %s AND c.deleted_at IS NULL
            ORDER BY c.id DESC
        """
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (owner_user_id,))
                columns = [desc[0] for desc in cur.description]
                return [self._format_config_row(dict(zip(columns, row))) for row in cur.fetchall()]

    def list_all(self, owner_user_ids: Optional[List[int]] = None) -> List[Dict[str, Any]]:
        sql = f"""
            SELECT c.*, a.proxy_wallet, a.name AS account_name
            FROM {self.TABLE} c
            JOIN accounts a ON a.id = c.account_id
            WHERE c.deleted_at IS NULL
        """
        params: list = []
        if owner_user_ids is not None:
            placeholders = ",".join(["%s"] * len(owner_user_ids))
            sql += f" AND c.owner_user_id IN ({placeholders})"
            params = list(owner_user_ids)
        sql += " ORDER BY c.id DESC"
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                columns = [desc[0] for desc in cur.description]
                return [self._format_config_row(dict(zip(columns, row))) for row in cur.fetchall()]

    def get_by_id(self, config_id: int) -> Optional[Dict[str, Any]]:
        sql = f"""
            SELECT c.*, a.proxy_wallet, a.name AS account_name
            FROM {self.TABLE} c
            JOIN accounts a ON a.id = c.account_id
            WHERE c.id = %s AND c.deleted_at IS NULL
        """
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (config_id,))
                columns = [desc[0] for desc in cur.description]
                row = cur.fetchone()
                return self._format_config_row(dict(zip(columns, row))) if row else None

    def create(self, data: Dict[str, Any]) -> int:
        leader_wallets = data.get("leader_wallets", [])
        if isinstance(leader_wallets, list):
            leader_wallets = json.dumps(leader_wallets)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT id FROM {self.TABLE} WHERE owner_user_id = %s AND name = %s AND deleted_at IS NULL",
                    (data["owner_user_id"], data["name"]),
                )
                if cur.fetchone():
                    raise ValueError(f"同名配置已存在: {data['name']}")
                sql = f"""
                    INSERT INTO {self.TABLE} (
                        owner_user_id, account_id, name, enabled,
                        fixed_entry_shares, entry_wait_ms,
                        stop_loss_ratio, exit_wait_ms,
                        leader_wallets,
                        created_at, updated_at
                    ) VALUES (
                        %(owner_user_id)s, %(account_id)s, %(name)s, %(enabled)s,
                        %(fixed_entry_shares)s, %(entry_wait_ms)s,
                        %(stop_loss_ratio)s, %(exit_wait_ms)s,
                        %(leader_wallets)s,
                        NOW(3), NOW(3)
                    )
                """
                params = dict(data)
                params["leader_wallets"] = leader_wallets
                cur.execute(sql, params)
            conn.commit()
            return cur.lastrowid

    def update(self, config_id: int, data: Dict[str, Any]) -> bool:
        sets = []
        params = []
        for key in (
            "name", "enabled", "fixed_entry_shares", "entry_wait_ms",
            "stop_loss_ratio", "exit_wait_ms", "leader_wallets",
        ):
            if key in data:
                sets.append(f"{key} = %s")
                val = data[key]
                if key == "leader_wallets" and isinstance(val, list):
                    val = json.dumps(val)
                params.append(val)
        if not sets:
            return False
        sets.append("params_version = params_version + 1")
        sets.append("updated_at = NOW(3)")
        params.append(config_id)
        sql = f"UPDATE {self.TABLE} SET {', '.join(sets)} WHERE id = %s AND deleted_at IS NULL"
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()
            return cur.rowcount > 0

    def soft_delete(self, config_id: int) -> bool:
        sql = f"UPDATE {self.TABLE} SET deleted_at = NOW(3), enabled = 0 WHERE id = %s AND deleted_at IS NULL"
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (config_id,))
            conn.commit()
            return cur.rowcount > 0

    @staticmethod
    def _format_config_row(d: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(d.get("leader_wallets"), str):
            d["leader_wallets"] = json.loads(d["leader_wallets"])
        return d


class FollowWeatherSweeperEventDAO:
    """strategy_follow_weather_sweeper_events 查询。"""

    TABLE = "strategy_follow_weather_sweeper_events"
    TRADES_TABLE = "strategy_follow_weather_sweeper_trades"

    def list_events(
        self,
        *,
        owner_user_id: Optional[int] = None,
        config_id: Optional[int] = None,
        proxy_wallet: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        conditions: List[str] = []
        params: List[Any] = []

        if owner_user_id is not None:
            conditions.append("t.owner_user_id = %s")
            params.append(owner_user_id)
        if config_id is not None:
            conditions.append("t.config_id = %s")
            params.append(config_id)
        if proxy_wallet:
            conditions.append("t.proxy_wallet = %s")
            params.append(proxy_wallet.lower())

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"""
            SELECT e.*, t.config_id, t.owner_user_id, t.proxy_wallet,
                   t.signal_id, t.token_id, t.market_slug, t.event_slug,
                   t.city, t.direction
            FROM {self.TABLE} e
            JOIN {self.TRADES_TABLE} t ON t.event_id = e.event_id
            WHERE {where}
            ORDER BY e.occurred_at DESC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                columns = [desc[0] for desc in cur.description]
                rows = cur.fetchall()
                return [self._format_row(dict(zip(columns, row))) for row in rows]

    def list_by_event_id(self, event_id: str) -> List[Dict[str, Any]]:
        sql = f"""
            SELECT * FROM {self.TABLE}
            WHERE event_id = %s
            ORDER BY sequence_no ASC
        """
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (event_id,))
                columns = [desc[0] for desc in cur.description]
                rows = cur.fetchall()
                return [self._format_row(dict(zip(columns, row))) for row in rows]

    def list_event_summaries(
        self,
        *,
        owner_user_id: Optional[int] = None,
        config_id: Optional[int] = None,
        owner_user_ids: Optional[List[int]] = None,
        search: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        conditions: List[str] = []
        params: List[Any] = []

        if owner_user_ids is not None:
            placeholders = ",".join(["%s"] * len(owner_user_ids))
            conditions.append(f"t.owner_user_id IN ({placeholders})")
            params.extend(owner_user_ids)
        elif owner_user_id is not None:
            conditions.append("t.owner_user_id = %s")
            params.append(owner_user_id)
        if config_id is not None:
            conditions.append("t.config_id = %s")
            params.append(config_id)
        if search:
            conditions.append("t.event_slug LIKE %s")
            params.append(f"%{search}%")

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"""
            SELECT t.*,
                   MIN(e.occurred_at) AS event_started_at,
                   MAX(e.occurred_at) AS event_ended_at,
                   COUNT(e.id) AS step_count,
                   (SELECT e2.phase FROM {self.TABLE} e2
                    WHERE e2.event_id = t.event_id
                    ORDER BY e2.sequence_no DESC LIMIT 1) AS final_event_phase
            FROM {self.TABLE} e
            JOIN {self.TRADES_TABLE} t ON t.event_id = e.event_id
            WHERE {where}
            GROUP BY t.id
            ORDER BY t.started_at DESC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                columns = [desc[0] for desc in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]

    @staticmethod
    def _format_row(d: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(d.get("detail"), str):
            d["detail"] = json.loads(d["detail"])
        if isinstance(d.get("config_snapshot"), str):
            d["config_snapshot"] = json.loads(d["config_snapshot"])
        return d


class FollowWeatherSweeperTradeDAO:
    """strategy_follow_weather_sweeper_trades 读写。"""

    TABLE = "strategy_follow_weather_sweeper_trades"

    def insert(self, data: Dict[str, Any]) -> int:
        columns = list(data.keys())
        placeholders = ", ".join(["%s"] * len(columns))
        col_str = ", ".join(columns)
        sql = f"INSERT INTO {self.TABLE} ({col_str}) VALUES ({placeholders})"
        params = [data[c] for c in columns]
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()
            return cur.lastrowid

    def update_by_event_id(self, event_id: str, data: Dict[str, Any]) -> bool:
        if not data:
            return False
        sets = [f"{k} = %s" for k in data.keys()]
        params = list(data.values())
        params.append(event_id)
        sql = f"UPDATE {self.TABLE} SET {', '.join(sets)} WHERE event_id = %s"
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()
            return cur.rowcount > 0

    def list_trades(
        self,
        *,
        owner_user_ids: Optional[List[int]] = None,
        phase: Optional[str] = None,
        close_reason: Optional[str] = None,
        search: Optional[str] = None,
        proxy_wallet: Optional[str] = None,
        direction: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 30,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        conditions: List[str] = []
        params: List[Any] = []

        if owner_user_ids is not None:
            placeholders = ",".join(["%s"] * len(owner_user_ids))
            conditions.append(f"owner_user_id IN ({placeholders})")
            params.extend(owner_user_ids)
        if proxy_wallet:
            conditions.append("proxy_wallet = %s")
            params.append(proxy_wallet.lower())
        if phase:
            conditions.append("phase = %s")
            params.append(phase)
        if close_reason:
            conditions.append("close_reason = %s")
            params.append(close_reason)
        if direction:
            conditions.append("direction = %s")
            params.append(direction)
        if search:
            conditions.append("event_slug LIKE %s")
            params.append(f"%{search}%")
        if since:
            conditions.append("started_at >= %s")
            params.append(since)

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"""
            SELECT * FROM {self.TABLE}
            WHERE {where}
            ORDER BY started_at DESC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                columns = [desc[0] for desc in cur.description]
                return [self._format_trade_row(dict(zip(columns, row))) for row in cur.fetchall()]

    def count_trades(
        self,
        *,
        owner_user_ids: Optional[List[int]] = None,
        phase: Optional[str] = None,
        close_reason: Optional[str] = None,
        search: Optional[str] = None,
        proxy_wallet: Optional[str] = None,
        direction: Optional[str] = None,
        since: Optional[str] = None,
    ) -> int:
        conditions: List[str] = []
        params: List[Any] = []

        if owner_user_ids is not None:
            placeholders = ",".join(["%s"] * len(owner_user_ids))
            conditions.append(f"owner_user_id IN ({placeholders})")
            params.extend(owner_user_ids)
        if proxy_wallet:
            conditions.append("proxy_wallet = %s")
            params.append(proxy_wallet.lower())
        if phase:
            conditions.append("phase = %s")
            params.append(phase)
        if close_reason:
            conditions.append("close_reason = %s")
            params.append(close_reason)
        if direction:
            conditions.append("direction = %s")
            params.append(direction)
        if search:
            conditions.append("event_slug LIKE %s")
            params.append(f"%{search}%")
        if since:
            conditions.append("started_at >= %s")
            params.append(since)

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT COUNT(*) FROM {self.TABLE} WHERE {where}"
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchone()[0]

    def get_by_event_id(self, event_id: str) -> Optional[Dict[str, Any]]:
        sql = f"SELECT * FROM {self.TABLE} WHERE event_id = %s"
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (event_id,))
                columns = [desc[0] for desc in cur.description]
                row = cur.fetchone()
                return self._format_trade_row(dict(zip(columns, row))) if row else None

    @staticmethod
    def _format_trade_row(d: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(d.get("config_snapshot"), str):
            d["config_snapshot"] = json.loads(d["config_snapshot"])
        return d

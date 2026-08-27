"""策略配置 + 事件记录 DAO。"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from framework.db import get_db

logger = logging.getLogger(__name__)


class WeatherSweepConfigDAO:
    """strategy_weather_sweep_configs CRUD。"""

    TABLE = "strategy_weather_sweep_configs"

    def list_all_enabled(self) -> List[Dict[str, Any]]:
        """加载所有 enabled 的配置（含 proxy_wallet），供实例管理使用。"""
        sql = f"""
            SELECT c.id, c.owner_user_id, c.account_id, c.name, c.params_version,
                   c.fixed_entry_shares, c.entry_wait_ms, c.sweep_outcome_filter,
                   c.signal_source_filter, c.signal_threshold_filter,
                   c.stop_loss_ratio, c.exit_wait_ms, c.tick_verify_retries,
                   c.tick_verify_backoff_ms,
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
            d["params"] = {
                "fixed_entry_shares": str(d["fixed_entry_shares"]),
                "entry_wait_ms": int(d["entry_wait_ms"]),
                "sweep_outcome_filter": d.get("sweep_outcome_filter", "no"),
                "signal_source_filter": d.get("signal_source_filter", "all"),
                "signal_threshold_filter": d.get("signal_threshold_filter", "all"),
                "stop_loss_ratio": str(d["stop_loss_ratio"]),
                "exit_wait_ms": int(d["exit_wait_ms"]),
                "tick_verify_retries": int(d["tick_verify_retries"]),
                "tick_verify_backoff_ms": int(d["tick_verify_backoff_ms"]),
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
                return [dict(zip(columns, row)) for row in cur.fetchall()]

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
                return [dict(zip(columns, row)) for row in cur.fetchall()]

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
                return dict(zip(columns, row)) if row else None

    def create(self, data: Dict[str, Any]) -> int:
        sql = f"""
            INSERT INTO {self.TABLE} (
                owner_user_id, account_id, name, enabled,
                fixed_entry_shares, entry_wait_ms, sweep_outcome_filter,
                signal_source_filter, signal_threshold_filter,
                stop_loss_ratio, exit_wait_ms, tick_verify_retries, tick_verify_backoff_ms,
                created_at, updated_at
            ) VALUES (
                %(owner_user_id)s, %(account_id)s, %(name)s, %(enabled)s,
                %(fixed_entry_shares)s, %(entry_wait_ms)s, %(sweep_outcome_filter)s,
                %(signal_source_filter)s, %(signal_threshold_filter)s,
                %(stop_loss_ratio)s, %(exit_wait_ms)s, %(tick_verify_retries)s, %(tick_verify_backoff_ms)s,
                NOW(3), NOW(3)
            )
        """
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, data)
            conn.commit()
            return cur.lastrowid

    def update(self, config_id: int, data: Dict[str, Any]) -> bool:
        sets = []
        params = []
        for key in (
            "name", "enabled", "fixed_entry_shares", "entry_wait_ms",
            "sweep_outcome_filter", "signal_source_filter", "signal_threshold_filter",
            "stop_loss_ratio", "exit_wait_ms",
            "tick_verify_retries", "tick_verify_backoff_ms",
        ):
            if key in data:
                sets.append(f"{key} = %s")
                params.append(data[key])
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


class WeatherSweepEventDAO:
    """strategy_weather_sweep_events 查询。"""

    TABLE = "strategy_weather_sweep_events"

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
            conditions.append("owner_user_id = %s")
            params.append(owner_user_id)
        if config_id is not None:
            conditions.append("config_id = %s")
            params.append(config_id)
        if proxy_wallet:
            conditions.append("proxy_wallet = %s")
            params.append(proxy_wallet)

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"""
            SELECT * FROM {self.TABLE}
            WHERE {where}
            ORDER BY occurred_at DESC
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
        """返回每个 event 的摘要（首条 + 末条 step）。"""
        conditions: List[str] = []
        params: List[Any] = []

        if owner_user_ids is not None:
            placeholders = ",".join(["%s"] * len(owner_user_ids))
            conditions.append(f"owner_user_id IN ({placeholders})")
            params.extend(owner_user_ids)
        elif owner_user_id is not None:
            conditions.append("owner_user_id = %s")
            params.append(owner_user_id)
        if config_id is not None:
            conditions.append("config_id = %s")
            params.append(config_id)
        if search:
            conditions.append("event_slug LIKE %s")
            params.append(f"%{search}%")

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"""
            SELECT e.event_id, e.config_id, e.owner_user_id, e.proxy_wallet,
                   e.signal_id, e.token_id, e.market_slug, e.event_slug,
                   MIN(e.occurred_at) AS started_at,
                   MAX(e.occurred_at) AS ended_at,
                   COUNT(*) AS step_count,
                   (SELECT phase FROM {self.TABLE} t
                    WHERE t.event_id = e.event_id
                    ORDER BY t.sequence_no DESC LIMIT 1) AS final_phase
            FROM {self.TABLE} e
            WHERE {where}
            GROUP BY e.event_id, e.config_id, e.owner_user_id, e.proxy_wallet,
                     e.signal_id, e.token_id, e.market_slug, e.event_slug
            ORDER BY started_at DESC
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

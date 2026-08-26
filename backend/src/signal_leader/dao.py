"""Leader 活动信号持久化 DAO — leader_signals 表。"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from framework.db import get_db

logger = logging.getLogger(__name__)


class LeaderSignalRepository:
    """leader_signals 表读写。"""

    def save(self, event: Dict[str, Any]) -> bool:
        sql = """
            INSERT INTO leader_signals
                (signal_id, signal_type, token_id, outcome,
                 leader_proxy_wallet, leader_name,
                 order_size, order_price, market_slug,
                 occurred_at, received_at, received_monotonic_ns, extra_json)
            VALUES
                (%(signal_id)s, %(signal_type)s, %(token_id)s, %(outcome)s,
                 %(leader_proxy_wallet)s, %(leader_name)s,
                 %(order_size)s, %(order_price)s, %(market_slug)s,
                 %(occurred_at)s, %(received_at)s, %(received_monotonic_ns)s, %(extra_json)s)
            ON DUPLICATE KEY UPDATE signal_id = signal_id
        """
        data = {**event}
        if "id" in data and "signal_id" not in data:
            data["signal_id"] = data.pop("id")
        if "event_type" in data and "signal_type" not in data:
            data["signal_type"] = data.pop("event_type")
        data["extra_json"] = json.dumps(data.get("extra_json", {}), ensure_ascii=False)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, data)
                conn.commit()
                return cur.rowcount > 0

    def find_by_signal_id(self, signal_id: str) -> Optional[Dict[str, Any]]:
        sql = "SELECT * FROM leader_signals WHERE signal_id = %s"
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (signal_id,))
                row = cur.fetchone()
                return self._row_to_dict(cur, row) if row else None

    def query(
        self,
        *,
        token_id: Optional[str] = None,
        leader_proxy_wallet: Optional[str] = None,
        outcome: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        conditions: List[str] = []
        params: List[Any] = []

        if token_id:
            conditions.append("token_id = %s")
            params.append(token_id)
        if leader_proxy_wallet:
            conditions.append("leader_proxy_wallet = %s")
            params.append(leader_proxy_wallet)
        if outcome:
            conditions.append("outcome = %s")
            params.append(outcome)
        if start_time:
            conditions.append("received_at >= %s")
            params.append(start_time)
        if end_time:
            conditions.append("received_at <= %s")
            params.append(end_time)

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT * FROM leader_signals WHERE {where} ORDER BY received_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
                return [self._row_to_dict(cur, r) for r in rows]

    def _row_to_dict(self, cursor, row) -> Dict[str, Any]:
        columns = [desc[0] for desc in cursor.description]
        d = dict(zip(columns, row))
        if isinstance(d.get("extra_json"), str):
            d["extra_json"] = json.loads(d["extra_json"])
        return d

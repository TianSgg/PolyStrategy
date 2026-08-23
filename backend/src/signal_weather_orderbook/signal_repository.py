"""天气信号查询 DAO — 读取 signal_weather_events 表。"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from shared.db import get_db

logger = logging.getLogger(__name__)


class WeatherSignalRepository:
    """查询 signal_weather_events 表中的信号记录。"""

    def find_by_id(self, notification_key: str) -> Optional[Dict[str, Any]]:
        sql = "SELECT * FROM signal_weather_events WHERE notification_key = %s"
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (notification_key,))
                row = cur.fetchone()
                return self._row_to_dict(cur, row) if row else None

    def query(
        self,
        *,
        token_id: Optional[str] = None,
        city: Optional[str] = None,
        event_type: Optional[str] = None,
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
        if city:
            conditions.append("city = %s")
            params.append(city)
        if event_type:
            conditions.append("event_type = %s")
            params.append(event_type)
        if outcome:
            conditions.append("outcome = %s")
            params.append(outcome)
        if start_time:
            conditions.append("occurred_at >= %s")
            params.append(start_time)
        if end_time:
            conditions.append("occurred_at <= %s")
            params.append(end_time)

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT * FROM signal_weather_events WHERE {where} ORDER BY occurred_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
                return [self._row_to_dict(cur, r) for r in rows]

    def _row_to_dict(self, cursor, row) -> Dict[str, Any]:
        columns = [desc[0] for desc in cursor.description]
        d = dict(zip(columns, row))
        if isinstance(d.get("payload"), str):
            d["payload"] = json.loads(d["payload"])
        return d

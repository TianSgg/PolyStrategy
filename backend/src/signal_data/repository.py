"""signal_events / signal_market_contexts / signal_analysis_features 的 DAO 层。

只负责 SQL 与序列化，不含业务逻辑。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from shared.db import get_db

logger = logging.getLogger(__name__)


class SignalEventRepository:
    """signal_events 表读写。"""

    def save(self, event: dict[str, Any]) -> bool:
        """插入一条信号记录。返回 True 表示成功，False 表示重复（幂等）。"""
        sql = """
            INSERT INTO signal_events
                (id, source_type, source_event_id, event_type, token_id,
                 outcome, side, leader_proxy_wallet,
                 occurred_at, received_at, received_monotonic_ns, payload_json)
            VALUES
                (%(id)s, %(source_type)s, %(source_event_id)s, %(event_type)s, %(token_id)s,
                 %(outcome)s, %(side)s, %(leader_proxy_wallet)s,
                 %(occurred_at)s, %(received_at)s, %(received_monotonic_ns)s, %(payload_json)s)
            ON DUPLICATE KEY UPDATE id = id
        """
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {
                    **event,
                    "payload_json": json.dumps(event.get("payload_json", {}), ensure_ascii=False),
                })
                conn.commit()
                return cur.rowcount > 0

    def find_by_id(self, signal_id: str) -> dict[str, Any] | None:
        sql = "SELECT * FROM signal_events WHERE id = %s"
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (signal_id,))
                row = cur.fetchone()
                return self._row_to_dict(cur, row) if row else None

    def query(
        self,
        *,
        source_type: str | None = None,
        token_id: str | None = None,
        leader_proxy_wallet: str | None = None,
        event_type: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """按条件查询信号事件。"""
        conditions: list[str] = []
        params: list[Any] = []

        if source_type:
            conditions.append("source_type = %s")
            params.append(source_type)
        if token_id:
            conditions.append("token_id = %s")
            params.append(token_id)
        if leader_proxy_wallet:
            conditions.append("leader_proxy_wallet = %s")
            params.append(leader_proxy_wallet)
        if event_type:
            conditions.append("event_type = %s")
            params.append(event_type)
        if start_time:
            conditions.append("received_at >= %s")
            params.append(start_time)
        if end_time:
            conditions.append("received_at <= %s")
            params.append(end_time)

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT * FROM signal_events WHERE {where} ORDER BY received_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
                return [self._row_to_dict(cur, r) for r in rows]

    def _row_to_dict(self, cursor, row) -> dict[str, Any]:
        columns = [desc[0] for desc in cursor.description]
        d = dict(zip(columns, row))
        if isinstance(d.get("payload_json"), str):
            d["payload_json"] = json.loads(d["payload_json"])
        return d


class SignalMarketContextRepository:
    """signal_market_contexts 表读写。"""

    def save(self, ctx: dict[str, Any]) -> int:
        sql = """
            INSERT INTO signal_market_contexts
                (signal_id, token_id, observed_at, tick_size,
                 best_bid, best_ask, bid_depth, ask_depth,
                 bid_levels, ask_levels, book_summary_json)
            VALUES
                (%(signal_id)s, %(token_id)s, %(observed_at)s, %(tick_size)s,
                 %(best_bid)s, %(best_ask)s, %(bid_depth)s, %(ask_depth)s,
                 %(bid_levels)s, %(ask_levels)s, %(book_summary_json)s)
        """
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {
                    **ctx,
                    "book_summary_json": json.dumps(ctx.get("book_summary_json", {}), ensure_ascii=False)
                    if ctx.get("book_summary_json") else None,
                })
                conn.commit()
                return cur.lastrowid

    def find_by_signal(self, signal_id: str) -> list[dict[str, Any]]:
        sql = "SELECT * FROM signal_market_contexts WHERE signal_id = %s ORDER BY observed_at"
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (signal_id,))
                rows = cur.fetchall()
                return [self._row_to_dict(cur, r) for r in rows]

    def _row_to_dict(self, cursor, row) -> dict[str, Any]:
        columns = [desc[0] for desc in cursor.description]
        d = dict(zip(columns, row))
        if isinstance(d.get("book_summary_json"), str):
            d["book_summary_json"] = json.loads(d["book_summary_json"])
        return d


class SignalAnalysisFeaturesRepository:
    """signal_analysis_features 表读写。"""

    def save(self, feature: dict[str, Any]) -> bool:
        sql = """
            INSERT INTO signal_analysis_features
                (signal_id, feature_set, feature_version, computed_at, features_json)
            VALUES
                (%(signal_id)s, %(feature_set)s, %(feature_version)s, %(computed_at)s, %(features_json)s)
            ON DUPLICATE KEY UPDATE
                features_json = VALUES(features_json),
                computed_at = VALUES(computed_at)
        """
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {
                    **feature,
                    "features_json": json.dumps(feature.get("features_json", {}), ensure_ascii=False),
                })
                conn.commit()
                return cur.rowcount > 0

    def find_by_signal(self, signal_id: str) -> list[dict[str, Any]]:
        sql = "SELECT * FROM signal_analysis_features WHERE signal_id = %s ORDER BY feature_set, feature_version"
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (signal_id,))
                rows = cur.fetchall()
                return [self._row_to_dict(cur, r) for r in rows]

    def _row_to_dict(self, cursor, row) -> dict[str, Any]:
        columns = [desc[0] for desc in cursor.description]
        d = dict(zip(columns, row))
        if isinstance(d.get("features_json"), str):
            d["features_json"] = json.loads(d["features_json"])
        return d

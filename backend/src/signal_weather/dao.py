from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from asyncmy.cursors import DictCursor

from signal_weather.types import WeatherCity
from signal_weather.types import WeatherSignalRecord

class WeatherCityRepository:
    """MySQL-backed runtime source for enabled weather monitoring cities."""

    def __init__(self, pool) -> None:
        self._pool = pool

    async def list_enabled(self) -> list[WeatherCity]:
        query = """
            SELECT city_name, city_slug, timezone,
                   has_highest_market, has_lowest_market,
                   monitor_highest, monitor_lowest
            FROM weather_cities
            WHERE enabled = 1
            ORDER BY sort_order ASC, id ASC
        """
        async with self._pool.acquire() as connection, connection.cursor(DictCursor) as cursor:
            await cursor.execute(query)
            rows = await cursor.fetchall()

        cities = [self._to_city(row) for row in rows]
        if not cities:
            raise RuntimeError("weather_cities has no enabled monitoring city")
        return cities

    async def list_all(self) -> list[dict]:
        query = """
            SELECT id, city_name, city_slug, timezone,
                   has_highest_market, has_lowest_market,
                   monitor_highest, monitor_lowest,
                   enabled, sort_order
            FROM weather_cities
            ORDER BY sort_order ASC, id ASC
        """
        async with self._pool.acquire() as connection, connection.cursor(DictCursor) as cursor:
            await cursor.execute(query)
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_by_id(self, city_id: int) -> dict | None:
        query = """
            SELECT id, city_name, city_slug, timezone,
                   has_highest_market, has_lowest_market,
                   monitor_highest, monitor_lowest,
                   enabled, sort_order
            FROM weather_cities WHERE id = %s
        """
        async with self._pool.acquire() as connection, connection.cursor(DictCursor) as cursor:
            await cursor.execute(query, (city_id,))
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def insert(self, data: dict) -> int:
        query = """
            INSERT INTO weather_cities
                (city_name, city_slug, timezone, has_highest_market, has_lowest_market,
                 monitor_highest, monitor_lowest, enabled, sort_order)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        values = (
            data["city_name"], data["city_slug"], data["timezone"],
            int(data.get("has_highest_market", False)),
            int(data.get("has_lowest_market", False)),
            int(data.get("monitor_highest", False)),
            int(data.get("monitor_lowest", False)),
            int(data.get("enabled", True)),
            data.get("sort_order", 0),
        )
        async with self._pool.acquire() as connection, connection.cursor() as cursor:
            await cursor.execute(query, values)
            return cursor.lastrowid

    async def update(self, city_id: int, data: dict) -> bool:
        fields = []
        values: list[Any] = []
        for col in ("city_name", "city_slug", "timezone", "has_highest_market",
                    "has_lowest_market", "monitor_highest", "monitor_lowest",
                    "enabled", "sort_order"):
            if col in data:
                fields.append(f"{col} = %s")
                val = data[col]
                if col in ("has_highest_market", "has_lowest_market",
                           "monitor_highest", "monitor_lowest", "enabled"):
                    val = int(val)
                values.append(val)
        if not fields:
            return False
        values.append(city_id)
        query = f"UPDATE weather_cities SET {', '.join(fields)} WHERE id = %s"
        async with self._pool.acquire() as connection, connection.cursor() as cursor:
            await cursor.execute(query, tuple(values))
            return cursor.rowcount > 0

    async def delete(self, city_id: int) -> bool:
        async with self._pool.acquire() as connection, connection.cursor() as cursor:
            await cursor.execute("DELETE FROM weather_cities WHERE id = %s", (city_id,))
            return cursor.rowcount > 0

    @staticmethod
    def _to_city(row: Mapping[str, object]) -> WeatherCity:
        name = str(row.get("city_name") or "").strip()
        slug = str(row.get("city_slug") or "").strip()
        timezone = str(row.get("timezone") or "").strip()
        if not name or not slug or not timezone:
            raise RuntimeError("weather_cities row needs non-empty city_name, city_slug, and timezone")
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as error:
            raise RuntimeError(f"weather_cities {slug}: invalid IANA timezone {timezone!r}") from error

        has_highest = bool(row.get("has_highest_market"))
        has_lowest = bool(row.get("has_lowest_market"))
        monitor_highest = bool(row.get("monitor_highest"))
        monitor_lowest = bool(row.get("monitor_lowest"))
        if monitor_highest and not has_highest:
            raise RuntimeError(f"weather_cities {slug}: monitor_highest requires has_highest_market")
        if monitor_lowest and not has_lowest:
            raise RuntimeError(f"weather_cities {slug}: monitor_lowest requires has_lowest_market")

        directions = tuple(
            direction
            for direction, enabled in (("highest", monitor_highest), ("lowest", monitor_lowest))
            if enabled
        )
        if not directions:
            raise RuntimeError(f"weather_cities {slug}: enabled city must monitor at least one direction")
        return WeatherCity(name=name, slug=slug, timezone=timezone, directions=directions)


class WeatherSignalEventRepository:
    """MySQL persistence and event-scoped history retrieval for weather alerts."""

    def __init__(self, pool) -> None:
        self._pool = pool

    async def ping(self) -> None:
        """Validate the signal table exists without changing its schema."""
        try:
            async with self._pool.acquire() as connection, connection.cursor() as cursor:
                await cursor.execute("SELECT 1 FROM weather_orderbook_signals LIMIT 1")
                await cursor.fetchone()
        except Exception as error:
            raise RuntimeError(
                "weather_orderbook_signals is unavailable; run migrations before starting the service"
            ) from error

    async def insert_if_absent(self, record: WeatherSignalRecord) -> bool:
        query = """
            INSERT INTO weather_orderbook_signals (
                signal_id, occurred_at, signal_type, event_slug,
                city, city_slug, direction, local_date,
                market_slug, temperature_label, outcome,
                main_market_slug, main_temperature_label, main_outcome,
                token_id, status, reason, payload
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            ) ON DUPLICATE KEY UPDATE signal_id = signal_id
        """
        values = (
            record.signal_id,
            _mysql_datetime(record.occurred_at),
            record.signal_type,
            record.event_slug,
            record.city,
            record.city_slug,
            record.direction,
            record.local_date,
            record.market_slug,
            record.temperature_label,
            record.outcome,
            record.main_market_slug,
            record.main_temperature_label,
            record.main_outcome,
            record.token_id,
            record.status,
            record.reason,
            json.dumps(record.payload, ensure_ascii=False, separators=(",", ":")),
        )
        async with self._pool.acquire() as connection, connection.cursor() as cursor:
            await cursor.execute(query, values)
            return cursor.rowcount == 1

    async def list_for_event(
        self,
        event_slug: str,
        limit: int,
        before_id: int | None = None,
    ) -> list[WeatherSignalRecord]:
        limit = max(1, min(limit, 501))
        query = """
            SELECT id, signal_id, occurred_at, signal_type, event_slug,
                   city, city_slug, direction, local_date,
                   market_slug, temperature_label, outcome,
                   main_market_slug, main_temperature_label, main_outcome,
                   token_id, status, reason, payload, created_at
            FROM weather_orderbook_signals
            WHERE event_slug = %s
        """
        params: list[object] = [event_slug]
        if before_id is not None:
            query += """
                AND (
                    occurred_at < (
                        SELECT occurred_at FROM weather_orderbook_signals WHERE id = %s
                    )
                    OR (
                        occurred_at = (
                            SELECT occurred_at FROM weather_orderbook_signals WHERE id = %s
                        ) AND id < %s
                    )
                )
            """
            params.extend([before_id, before_id, before_id])
        query += " ORDER BY occurred_at DESC, id DESC LIMIT %s"
        params.append(limit)
        async with self._pool.acquire() as connection, connection.cursor(DictCursor) as cursor:
            await cursor.execute(query, tuple(params))
            rows = await cursor.fetchall()
        return [self._to_record(row) for row in rows]

    async def list_recent(
        self,
        limit: int,
        before_id: int | None = None,
        main_only: bool = True,
    ) -> list[WeatherSignalRecord]:
        limit = max(1, min(limit, 501))
        query = """
            SELECT id, signal_id, occurred_at, signal_type, event_slug,
                   city, city_slug, direction, local_date,
                   market_slug, temperature_label, outcome,
                   main_market_slug, main_temperature_label, main_outcome,
                   token_id, status, reason, payload, created_at
            FROM weather_orderbook_signals
        """
        conditions: list[str] = []
        params: list[object] = []
        if main_only:
            conditions.append("is_from_main = 1")
        if before_id is not None:
            conditions.append("""(
                    occurred_at < (
                        SELECT occurred_at FROM weather_orderbook_signals WHERE id = %s
                    )
                    OR (
                        occurred_at = (
                            SELECT occurred_at FROM weather_orderbook_signals WHERE id = %s
                        ) AND id < %s
                    )
                )""")
            params.extend([before_id, before_id, before_id])
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY occurred_at DESC, id DESC LIMIT %s"
        params.append(limit)
        async with self._pool.acquire() as connection, connection.cursor(DictCursor) as cursor:
            await cursor.execute(query, tuple(params))
            rows = await cursor.fetchall()
        return [self._to_record(row) for row in rows]

    async def count_for_events(self, event_slugs: set[str]) -> dict[str, int]:
        """Return durable signal counts for the active event set in one query."""
        if not event_slugs:
            return {}
        slugs = sorted(event_slugs)
        placeholders = ", ".join("%s" for _ in slugs)
        query = f"""
            SELECT event_slug, COUNT(*) AS signal_count
            FROM weather_orderbook_signals
            WHERE event_slug IN ({placeholders})
            GROUP BY event_slug
        """
        async with self._pool.acquire() as connection, connection.cursor(DictCursor) as cursor:
            await cursor.execute(query, tuple(slugs))
            rows = await cursor.fetchall()
        return {str(row["event_slug"]): int(row["signal_count"]) for row in rows}

    @staticmethod
    def _to_record(row: dict[str, Any]) -> WeatherSignalRecord:
        payload = row.get("payload")
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            payload = {}
        return WeatherSignalRecord(
            id=int(row["id"]),
            signal_id=str(row["signal_id"]),
            occurred_at=_utc_datetime(row["occurred_at"]),
            signal_type=str(row["signal_type"]),
            event_slug=str(row["event_slug"]),
            city=str(row["city"]),
            city_slug=str(row["city_slug"]),
            direction=str(row["direction"]),
            local_date=row["local_date"],
            market_slug=row.get("market_slug"),
            temperature_label=row.get("temperature_label"),
            outcome=row.get("outcome"),
            main_market_slug=row.get("main_market_slug"),
            main_temperature_label=row.get("main_temperature_label"),
            main_outcome=row.get("main_outcome"),
            token_id=row.get("token_id"),
            status=row.get("status"),
            reason=row.get("reason"),
            payload=payload,
            created_at=_utc_datetime(row["created_at"]),
        )


class WeatherDao(WeatherCityRepository, WeatherSignalEventRepository):
    """统一的天气服务 DAO。

    城市配置和天气信号属于同一个微服务的数据边界，统一由这个门面
    暴露。旧的 Repository 类暂时保留，便于并行迁移期间兼容已有导入。
    """

    pass


def _mysql_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("occurred_at must be timezone-aware UTC")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    return value.replace(tzinfo=timezone.utc)

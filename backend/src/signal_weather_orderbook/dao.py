from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from asyncmy.cursors import DictCursor

from signal_weather_orderbook.types import WeatherCity
from signal_weather_orderbook.types import WeatherNotificationRecord

CITY_TIMEZONE_MAP: dict[str, str] = {
    "taipei": "Asia/Taipei",
    "miami": "America/New_York",
    "kuala-lumpur": "Asia/Kuala_Lumpur",
    "paris": "Europe/Paris",
    "mexico-city": "America/Mexico_City",
    "nyc": "America/New_York",
    "panama-city": "America/Panama",
    "sao-paulo": "America/Sao_Paulo",
    "buenos-aires": "America/Argentina/Buenos_Aires",
    "lucknow": "Asia/Kolkata",
    "cape-town": "Africa/Johannesburg",
    "karachi": "Asia/Karachi",
    "london": "Europe/London",
    "wellington": "Pacific/Auckland",
    "tel-aviv": "Asia/Jerusalem",
    "tokyo": "Asia/Tokyo",
    "denver": "America/Denver",
    "manila": "Asia/Manila",
    "toronto": "America/Toronto",
    "amsterdam": "Europe/Amsterdam",
    "ankara": "Europe/Istanbul",
    "atlanta": "America/New_York",
    "austin": "America/Chicago",
    "beijing": "Asia/Shanghai",
    "busan": "Asia/Seoul",
    "chengdu": "Asia/Shanghai",
    "chicago": "America/Chicago",
    "chongqing": "Asia/Shanghai",
    "dallas": "America/Chicago",
    "guangzhou": "Asia/Shanghai",
    "helsinki": "Europe/Helsinki",
    "hong-kong": "Asia/Hong_Kong",
    "houston": "America/Chicago",
    "istanbul": "Europe/Istanbul",
    "jeddah": "Asia/Riyadh",
    "jinan": "Asia/Shanghai",
    "los-angeles": "America/Los_Angeles",
    "madrid": "Europe/Madrid",
    "milan": "Europe/Rome",
    "moscow": "Europe/Moscow",
    "munich": "Europe/Berlin",
    "qingdao": "Asia/Shanghai",
    "san-francisco": "America/Los_Angeles",
    "seattle": "America/Los_Angeles",
    "shanghai": "Asia/Shanghai",
    "shenzhen": "Asia/Shanghai",
    "singapore": "Asia/Singapore",
    "warsaw": "Europe/Warsaw",
    "wuhan": "Asia/Shanghai",
    "zhengzhou": "Asia/Shanghai",
}


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


class WeatherCityFileLoader:
    """Load weather cities from a local JSON file."""

    def __init__(self, file_path: str) -> None:
        self._path = Path(file_path)

    def list_enabled(self) -> list[WeatherCity]:
        if not self._path.is_file():
            raise RuntimeError(f"Weather cities file not found: {self._path}")
        data = json.loads(self._path.read_text(encoding="utf-8"))
        entries = data.get("cities", [])
        if not entries:
            raise RuntimeError(f"Weather cities file has no cities: {self._path}")
        cities: list[WeatherCity] = []
        for entry in entries:
            name = entry.get("name", "").strip()
            slug = entry.get("slug", "").strip()
            if not name or not slug:
                continue
            timezone = entry.get("timezone") or CITY_TIMEZONE_MAP.get(slug)
            if not timezone:
                raise RuntimeError(f"No timezone for city {slug}; add 'timezone' to JSON or CITY_TIMEZONE_MAP")
            try:
                ZoneInfo(timezone)
            except ZoneInfoNotFoundError as error:
                raise RuntimeError(f"Invalid timezone {timezone!r} for {slug}") from error
            monitor = entry.get("monitor", "highest")
            if monitor == "both":
                directions = ("highest", "lowest")
            elif monitor == "lowest":
                directions = ("lowest",)
            else:
                directions = ("highest",)
            cities.append(WeatherCity(name=name, slug=slug, timezone=timezone, directions=directions))
        if not cities:
            raise RuntimeError(f"Weather cities file produced no valid cities: {self._path}")
        return cities


class WeatherNotificationRepository:
    """MySQL persistence and event-scoped history retrieval for weather alerts."""

    def __init__(self, pool) -> None:
        self._pool = pool

    async def ping(self) -> None:
        """Validate the notification table exists without changing its schema."""
        try:
            async with self._pool.acquire() as connection, connection.cursor() as cursor:
                await cursor.execute("SELECT 1 FROM weather_notifications LIMIT 1")
                await cursor.fetchone()
        except Exception as error:
            raise RuntimeError(
                "weather_notifications is unavailable; run db/01_schema.sql before starting the service"
            ) from error

    async def insert_if_absent(self, record: WeatherNotificationRecord) -> bool:
        query = """
            INSERT INTO weather_notifications (
                notification_key, occurred_at, event_type, event_slug,
                city, city_slug, direction, local_date,
                market_slug, temperature_label, outcome,
                main_market_slug, main_temperature_label, main_outcome,
                token_id, status, reason, message, payload
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            ) ON DUPLICATE KEY UPDATE notification_key = notification_key
        """
        values = (
            record.notification_key,
            _mysql_datetime(record.occurred_at),
            record.event_type,
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
            record.message,
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
    ) -> list[WeatherNotificationRecord]:
        limit = max(1, min(limit, 501))
        query = """
            SELECT id, notification_key, occurred_at, event_type, event_slug,
                   city, city_slug, direction, local_date,
                   market_slug, temperature_label, outcome,
                   main_market_slug, main_temperature_label, main_outcome,
                   token_id, status, reason, message, payload, created_at
            FROM weather_notifications
            WHERE event_slug = %s
        """
        params: list[object] = [event_slug]
        if before_id is not None:
            query += """
                AND (
                    occurred_at < (
                        SELECT occurred_at FROM weather_notifications WHERE id = %s
                    )
                    OR (
                        occurred_at = (
                            SELECT occurred_at FROM weather_notifications WHERE id = %s
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
    ) -> list[WeatherNotificationRecord]:
        limit = max(1, min(limit, 501))
        query = """
            SELECT id, notification_key, occurred_at, event_type, event_slug,
                   city, city_slug, direction, local_date,
                   market_slug, temperature_label, outcome,
                   main_market_slug, main_temperature_label, main_outcome,
                   token_id, status, reason, message, payload, created_at
            FROM weather_notifications
        """
        params: list[object] = []
        if before_id is not None:
            query += """
                WHERE (
                    occurred_at < (
                        SELECT occurred_at FROM weather_notifications WHERE id = %s
                    )
                    OR (
                        occurred_at = (
                            SELECT occurred_at FROM weather_notifications WHERE id = %s
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

    async def count_for_events(self, event_slugs: set[str]) -> dict[str, int]:
        """Return durable notification counts for the active event set in one query."""
        if not event_slugs:
            return {}
        slugs = sorted(event_slugs)
        placeholders = ", ".join("%s" for _ in slugs)
        query = f"""
            SELECT event_slug, COUNT(*) AS notification_count
            FROM weather_notifications
            WHERE event_slug IN ({placeholders})
            GROUP BY event_slug
        """
        async with self._pool.acquire() as connection, connection.cursor(DictCursor) as cursor:
            await cursor.execute(query, tuple(slugs))
            rows = await cursor.fetchall()
        return {str(row["event_slug"]): int(row["notification_count"]) for row in rows}

    @staticmethod
    def _to_record(row: dict[str, Any]) -> WeatherNotificationRecord:
        payload = row.get("payload")
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            payload = {}
        return WeatherNotificationRecord(
            id=int(row["id"]),
            notification_key=str(row["notification_key"]),
            occurred_at=_utc_datetime(row["occurred_at"]),
            event_type=str(row["event_type"]),
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
            message=str(row["message"]),
            payload=payload,
            created_at=_utc_datetime(row["created_at"]),
        )


def _mysql_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("occurred_at must be timezone-aware UTC")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    return value.replace(tzinfo=timezone.utc)

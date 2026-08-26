from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from signal_weather.coordinator import BroadcastHandler, EventStartedHandler, WeatherCoordinator
from signal_weather.discovery import WeatherDiscovery
from signal_weather.types import WeatherCity
from signal_weather.gateway import PolymarketMarketClient
from signal_weather.types import WeatherEvent, WeatherSignalRecord
from signal_weather.dao import WeatherSignalEventRepository

logger = logging.getLogger(__name__)
WeatherEventHandler = Callable[[WeatherEvent], Awaitable[None]]


class WeatherOrderBookService:
    """Application service owning weather-monitor startup, maintenance, and queries.

    HTTP handlers must call this service rather than reaching into the
    coordinator or CLOB client directly.  It keeps the runtime lifecycle in
    one place while ``coordinator.py`` remains focused on event selection and
    candidate advancement.
    """

    def __init__(
        self,
        cities: list[WeatherCity],
        market_client: PolymarketMarketClient,
        on_weather_event: WeatherEventHandler,
        on_event_started: EventStartedHandler | None = None,
        signal_event_repository: WeatherSignalEventRepository | None = None,
        on_broadcast: BroadcastHandler | None = None,
    ) -> None:
        self.cities = cities
        self.market_client = market_client
        self.discovery = WeatherDiscovery(market_client)
        self.coordinator = WeatherCoordinator(
            cities,
            self.discovery,
            on_weather_event,
            on_event_started,
            on_broadcast=on_broadcast,
        )
        self.signal_event_repository = signal_event_repository
        self._signal_counts: dict[str, int] = {}
        self._signal_cache: dict[str, list[WeatherSignalRecord]] = {}
        self._signal_count_subscribers: set[asyncio.Queue] = set()
        self._rollover_task: asyncio.Task | None = None

    async def start(self) -> None:
        await self.coordinator.start()
        await self.refresh_signal_counts()
        await self._warm_signal_cache()
        self._rollover_task = asyncio.create_task(
            self._rollover_loop(),
            name="weather-local-date-rollover",
        )

    async def stop(self) -> None:
        if self._rollover_task:
            self._rollover_task.cancel()
            await asyncio.gather(self._rollover_task, return_exceptions=True)
        await self.coordinator.stop()

    def maintenance_status(self) -> dict[str, bool]:
        return {
            "local_date_rollover_running": bool(self._rollover_task and not self._rollover_task.done()),
        }

    def status(self) -> list[dict]:
        return self.coordinator.status()

    async def dashboard(self) -> list[dict]:
        dashboard = await self.coordinator.dashboard()
        active_event_slugs = self._active_event_slugs()
        self._signal_counts = {
            slug: count for slug, count in self._signal_counts.items() if slug in active_event_slugs
        }
        for city in dashboard:
            for direction in city["directions"]:
                direction["signal_count"] = self._signal_counts.get(direction.get("event_slug"), 0)
        return dashboard

    async def refresh_signal_counts(self) -> None:
        """Reload only active events after startup or a city-date plan replacement."""
        if self.signal_event_repository is None:
            return
        event_slugs = self._active_event_slugs()
        self._signal_counts = await self.signal_event_repository.count_for_events(event_slugs)
        self._publish_signal_counts({"type": "snapshot", "counts": self._signal_counts})

    async def _warm_signal_cache(self) -> None:
        """Load notifications for all active events into memory at startup."""
        if self.signal_event_repository is None:
            return
        for slug in self._active_event_slugs():
            records = await self.signal_event_repository.list_for_event(slug, limit=100)
            self._signal_cache[slug] = records

    def note_persisted_signal(self, event_slug: str, record: WeatherSignalRecord | None = None) -> None:
        """Increment count cache and append record to notification cache."""
        if not event_slug or event_slug not in self._active_event_slugs():
            return
        count = self._signal_counts.get(event_slug, 0) + 1
        self._signal_counts[event_slug] = count
        self._publish_signal_counts({"type": "update", "event_slug": event_slug, "count": count})
        if record is not None:
            cache = self._signal_cache.setdefault(event_slug, [])
            cache.insert(0, record)
            if len(cache) > 100:
                cache.pop()

    def cached_signals_for_event(self, event_slug: str) -> list[WeatherSignalRecord] | None:
        """Return cached records or None if this event is not in cache."""
        return self._signal_cache.get(event_slug)

    def evict_signal_cache(self, event_slug: str) -> None:
        self._signal_cache.pop(event_slug, None)

    def signal_count_snapshot(self) -> dict[str, int]:
        return self._signal_counts.copy()

    def subscribe_signal_counts(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._signal_count_subscribers.add(queue)
        return queue

    def unsubscribe_signal_counts(self, queue: asyncio.Queue) -> None:
        self._signal_count_subscribers.discard(queue)

    def _publish_signal_counts(self, payload: dict) -> None:
        for queue in tuple(self._signal_count_subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(payload)

    def _active_event_slugs(self) -> set[str]:
        return {
            item["event_slug"] for item in self.coordinator.status() if item.get("event_slug")
        }

    def direction_detail(self, city_slug: str, direction: str) -> dict | None:
        return self.coordinator.direction_detail(city_slug, direction)

    async def fetch_orderbook(self, token_id: str) -> dict | None:
        return await self.market_client.fetch_orderbook(token_id)

    def live_snapshot(self) -> list[dict]:
        return self.coordinator.live_snapshot()

    def subscribe_live_orderbooks(self) -> asyncio.Queue:
        return self.coordinator.subscribe_live_orderbooks()

    def unsubscribe_live_orderbooks(self, queue: asyncio.Queue) -> None:
        self.coordinator.unsubscribe_live_orderbooks(queue)

    async def _rollover_loop(self) -> None:
        """Replace plans at the earliest configured city's local midnight.

        City timezones are not all whole-hour offsets (for example Lucknow is
        UTC+05:30), so a UTC-hour-only scheduler can be 30 minutes late.
        """
        while True:
            now = datetime.now(timezone.utc)
            next_rollover = self._next_local_midnight(now)
            await asyncio.sleep(max(0, (next_rollover - now).total_seconds()))
            try:
                old_slugs = self._active_event_slugs()
                refreshed = await self.coordinator.refresh_local_days()
                if refreshed:
                    new_slugs = self._active_event_slugs()
                    for slug in old_slugs - new_slugs:
                        self.evict_signal_cache(slug)
                    await self.refresh_signal_counts()
                    await self._warm_signal_cache()
                    logger.info("Weather local-date rollover refreshed cities=%s", refreshed)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Weather local-date rollover check failed")

    def _next_local_midnight(self, now: datetime) -> datetime:
        """Return the next UTC instant at which any configured city hits midnight."""
        return min(
            (
                (now.astimezone(ZoneInfo(city.timezone)).replace(
                    hour=0, minute=0, second=0, microsecond=0,
                ) + timedelta(days=1)).astimezone(timezone.utc)
                for city in self.cities
            ),
            default=now + timedelta(hours=1),
        )


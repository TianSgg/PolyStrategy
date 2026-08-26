"""天气信号服务的业务门面。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import WebSocket, WebSocketDisconnect

from signal_weather.dao import WeatherDao
from signal_weather.internal.weather_engine import WeatherEngine
from signal_weather.internal._discovery import WeatherDiscovery
from signal_weather.internal.polymarket_client import PolymarketMarketClient
from signal_weather.types import WeatherCity, WeatherEvent, WeatherSignalRecord

logger = logging.getLogger(__name__)


class _SignalClient:
    __slots__ = ("ws", "client_id", "main_only")

    def __init__(self, ws: WebSocket, client_id: str, main_only: bool) -> None:
        self.ws = ws
        self.client_id = client_id
        self.main_only = main_only


class _SignalHub:
    """Small in-process broadcaster for strategy WebSocket subscribers."""

    def __init__(self) -> None:
        self._clients: dict[WebSocket, _SignalClient] = {}
        self._lock = asyncio.Lock()

    async def handle_connection(self, ws: WebSocket) -> None:
        await ws.accept()
        hello = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=5.0))
        client_id = hello.get("client_id", "unknown")
        main_only = bool(hello.get("main_only", False))
        await ws.send_text(json.dumps({
            "type": "welcome",
            "server_id": "weather_signal",
            "subscriptions": hello.get("subscribe", []),
        }))
        async with self._lock:
            self._clients[ws] = _SignalClient(ws, client_id, main_only)
        try:
            async for raw in ws.iter_text():
                if json.loads(raw).get("type") == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.debug("Weather signal WebSocket client failed", exc_info=True)
        finally:
            async with self._lock:
                self._clients.pop(ws, None)

    async def broadcast(self, signal: dict) -> None:
        payload = json.dumps({"type": "weather_sweep", "signal": signal})
        async with self._lock:
            clients = list(self._clients.values())
        for client in clients:
            if client.main_only and not signal.get("is_from_main", True):
                continue
            try:
                await client.ws.send_text(payload)
            except Exception:
                async with self._lock:
                    self._clients.pop(client.ws, None)


def _build_signal_record(event: WeatherEvent, city: WeatherCity, main_ctx: dict | None, next_candidate_orderbook: dict | None) -> WeatherSignalRecord:
    occurred_at_ms = event.current_orderbook.get("observed_at_unix_ms")
    try:
        occurred_at = datetime.fromtimestamp(int(occurred_at_ms) / 1000, timezone.utc)
    except (TypeError, ValueError, OSError):
        occurred_at = datetime.now(timezone.utc)
    payload = event.payload()
    if main_ctx:
        payload["main_monitor"] = main_ctx
    if next_candidate_orderbook:
        payload["next_candidate_orderbook"] = next_candidate_orderbook
    return WeatherSignalRecord(
        signal_id=f"{event.event_type}:{event.asset.event_slug}:{event.asset.asset_id}:{int(occurred_at.timestamp() * 1000)}",
        occurred_at=occurred_at,
        signal_type=event.event_type,
        event_slug=event.asset.event_slug,
        city=event.asset.city,
        city_slug=city.slug,
        direction=event.asset.event_slug.split("-temperature-in-", 1)[0],
        local_date=occurred_at.astimezone(ZoneInfo(city.timezone)).date(),
        market_slug=event.asset.market_slug,
        temperature_label=event.asset.temperature_label,
        outcome=event.asset.outcome,
        main_market_slug=main_ctx.get("main_market_slug") if main_ctx else None,
        main_temperature_label=main_ctx.get("main_temperature_label") if main_ctx else None,
        main_outcome=main_ctx.get("main_outcome") if main_ctx else None,
        token_id=event.asset.asset_id,
        status=None,
        reason=event.reason,
        payload=payload,
    )


class WeatherService:
    """Weather monitoring application service."""

    def __init__(self, cities: list[WeatherCity], dao: WeatherDao, market_client: PolymarketMarketClient) -> None:
        self.cities = cities
        self.dao = dao
        self.market_client = market_client
        self.signal_hub = _SignalHub()
        self.coordinator = WeatherEngine(
            cities,
            WeatherDiscovery(market_client),
            self._on_weather_event,
            on_broadcast=self._broadcast_event,
        )
        self._city_by_name = {city.name: city for city in cities}
        self._signal_counts: dict[str, int] = {}
        self._signal_cache: dict[str, list[WeatherSignalRecord]] = {}
        self._signal_count_subscribers: set[asyncio.Queue] = set()
        self._rollover_task: asyncio.Task | None = None

    async def start(self) -> None:
        await self.dao.ping()
        await self.coordinator.start()
        await self.refresh_signal_counts()
        await self._warm_signal_cache()
        self._rollover_task = asyncio.create_task(self._rollover_loop(), name="weather-local-date-rollover")

    async def stop(self) -> None:
        if self._rollover_task:
            self._rollover_task.cancel()
            await asyncio.gather(self._rollover_task, return_exceptions=True)
        await self.coordinator.stop()

    async def _on_weather_event(self, event: WeatherEvent, main_ctx=None, next_candidate_orderbook=None) -> None:
        city = self._city_by_name.get(event.asset.city)
        if city is None:
            logger.error("Unknown weather city: %s", event.asset.city)
            return
        try:
            record = _build_signal_record(event, city, main_ctx, next_candidate_orderbook)
            inserted = await self.dao.insert_if_absent(record)
        except Exception:
            logger.exception("Failed to persist weather event=%s", event.asset.event_slug)
            return
        if inserted:
            self.note_persisted_signal(event.asset.event_slug, record)

    async def _broadcast_event(self, event_type: str, payload: dict) -> None:
        asset = payload.get("asset", {})
        current = payload.get("current_orderbook", {})
        event_slug = asset.get("event_slug", "")
        signal = {
            "event_id": f"{event_slug}:{asset.get('asset_id', '')}:{int(time.time() * 1000)}",
            "event_type": event_type,
            "token_id": asset.get("asset_id", ""),
            "outcome": asset.get("outcome", ""),
            "city": asset.get("city", ""),
            "event_slug": event_slug,
            "market_slug": asset.get("market_slug"),
            "temperature_label": asset.get("temperature_label"),
            "direction": event_slug.split("-temperature-in-", 1)[0] if "-temperature-in-" in event_slug else "",
            "reason": payload.get("reason", ""),
            "is_from_main": payload.get("is_from_main", True),
            "occurred_at_ms": current.get("observed_at_unix_ms", int(time.time() * 1000)),
            "received_at_ns": time.time_ns(),
            "orderbook_snapshot": current,
        }
        if payload.get("next_candidate_orderbook"):
            signal["next_candidate_orderbook"] = payload["next_candidate_orderbook"]
        await self.signal_hub.broadcast(signal)

    async def handle_signal_websocket(self, websocket: WebSocket) -> None:
        await self.signal_hub.handle_connection(websocket)

    def status(self) -> list[dict]:
        return self.coordinator.status()

    async def dashboard(self) -> list[dict]:
        dashboard = await self.coordinator.dashboard()
        active = self._active_event_slugs()
        self._signal_counts = {slug: count for slug, count in self._signal_counts.items() if slug in active}
        for city in dashboard:
            for direction in city["directions"]:
                direction["signal_count"] = self._signal_counts.get(direction.get("event_slug"), 0)
        return dashboard

    def direction_detail(self, city_slug: str, direction: str) -> dict | None:
        return self.coordinator.direction_detail(city_slug, direction)

    async def fetch_orderbook(self, token_id: str) -> dict | None:
        return await self.market_client.fetch_orderbook(token_id)

    async def list_event_signals(self, event_slug: str, limit: int, before_id: int | None = None) -> list[WeatherSignalRecord]:
        cached = self._signal_cache.get(event_slug) if before_id is None else None
        return cached[:limit + 1] if cached is not None else await self.dao.list_for_event(event_slug, limit, before_id)

    async def list_recent_signals(self, limit: int, before_id: int | None = None) -> list[WeatherSignalRecord]:
        return await self.dao.list_recent(limit, before_id)

    def live_snapshot(self) -> list[dict]:
        return self.coordinator.live_snapshot()

    def subscribe_live_orderbooks(self) -> asyncio.Queue:
        return self.coordinator.subscribe_live_orderbooks()

    def unsubscribe_live_orderbooks(self, queue: asyncio.Queue) -> None:
        self.coordinator.unsubscribe_live_orderbooks(queue)

    def cached_signals_for_event(self, event_slug: str) -> list[WeatherSignalRecord] | None:
        return self._signal_cache.get(event_slug)

    def signal_count_snapshot(self) -> dict[str, int]:
        return self._signal_counts.copy()

    def subscribe_signal_counts(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._signal_count_subscribers.add(queue)
        return queue

    def unsubscribe_signal_counts(self, queue: asyncio.Queue) -> None:
        self._signal_count_subscribers.discard(queue)

    async def refresh_signal_counts(self) -> None:
        self._signal_counts = await self.dao.count_for_events(self._active_event_slugs())
        self._publish_signal_counts({"type": "snapshot", "counts": self._signal_counts})

    async def _warm_signal_cache(self) -> None:
        for slug in self._active_event_slugs():
            self._signal_cache[slug] = await self.dao.list_for_event(slug, 100)

    def note_persisted_signal(self, event_slug: str, record: WeatherSignalRecord | None = None) -> None:
        if event_slug not in self._active_event_slugs():
            return
        self._signal_counts[event_slug] = self._signal_counts.get(event_slug, 0) + 1
        self._publish_signal_counts({"type": "update", "event_slug": event_slug, "count": self._signal_counts[event_slug]})
        if record:
            cache = self._signal_cache.setdefault(event_slug, [])
            cache.insert(0, record)
            del cache[100:]

    def _active_event_slugs(self) -> set[str]:
        return {item["event_slug"] for item in self.coordinator.status() if item.get("event_slug")}

    def _publish_signal_counts(self, payload: dict) -> None:
        for queue in tuple(self._signal_count_subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(payload)

    async def _rollover_loop(self) -> None:
        while True:
            now = datetime.now(timezone.utc)
            next_rollover = self._next_local_midnight(now)
            await asyncio.sleep(max(0, (next_rollover - now).total_seconds()))
            try:
                old_slugs = self._active_event_slugs()
                refreshed = await self.coordinator.refresh_local_days()
                if refreshed:
                    for slug in old_slugs - self._active_event_slugs():
                        self._signal_cache.pop(slug, None)
                    await self.refresh_signal_counts()
                    await self._warm_signal_cache()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Weather local-date rollover failed")

    def _next_local_midnight(self, now: datetime) -> datetime:
        return min(((now.astimezone(ZoneInfo(city.timezone)).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).astimezone(timezone.utc) for city in self.cities), default=now + timedelta(hours=1))


WeatherOrderBookService = WeatherService

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from event_bus import EventBus
from signal_weather_orderbook.discovery import WeatherDiscovery
from signal_weather_orderbook.types import MarketCandidate, WeatherCity
from signal_weather_orderbook.types import WeatherEvent
from signal_weather_orderbook.monitor import WeatherOrderBookMonitor
from signal_weather_orderbook.gateway import SharedMarketWebSocket

logger = logging.getLogger(__name__)
EventStartedHandler = Callable[[WeatherCity, str, date, str, Optional[MarketCandidate], bool], Awaitable[None]]


@dataclass
class DirectionState:
    candidates: list[MarketCandidate]
    index: int = -1
    status: str = "discovering"
    main_monitor: WeatherOrderBookMonitor | None = None
    next_monitor: WeatherOrderBookMonitor | None = None


class WeatherCoordinator:
    """Owns startup HTTP selection and candidate-market WS advancement."""
    def __init__(
        self,
        cities: list[WeatherCity],
        discovery: WeatherDiscovery,
        on_event,
        on_event_started: EventStartedHandler | None = None,
        event_bus: EventBus | None = None,
    ):
        self.cities, self.discovery, self._on_event = cities, discovery, on_event
        self._on_event_started = on_event_started
        self._event_bus = event_bus
        self._states: dict[tuple[str, str], DirectionState] = {}
        self._city_dates: dict[str, date] = {}
        self._maintenance_lock = asyncio.Lock()
        self._live_subscribers: set[asyncio.Queue] = set()
        self._shared_ws = SharedMarketWebSocket(on_reconnect=self._on_ws_reconnect)

    async def start(self) -> None:
        await self._shared_ws.start()
        discovered = await asyncio.gather(*(self._discover_city_plan(city) for city in self.cities), return_exceptions=True)
        installations = []
        for city, result in zip(self.cities, discovered):
            if isinstance(result, Exception): continue
            target_date, plan = result
            self._city_dates[city.name] = target_date
            installations.append(self._install_city_plan(city, plan))
        await asyncio.gather(*installations, return_exceptions=True)

    async def _discover_city_plan(self, city: WeatherCity) -> tuple[date, dict[str, list[MarketCandidate]]]:
        target_date = await self.discovery.local_date(city)
        return target_date, await self.discovery.plans_for_city(city, target_date)

    async def stop(self) -> None:
        for state in self._states.values():
            if state.main_monitor:
                await state.main_monitor.stop()
            if state.next_monitor:
                await state.next_monitor.stop()
        await self._shared_ws.stop()

    async def refresh_local_days(self) -> list[str]:
        """Replace a city's event plan only after its local date changes."""
        async with self._maintenance_lock:
            return await self._refresh_local_days()

    async def _refresh_local_days(self) -> list[str]:
        dates = await asyncio.gather(*(self.discovery.local_date(city) for city in self.cities), return_exceptions=True)
        changes = [
            (city, target_date)
            for city, target_date in zip(self.cities, dates)
            if not isinstance(target_date, Exception) and self._city_dates.get(city.name) != target_date
        ]
        plans = await asyncio.gather(
            *(self.discovery.plans_for_city(city, target_date) for city, target_date in changes),
            return_exceptions=True,
        )
        refreshed: list[str] = []
        for (city, target_date), plan in zip(changes, plans):
            if isinstance(plan, Exception):
                continue
            await self._replace_city_plan(city, target_date, plan, emit_started=True)
            refreshed.append(city.name)
        return refreshed


    async def _install_city_plan(self, city: WeatherCity, plan: dict[str, list[MarketCandidate]]) -> set[str]:
        advances: list[Awaitable[None]] = []
        for direction, candidates in plan.items():
            self._states[(city.name, direction)] = DirectionState(candidates)
            advances.append(self._advance(city.name, direction, 0))
        results = await asyncio.gather(*advances, return_exceptions=True)
        return {
            direction
            for direction, result in zip(plan, results)
            if isinstance(result, Exception)
        }

    async def _replace_city_plan(
        self,
        city: WeatherCity,
        target_date: date,
        plan: dict[str, list[MarketCandidate]],
        *,
        emit_started: bool,
    ) -> None:
        current_keys = [key for key in self._states if key[0] == city.name]
        old_states = {key: self._states[key] for key in current_keys}
        for key in current_keys:
            state = self._states[key]
            if state.main_monitor:
                await self._unregister_monitor(state.main_monitor)
            if state.next_monitor:
                await self._unregister_monitor(state.next_monitor)
            del self._states[key]
        self._city_dates[city.name] = target_date
        failed_directions = await self._install_city_plan(city, plan)
        for direction in failed_directions:
            key = (city.name, direction)
            previous = old_states.get(key)
            if previous is None:
                continue
            # Restore old monitors for failed directions
            if previous.main_monitor:
                previous.main_monitor.start()
                await self._register_monitor(previous.main_monitor)
            if previous.next_monitor:
                previous.next_monitor.start()
                await self._register_monitor(previous.next_monitor)
            self._states[key] = previous
        if not emit_started or self._on_event_started is None:
            return
        for direction in city.directions:
            if direction in failed_directions:
                logger.warning("Date rollover initialization failed city=%s direction=%s", city.name, direction)
                continue
            state = self._states.get((city.name, direction))
            if state is None:
                continue
            candidate = self._selected_candidate(state)
            monitor_connected = self._shared_ws.connected
            if state.status == "monitoring" and not monitor_connected:
                logger.warning("Date rollover monitor did not start city=%s direction=%s", city.name, direction)
                continue
            try:
                await self._on_event_started(
                    city,
                    direction,
                    target_date,
                    state.status,
                    candidate,
                    monitor_connected,
                )
            except Exception:
                logger.exception("Date rollover notification failed city=%s direction=%s", city.name, direction)

    async def _advance(self, city: str, direction: str, start: int) -> None:
        state = self._states[(city, direction)]
        status, index, candidate = await self._expected_direction(state.candidates, start)
        await self._set_direction(city, direction, state.candidates, status, index, candidate)

    async def _advance_next(self, city: str, direction: str, start: int) -> None:
        """Advance to the next candidate without HTTP skip — always confirm via WS."""
        state = self._states[(city, direction)]
        if start >= len(state.candidates):
            await self._set_direction(city, direction, state.candidates, "exhausted", -1, None)
            return
        await self._set_direction(city, direction, state.candidates, "monitoring", start, state.candidates[start])

    async def _expected_direction(self, candidates: list[MarketCandidate], start: int = 0) -> tuple[str, int, MarketCandidate | None]:
        selected = await self.discovery.select_candidate(candidates, start)
        if not selected:
            return "exhausted", -1, None
        index, candidate = selected
        yes_book = await self.discovery._market_client.fetch_orderbook(candidate.yes_asset.asset_id)
        if yes_book is None:
            raise RuntimeError("CLOB orderbook request failed during candidate verification")
        if self.discovery._is_high_certainty(yes_book):
            return "resolved", index, candidate
        return "monitoring", index, candidate

    async def _set_direction(
        self,
        city: str,
        direction: str,
        candidates: list[MarketCandidate],
        status: str,
        index: int,
        candidate: MarketCandidate | None,
    ) -> None:
        old_state = self._states.get((city, direction))
        if old_state:
            if old_state.main_monitor:
                await self._unregister_monitor(old_state.main_monitor)
            if old_state.next_monitor:
                await self._unregister_monitor(old_state.next_monitor)

        state = DirectionState(candidates=candidates, index=index, status=status)
        self._states[(city, direction)] = state

        if status == "monitoring" and candidate:
            # Create current monitor (full mode)
            state.main_monitor = WeatherOrderBookMonitor(
                candidate, self._handle_event, self._publish_live_orderbook, mode="full",
            )
            state.main_monitor.start()
            await self._register_monitor(state.main_monitor)

            # Create next monitor (sweep_only mode) if next candidate exists
            next_index = index + 1
            if next_index < len(candidates):
                next_candidate = candidates[next_index]
                state.next_monitor = WeatherOrderBookMonitor(
                    next_candidate, self._handle_event, mode="sweep_only",
                )
                state.next_monitor.start()
                await self._register_monitor(state.next_monitor)

    async def _register_monitor(self, monitor: WeatherOrderBookMonitor) -> None:
        await self._shared_ws.subscribe(
            monitor.subscription_token_id(),
            monitor,
            monitor.registered_asset_ids(),
        )
        yes_id = monitor.candidate.yes_asset.asset_id
        await self._shared_ws.subscribe_for_initial_dump(yes_id)

    async def _unregister_monitor(self, monitor: WeatherOrderBookMonitor) -> None:
        await monitor.stop()
        await self._shared_ws.unsubscribe(
            monitor.subscription_token_id(),
            monitor,
            monitor.registered_asset_ids(),
        )

    async def _on_ws_reconnect(self) -> None:
        """After WS reconnects, temporarily subscribe all YES tokens for initial dumps."""
        for state in self._states.values():
            for monitor in (state.main_monitor, state.next_monitor):
                if monitor and monitor.running:
                    yes_id = monitor.candidate.yes_asset.asset_id
                    await self._shared_ws.subscribe_for_initial_dump(yes_id)

    def live_snapshot(self) -> list[dict]:
        """The current in-memory L2 state of every active monitor."""
        result = []
        for state in self._states.values():
            if state.main_monitor:
                result.append(state.main_monitor.live_payload())
        return result

    def subscribe_live_orderbooks(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._live_subscribers.add(queue)
        return queue

    def unsubscribe_live_orderbooks(self, queue: asyncio.Queue) -> None:
        self._live_subscribers.discard(queue)

    def _publish_live_orderbook(self, payload: dict) -> None:
        for queue in tuple(self._live_subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(payload)

    def _main_monitor_context(self, state: DirectionState) -> dict[str, str | None] | None:
        """Return main_monitor snapshot (always populated when main_monitor exists)."""
        if not state.main_monitor:
            return None
        c = state.main_monitor.candidate
        return {
            "main_market_slug": c.market_slug,
            "main_temperature_label": c.temperature_label,
            "main_outcome": state.main_monitor.favored_outcome(),
        }

    async def _handle_event(self, event: WeatherEvent) -> None:
        state_key = self._find_state_key_for_event(event)
        state = self._states[state_key] if state_key else None
        main_ctx = self._main_monitor_context(state) if state else None

        log = logger.debug if (event.event_type == "sweep" and event.asset.outcome != "no") else logger.info
        log(
            "[Coordinator] event=%s city=%s asset=%s outcome=%s reason=%s",
            event.event_type, event.asset.city, event.asset.asset_id[:8], event.asset.outcome, event.reason,
        )

        asyncio.create_task(self._on_event(event, main_ctx), name=f"notify-{event.event_type}")
        if self._event_bus:
            payload = event.payload()
            if main_ctx:
                payload["main_monitor"] = main_ctx
            self._event_bus.publish(f"weather.{event.event_type}", payload)
            logger.debug("[Coordinator] EventBus published: weather.%s", event.event_type)

        if not state_key:
            return
        state = self._states[state_key]

        if event.event_type == "no_longer_possible":
            if state.main_monitor and state.main_monitor.candidate.market_slug == event.asset.market_slug:
                logger.info("[Coordinator] Promoting next candidate: city=%s direction=%s", state_key[0], state_key[1])
                await self._promote_next(state_key)

        elif event.event_type == "market_resolved":
            if state.main_monitor and state.main_monitor.candidate.market_slug == event.asset.market_slug:
                logger.info("[Coordinator] Market resolved: city=%s direction=%s → status=resolved", state_key[0], state_key[1])
                await self._unregister_monitor(state.main_monitor)
                state.main_monitor = None
                if state.next_monitor:
                    await self._unregister_monitor(state.next_monitor)
                    state.next_monitor = None
                state.status = "resolved"

    async def _promote_next(self, state_key: tuple[str, str]) -> None:
        """Promote next_monitor to current, create new next."""
        state = self._states[state_key]

        # Unregister old current
        if state.main_monitor:
            await self._unregister_monitor(state.main_monitor)

        new_index = state.index + 1

        if state.next_monitor:
            # Promote: switch mode to full
            state.main_monitor = state.next_monitor
            state.main_monitor.mode = "full"
            state.next_monitor = None
            state.index = new_index

            # Check if the promoted monitor is already in high-certainty state
            state.main_monitor.check_high_certainty()

            # Create new next monitor for index+2
            lookahead = new_index + 1
            if lookahead < len(state.candidates):
                next_candidate = state.candidates[lookahead]
                state.next_monitor = WeatherOrderBookMonitor(
                    next_candidate, self._handle_event, mode="sweep_only",
                )
                state.next_monitor.start()
                await self._register_monitor(state.next_monitor)

            # Schema: see event_bus.py module docstring "weather.next_candidate"
            if self._event_bus:
                candidate = state.candidates[new_index]
                self._event_bus.publish("weather.next_candidate", {
                    "city": state_key[0],
                    "direction": state_key[1],
                    "event_slug": candidate.event_slug,
                    "market_slug": candidate.market_slug,
                    "temperature_label": candidate.temperature_label,
                    "yes_token_id": candidate.yes_asset.asset_id,
                    "no_token_id": candidate.no_asset.asset_id,
                })
        else:
            # No next_monitor — fall back to standard advancement
            if new_index >= len(state.candidates):
                state.main_monitor = None
                state.index = -1
                state.status = "exhausted"
            else:
                await self._advance_next(*state_key, new_index)

    def _find_state_key_for_event(self, event: WeatherEvent) -> tuple[str, str] | None:
        for key, state in self._states.items():
            if key[0] != event.asset.city:
                continue
            if state.main_monitor and state.main_monitor.candidate.event_slug == event.asset.event_slug:
                return key
            if state.next_monitor and state.next_monitor.candidate.event_slug == event.asset.event_slug:
                return key
        return None

    def status(self) -> list[dict]:
        result = []
        for (city, direction), state in self._states.items():
            candidate = self._selected_candidate(state)
            next_candidate = None
            if state.next_monitor:
                next_candidate = state.next_monitor.candidate
            result.append({
                "city": city,
                "direction": direction,
                "local_date": self._city_dates.get(city).isoformat() if city in self._city_dates else None,
                "status": state.status,
                "candidate_index": state.index,
                "event_slug": candidate.event_slug if candidate else None,
                "temperature_label": candidate.temperature_label if candidate else None,
                "next_temperature_label": next_candidate.temperature_label if next_candidate else None,
                "monitor": state.main_monitor.status() if state.main_monitor else None,
                "next_monitor": state.next_monitor.status() if state.next_monitor else None,
                "shared_ws": self._shared_ws.status(),
            })
        return result

    async def dashboard(self) -> list[dict]:
        """Frontend-ready city, direction, market, and local-time status."""
        now = datetime.now(timezone.utc)
        cities: list[dict] = []
        for city in self.cities:
            timezone_name = city.timezone
            local_time = now.astimezone(ZoneInfo(timezone_name)).isoformat()
            directions = [self._direction_payload(city, direction) for direction in city.directions]
            cities.append({
                "name": city.name,
                "slug": city.slug,
                "timezone": timezone_name,
                "local_time": local_time,
                "local_date": local_time[:10] if local_time else None,
                "directions": directions,
            })
        return cities

    def direction_detail(self, city_slug: str, direction: str) -> dict | None:
        city = next((item for item in self.cities if item.slug == city_slug), None)
        if city is None or direction not in city.directions:
            return None
        return self._direction_payload(city, direction)

    def _direction_payload(self, city: WeatherCity, direction: str) -> dict:
        state = self._states.get((city.name, direction))
        candidates = state.candidates if state else []
        selected = self._selected_candidate(state)
        next_candidate = state.next_monitor.candidate if state and state.next_monitor else None
        return {
            "direction": direction,
            "status": state.status if state else "discovering",
            "event_slug": selected.event_slug if selected else (candidates[0].event_slug if candidates else None),
            "main_market_slug": selected.market_slug if selected else None,
            "main_temperature_label": selected.temperature_label if selected else None,
            "next_market_slug": next_candidate.market_slug if next_candidate else None,
            "next_temperature_label": next_candidate.temperature_label if next_candidate else None,
            "monitor": state.main_monitor.status() if state and state.main_monitor else None,
            "next_monitor": state.next_monitor.status() if state and state.next_monitor else None,
            "markets": [
                {
                    "temperature_label": candidate.temperature_label,
                    "market_slug": candidate.market_slug,
                    "event_slug": candidate.event_slug,
                    "yes_token_id": candidate.yes_asset.asset_id,
                    "no_token_id": candidate.no_asset.asset_id,
                    "selected": bool(selected and candidate.market_slug == selected.market_slug),
                    "is_next": bool(next_candidate and candidate.market_slug == next_candidate.market_slug),
                }
                for candidate in candidates
            ],
        }

    @staticmethod
    def _selected_candidate(state: DirectionState | None) -> MarketCandidate | None:
        if state is None:
            return None
        if state.main_monitor:
            return state.main_monitor.candidate
        if 0 <= state.index < len(state.candidates):
            return state.candidates[state.index]
        return state.candidates[0] if state.candidates else None

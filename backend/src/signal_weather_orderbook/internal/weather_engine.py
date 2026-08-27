from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from time import time
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from signal_weather_orderbook.types import MarketCandidate, WeatherAsset, WeatherCity, WeatherEvent
from .market_websocket import SharedMarketWebSocket
from .orderbook import LocalOrderBook
from .polymarket_client import PolymarketMarketClient

logger = logging.getLogger(__name__)
EventStartedHandler = Callable[[WeatherCity, str, date, str, Optional[MarketCandidate], bool], Awaitable[None]]
BroadcastHandler = Callable[[str, dict], Awaitable[None]]
EventHandler = Callable[[WeatherEvent], Awaitable[None]]
BookUpdateHandler = Callable[[dict], None]
DriftCallback = Callable[[str], Awaitable[None]]

CONFIRM_SECONDS = int(os.environ.get("HIGH_CERTAINTY_CONFIRM_SECONDS", "60"))
MAX_HIGH_CERTAINTY_TICK = 0.001
MIN_HIGH_CERTAINTY_ASK = 0.999
MIN_HIGH_CERTAINTY_BID_WITH_ASK = 0.995
SWEEP_ASK_THRESHOLDS = (0.99, 0.98)
SWEEP_TICK_SIZE = 0.01


@dataclass
class DirectionState:
    candidates: list[MarketCandidate]
    index: int = -1
    status: str = "discovering"
    main_monitor: WeatherOrderBookMonitor | None = None
    next_monitor: WeatherOrderBookMonitor | None = None


class WeatherEngine:
    """Owns startup HTTP selection and candidate-market WS advancement."""
    def __init__(
        self,
        cities: list[WeatherCity],
        discovery: WeatherDiscovery,
        on_event,
        on_event_started: EventStartedHandler | None = None,
        on_broadcast: BroadcastHandler | None = None,
    ):
        self.cities, self.discovery, self._on_event = cities, discovery, on_event
        self._on_event_started = on_event_started
        self._on_broadcast = on_broadcast
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
        self._resync_task = asyncio.create_task(self._periodic_resync(), name="periodic-resync")

    async def _discover_city_plan(self, city: WeatherCity) -> tuple[date, dict[str, list[MarketCandidate]]]:
        target_date = await self.discovery.local_date(city)
        return target_date, await self.discovery.plans_for_city(city, target_date)

    async def stop(self) -> None:
        if hasattr(self, '_resync_task') and self._resync_task:
            self._resync_task.cancel()
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
                candidate, self._handle_event, self._publish_live_orderbook,
                on_drift=self._on_drift, mode="full",
            )
            state.main_monitor.start()
            await self._register_monitor(state.main_monitor)

            # Create next monitor (sweep_only mode) if next candidate exists
            next_index = index + 1
            if next_index < len(candidates):
                next_candidate = candidates[next_index]
                state.next_monitor = WeatherOrderBookMonitor(
                    next_candidate, self._handle_event,
                    on_drift=self._on_drift, mode="sweep_only",
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

    async def _on_drift(self, token_id: str) -> None:
        """Called when a monitor detects BBO drift; resync that token."""
        await self._shared_ws.resync_token(token_id)

    async def _periodic_resync(self) -> None:
        """Every 10 minutes, resync all subscribed tokens with staggered timing."""
        RESYNC_INTERVAL = 600  # 10 minutes
        await asyncio.sleep(RESYNC_INTERVAL)
        while True:
            try:
                tokens = list(self._shared_ws._subscribed_tokens)
                if tokens and self._shared_ws.connected:
                    interval = RESYNC_INTERVAL / len(tokens)
                    logger.info("Periodic resync: %d tokens, %.1fs apart", len(tokens), interval)
                    for token_id in tokens:
                        if not self._shared_ws.connected:
                            break
                        await self._shared_ws.resync_token(token_id)
                        await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Periodic resync error")
            await asyncio.sleep(RESYNC_INTERVAL)

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

        next_ob = None
        if event.event_type == "no_longer_possible" and state and state.next_monitor:
            next_ob = {
                "market_slug": state.next_monitor.candidate.market_slug,
                "temperature_label": state.next_monitor.candidate.temperature_label,
                **state.next_monitor.bbo_snapshot(),
            }

        is_from_main = bool(
            main_ctx and main_ctx.get("main_market_slug") == event.asset.market_slug
        )

        asyncio.create_task(self._on_event(event, main_ctx, next_ob), name=f"notify-{event.event_type}")
        if self._on_broadcast:
            payload = event.payload()
            payload["is_from_main"] = is_from_main
            if main_ctx:
                payload["main_monitor"] = main_ctx
            if next_ob:
                payload["next_candidate_orderbook"] = next_ob
            asyncio.create_task(self._on_broadcast(event.event_type, payload))

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
                    next_candidate, self._handle_event,
                    on_drift=self._on_drift, mode="sweep_only",
                )
                state.next_monitor.start()
                await self._register_monitor(state.next_monitor)

            if self._on_broadcast:
                candidate = state.candidates[new_index]
                asyncio.create_task(self._on_broadcast("next_candidate", {
                    "city": state_key[0],
                    "direction": state_key[1],
                    "event_slug": candidate.event_slug,
                    "market_slug": candidate.market_slug,
                    "temperature_label": candidate.temperature_label,
                    "yes_token_id": candidate.yes_asset.asset_id,
                    "no_token_id": candidate.no_asset.asset_id,
                }))
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

MAX_HIGH_CERTAINTY_TICK = 0.001
MIN_HIGH_CERTAINTY_ASK = 0.999
MIN_HIGH_CERTAINTY_BID_WITH_ASK = 0.995


class WeatherDiscovery:
    """HTTP-only discovery and candidate selection before real-time WS monitoring."""

    def __init__(self, market_client: PolymarketMarketClient):
        self._market_client = market_client

    async def plans_for_city(self, city: WeatherCity, target_date=None) -> dict[str, list[MarketCandidate]]:
        target_date = target_date or await self.local_date(city)
        tasks = [self._event_plan(city, direction, target_date) for direction in city.directions]
        plans = await asyncio.gather(*tasks)
        return dict(zip(city.directions, plans))

    async def _event_plan(self, city: WeatherCity, direction: str, target_date) -> list[MarketCandidate]:
        slug = self.event_slug(city, direction, target_date)
        event = await self._market_client.fetch_event(slug)
        if not event:
            return []
        candidates = [self._candidate(city.name, direction, slug, market) for market in event.get("markets", [])]
        candidates = [candidate for candidate in candidates if candidate]
        candidates.sort(key=lambda candidate: self._temperature(candidate.temperature_label), reverse=direction == "lowest")
        return candidates

    @staticmethod
    def event_slug(city: WeatherCity, direction: str, target_date) -> str:
        return (
            f"{direction}-temperature-in-{city.slug}-on-"
            f"{target_date.strftime('%B').lower()}-{target_date.day}-{target_date.year}"
        )

    async def select_candidate(self, candidates: list[MarketCandidate], start_index: int = 0) -> tuple[int, MarketCandidate] | None:
        """Skip NO markets already high-certainty using CLOB HTTP snapshots."""
        for index in range(start_index, len(candidates)):
            book = await self._market_client.fetch_orderbook(candidates[index].no_asset.asset_id)
            if book is None:
                raise RuntimeError("CLOB orderbook request failed during candidate selection")
            if not self._is_high_certainty(book):
                return index, candidates[index]
        return None

    async def local_date(self, city: WeatherCity):
        return datetime.now(timezone.utc).astimezone(ZoneInfo(city.timezone)).date()

    async def timezone_name(self, city: WeatherCity) -> str:
        return city.timezone

    @staticmethod
    def _candidate(city: str, direction: str, event_slug: str, market: dict) -> MarketCandidate | None:
        if market.get("closed") or market.get("active") is False:
            return None
        outcomes = WeatherDiscovery._list(market.get("outcomes"))
        token_ids = WeatherDiscovery._list(market.get("clobTokenIds") or market.get("clob_token_ids"))
        pairs = {str(outcome).lower(): str(token_id) for outcome, token_id in zip(outcomes, token_ids)}
        if not pairs.get("yes") or not pairs.get("no"):
            return None
        common = dict(city=city, event_slug=event_slug, market_slug=market.get("slug", ""), temperature_label=market.get("groupItemTitle") or market.get("question", ""))
        return MarketCandidate(direction=direction, yes_asset=WeatherAsset(asset_id=pairs["yes"], outcome="yes", **common), no_asset=WeatherAsset(asset_id=pairs["no"], outcome="no", **common), **common)

    @staticmethod
    def _is_high_certainty(book: dict | None) -> bool:
        if not isinstance(book, dict):
            return False
        try:
            tick = float(book.get("tick_size") or book.get("min_tick_size"))
        except (TypeError, ValueError):
            return False
        asks = [item for item in book.get("asks", []) if float(item.get("size", 0)) > 0]
        bids = [item for item in book.get("bids", []) if float(item.get("size", 0)) > 0]
        best_ask = min((float(item["price"]) for item in asks), default=None)
        best_bid = max((float(item["price"]) for item in bids), default=None)
        return tick <= MAX_HIGH_CERTAINTY_TICK and (
            best_ask is None
            or (
                best_ask >= MIN_HIGH_CERTAINTY_ASK
                and best_bid is not None
                and best_bid >= MIN_HIGH_CERTAINTY_BID_WITH_ASK
            )
        )

    @staticmethod
    def _list(value) -> list:
        if isinstance(value, list): return value
        if isinstance(value, str):
            try: return json.loads(value)
            except json.JSONDecodeError: return []
        return []

    @staticmethod
    def _temperature(value: str) -> float:
        import re
        # Interval labels put the unit after the second endpoint, for example
        # ``88-89°F``.  Read the unit from anywhere in the label, then use
        # the first endpoint as the ordering boundary.  This also covers
        # labels such as ``83°F or below`` and ``102°F or higher``.
        number_match = re.search(r"-?\d+(?:\.\d+)?", value or "")
        unit_match = re.search(r"°?\s*([CF])\b", value or "", re.IGNORECASE)
        if not number_match or not unit_match:
            return float("inf")
        number = float(number_match.group())
        return number if unit_match.group(1).upper() == "C" else (number - 32) * 5 / 9

class WeatherOrderBookMonitor:
    """Evaluation engine for one candidate market (YES + NO).

    Does NOT own a WebSocket connection. Receives raw messages via deliver()
    from a SharedMarketWebSocket instance.
    """

    def __init__(
        self,
        candidate: MarketCandidate,
        on_event: EventHandler,
        on_book_update: BookUpdateHandler | None = None,
        on_drift: DriftCallback | None = None,
        mode: Literal["full", "sweep_only"] = "full",
    ):
        self.candidate = candidate
        self.mode = mode
        self._on_event = on_event
        self._on_book_update = on_book_update
        self._on_drift = on_drift
        self._assets = {candidate.yes_asset.asset_id: candidate.yes_asset, candidate.no_asset.asset_id: candidate.no_asset}
        self._books: dict[str, LocalOrderBook] = {asset_id: LocalOrderBook() for asset_id in self._assets}
        self._last: dict[str, dict] = {}
        self._ticks: dict[str, float | None] = {asset_id: None for asset_id in self._assets}
        self._confirmations: dict[str, asyncio.Task] = {}
        self._drift_cooldown: dict[str, float] = {}
        self.running = False

    def favored_outcome(self) -> str | None:
        """Return 'yes' or 'no' based on which token has higher mid-price."""
        prices: dict[str, float] = {}
        for asset_id, book in self._books.items():
            price = self._reference_price(book.top_of_book())
            if price is not None:
                prices[asset_id] = price
        if len(prices) != 2:
            return None
        max_asset = max(prices, key=prices.get)
        return self._assets[max_asset].outcome

    def subscription_token_id(self) -> str:
        """The single token ID to subscribe on the wire (NO token)."""
        return self.candidate.no_asset.asset_id

    def registered_asset_ids(self) -> list[str]:
        """All asset IDs this monitor wants routed to it (YES + NO)."""
        return list(self._assets.keys())

    def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False
        confirmations = tuple(self._confirmations.values())
        for task in confirmations:
            task.cancel()
        if confirmations:
            await asyncio.gather(*confirmations, return_exceptions=True)
        self._confirmations.clear()

    def status(self) -> dict:
        return {
            "city": self.candidate.city,
            "direction": self.candidate.direction,
            "temperature_label": self.candidate.temperature_label,
            "market_slug": self.candidate.market_slug,
            "mode": self.mode,
            "running": self.running,
        }

    def bbo_snapshot(self) -> dict:
        """Compact best-bid/best-ask snapshot for YES and NO tokens."""
        result: dict[str, dict] = {}
        for asset_id, asset in self._assets.items():
            book = self._books.get(asset_id)
            if book is None:
                continue
            best_bid = max(book.bids, default=None)
            best_ask = min(book.asks, default=None)
            result[asset.outcome] = {
                "token_id": asset_id,
                "best_bid": {"price": best_bid, "size": book.bids[best_bid]} if best_bid is not None else None,
                "best_ask": {"price": best_ask, "size": book.asks[best_ask]} if best_ask is not None else None,
            }
        return result

    def live_payload(self) -> dict:
        """Complete local L2 books for the dashboard."""
        if not self._books:
            return {"city": self.candidate.city, "direction": self.candidate.direction,
                    "market_slug": self.candidate.market_slug, "books": {}}
        observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " UTC"
        books: dict[str, dict] = {}
        for asset_id, asset in self._assets.items():
            book = self._books.get(asset_id)
            if book is None:
                continue
            bids = [{"price": price, "size": size} for price, size in sorted(book.bids.items(), reverse=True)]
            asks = [{"price": price, "size": size} for price, size in sorted(book.asks.items())]
            books[asset.outcome] = {
                "token_id": asset_id,
                "observed_at": observed_at,
                "tick_size": self._ticks.get(asset_id),
                "best_bid": bids[0] if bids else None,
                "best_ask": asks[0] if asks else None,
                "bid_levels": len(bids),
                "ask_levels": len(asks),
                "bids": bids,
                "asks": asks,
            }
        return {
            "city": self.candidate.city,
            "direction": self.candidate.direction,
            "market_slug": self.candidate.market_slug,
            "mode": self.mode,
            "books": books,
        }

    def deliver(self, data: dict | list) -> None:
        """Entry point: receive a parsed WS message from SharedMarketWebSocket."""
        if not self.running:
            return
        if isinstance(data, list):
            for snapshot in data:
                asset_id = snapshot.get("asset_id")
                if asset_id in self._assets:
                    self._books[asset_id].apply_snapshot(snapshot.get("bids", []), snapshot.get("asks", []))
                    self._set_tick(asset_id, snapshot.get("tick_size") or snapshot.get("min_tick_size"))
                    self._evaluate(asset_id)
            self._publish_book_update()
            return
        if data.get("event_type") == "book":
            asset_id = data.get("asset_id")
            if asset_id in self._assets:
                previous_books = {
                    item_id: book.top_of_book() for item_id, book in self._books.items()
                }
                previous_asks = self._books[asset_id].asks.copy()
                previous_tick = self._ticks[asset_id]
                self._books[asset_id].apply_snapshot(data.get("bids", []), data.get("asks", []))
                self._set_tick(asset_id, data.get("tick_size") or data.get("min_tick_size"))
                self._evaluate(
                    asset_id,
                    previous_asks,
                    self._higher_probability_asset(previous_books),
                    previous_tick,
                )
                self._publish_book_update()
            return
        if data.get("event_type") == "tick_size_change":
            asset_id = data.get("asset_id")
            if asset_id in self._assets:
                self._set_tick(asset_id, data.get("new_tick_size"))
                self._publish_book_update()
            return
        if data.get("event_type") == "price_change":
            previous_books = {
                asset_id: book.top_of_book() for asset_id, book in self._books.items()
            }
            previous_asks: dict[str, dict[float, float]] = {}
            previous_ticks: dict[str, float | None] = {}
            affected_assets: set[str] = set()
            for change in data.get("price_changes", []):
                asset_id = change.get("asset_id")
                if asset_id in self._assets:
                    if asset_id not in previous_asks:
                        previous_asks[asset_id] = self._books[asset_id].asks.copy()
                        previous_ticks[asset_id] = self._ticks[asset_id]
                    self._books[asset_id].apply_change(change["price"], change["size"], change["side"])
                    affected_assets.add(asset_id)
                    self._check_bbo_drift(asset_id, change)
            high_probability_asset = self._higher_probability_asset(previous_books)
            for asset_id in affected_assets:
                self._evaluate(asset_id, previous_asks[asset_id], high_probability_asset, previous_ticks[asset_id])
            if affected_assets:
                self._publish_book_update()

    def _check_bbo_drift(self, asset_id: str, change: dict) -> None:
        """Compare local BBO with server-reported bestBid/bestAsk after applying change."""
        if not self._on_drift:
            return
        now = time()
        if now - self._drift_cooldown.get(asset_id, 0) < 60:
            return
        book = self._books[asset_id]
        server_bid = change.get("bestBid") or change.get("best_bid")
        server_ask = change.get("bestAsk") or change.get("best_ask")
        if server_bid is None and server_ask is None:
            return
        drifted = False
        if server_bid is not None:
            local_bid = max(book.bids, default=None)
            if local_bid is not None and abs(local_bid - float(server_bid)) > 1e-9:
                drifted = True
        if server_ask is not None:
            local_ask = min(book.asks, default=None)
            if local_ask is not None and abs(local_ask - float(server_ask)) > 1e-9:
                drifted = True
        if drifted:
            self._drift_cooldown[asset_id] = now
            asset = self._assets[asset_id]
            logger.warning(
                "[Monitor] BBO drift detected: %s %s %s outcome=%s local=%s/%s server=%s/%s",
                asset.city, asset.temperature_label, asset_id[:8], asset.outcome,
                max(book.bids, default=None), min(book.asks, default=None),
                server_bid, server_ask,
            )
            asyncio.create_task(self._on_drift(asset_id))

    def _evaluate(
        self,
        asset_id: str,
        previous_asks: dict[float, float] | None = None,
        high_probability_asset: str | None = None,
        previous_tick: float | None = None,
    ) -> None:
        book = self._books[asset_id]
        previous, current = self._last.get(asset_id), {
            **book.top_of_book(),
            "observed_at": datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3] + " UTC",
            "observed_at_unix_ms": int(time() * 1000),
        }
        if (
            previous_asks is not None
            and asset_id == high_probability_asset
            and previous_tick == SWEEP_TICK_SIZE
            and self._ticks[asset_id] == SWEEP_TICK_SIZE
        ):
            for threshold in SWEEP_ASK_THRESHOLDS:
                before_exists = any(price <= threshold and size > 0 for price, size in previous_asks.items())
                after_exists = any(price <= threshold and size > 0 for price, size in book.asks.items())
                if before_exists and not after_exists:
                    asset = self._assets[asset_id]
                    log = logger.info if asset.outcome == "no" else logger.debug
                    log(
                        "[Monitor] SWEEP detected%s: %s %s %s outcome=%s threshold=%.2f tick=%s",
                        "" if self.mode == "full" else " (next)",
                        asset.city, asset.temperature_label, asset_id[:8], asset.outcome, threshold, self._ticks[asset_id],
                    )
                    self._publish("sweep", asset_id, previous, current, f"ask_levels_through_{threshold:.2f}_cleared")
                    break
        self._last[asset_id] = current
        # High-certainty confirmation only in full mode
        if self.mode == "full":
            if self._high(asset_id):
                if asset_id not in self._confirmations:
                    asset = self._assets[asset_id]
                    logger.info(
                        "[Monitor] High-certainty timer started: %s %s %s outcome=%s (%ds)",
                        asset.city, asset.temperature_label, asset_id[:8], asset.outcome, CONFIRM_SECONDS,
                    )
                    self._confirmations[asset_id] = asyncio.create_task(self._confirm(asset_id, previous))
            elif task := self._confirmations.pop(asset_id, None):
                asset = self._assets[asset_id]
                logger.info("[Monitor] High-certainty cancelled: %s %s %s", asset.city, asset.temperature_label, asset_id[:8])
                task.cancel()

    def check_high_certainty(self) -> None:
        """Re-evaluate high-certainty state for all assets after mode promotion."""
        for asset_id in self._assets:
            if self._high(asset_id) and asset_id not in self._confirmations:
                previous = self._last.get(asset_id)
                self._confirmations[asset_id] = asyncio.create_task(self._confirm(asset_id, previous))

    def _high(self, asset_id: str) -> bool:
        book = self._books.get(asset_id)
        if book is None:
            return False
        if self._ticks.get(asset_id) != MAX_HIGH_CERTAINTY_TICK:
            return False
        best_ask = min(book.asks, default=None)
        best_bid = max(book.bids, default=None)
        return (
            best_ask is None
            or (
                best_ask >= MIN_HIGH_CERTAINTY_ASK
                and best_bid is not None
                and best_bid >= MIN_HIGH_CERTAINTY_BID_WITH_ASK
            )
        )

    @staticmethod
    def _reference_price(book: dict) -> float | None:
        ask = book.get("best_ask")
        bid = book.get("best_bid")
        ask_price = float(ask["price"]) if ask is not None else None
        bid_price = float(bid["price"]) if bid is not None else None
        if ask_price is not None and bid_price is not None:
            return (ask_price + bid_price) / 2
        return ask_price if ask_price is not None else bid_price

    def _higher_probability_asset(self, books: dict[str, dict]) -> str | None:
        prices = {asset_id: self._reference_price(book) for asset_id, book in books.items()}
        available = [(price, asset_id) for asset_id, price in prices.items() if price is not None]
        if len(available) != 2:
            return None
        first, second = available
        if first[0] == second[0]:
            return None
        return max(available)[1]

    async def _confirm(self, asset_id: str, before: dict | None) -> None:
        try:
            await asyncio.sleep(CONFIRM_SECONDS)
            after = self._last.get(asset_id)
            if after and self._high(asset_id):
                event_type = "market_resolved" if self._assets[asset_id].outcome == "yes" else "no_longer_possible"
                asset = self._assets[asset_id]
                logger.info(
                    "[Monitor] High-certainty confirmed → %s: %s %s %s outcome=%s",
                    event_type, asset.city, asset.temperature_label, asset_id[:8], asset.outcome,
                )
                self._publish(event_type, asset_id, before, after, f"high_certainty_maintained_{CONFIRM_SECONDS}s")
        except asyncio.CancelledError:
            raise
        finally:
            self._confirmations.pop(asset_id, None)

    def _publish(self, event_type: str, asset_id: str, before: dict | None, after: dict, reason: str) -> None:
        event = WeatherEvent(event_type=event_type, asset=self._assets[asset_id], previous_orderbook=before, current_orderbook=after, reason=reason)
        asyncio.create_task(self._on_event(event))

    def _publish_book_update(self) -> None:
        if self._on_book_update:
            self._on_book_update(self.live_payload())

    def _set_tick(self, asset_id: str | None, value: object) -> None:
        if asset_id in self._ticks:
            try:
                self._ticks[asset_id] = float(value)
            except (TypeError, ValueError):
                pass

__all__ = ["WeatherEngine", "WeatherDiscovery", "WeatherOrderBookMonitor"]

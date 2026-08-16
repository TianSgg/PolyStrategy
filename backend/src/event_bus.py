"""
Async pub/sub event bus.

Registered topics and their payload schemas:

weather.sweep
    Fired when a weather monitor detects an orderbook sweep (ask levels cleared).
    Payload: {
        "event_type": "sweep",
        "asset": {
            "asset_id": str,        # Polymarket token ID
            "outcome": "yes" | "no",
            "city": str,
            "event_slug": str,
            "market_slug": str,
            "temperature_label": str,
        },
        "previous_orderbook": dict | None,
        "current_orderbook": dict,
        "reason": str,              # e.g. "ask_levels_through_0.95_cleared"
        "main_monitor": dict | None,  # present if a main monitor context exists
    }

weather.no_longer_possible
    Fired when a market is confirmed no longer possible (high certainty maintained).
    Payload: same structure as weather.sweep, with event_type="no_longer_possible".

weather.market_resolved
    Fired when a market is confirmed resolved.
    Payload: same structure as weather.sweep, with event_type="market_resolved".

weather.next_candidate
    Fired when the coordinator promotes the next temperature candidate to main.
    Payload: {
        "city": str,
        "direction": str,           # e.g. "highest" | "lowest"
        "event_slug": str,
        "market_slug": str,
        "temperature_label": str,
        "yes_token_id": str,
        "no_token_id": str,
    }
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class EventBus:
    """Async pub/sub event bus. Publishers fire events, subscribers await them via queues."""

    _subscribers: dict[str, list[asyncio.Queue]] = field(default_factory=dict)

    def subscribe(self, event_type: str, maxsize: int = 128) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subscribers.setdefault(event_type, []).append(queue)
        return queue

    def unsubscribe(self, event_type: str, queue: asyncio.Queue) -> None:
        queues = self._subscribers.get(event_type)
        if queues:
            try:
                queues.remove(queue)
            except ValueError:
                pass

    def publish(self, event_type: str, payload: Any) -> None:
        for queue in self._subscribers.get(event_type, []):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                logger.warning("EventBus queue full for %s, dropped oldest message", event_type)
            queue.put_nowait(payload)

    @property
    def event_types(self) -> list[str]:
        return list(self._subscribers.keys())


_event_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    global _event_bus
    if _event_bus is None:
        _event_bus = EventBus()
    return _event_bus

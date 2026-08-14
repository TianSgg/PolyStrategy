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

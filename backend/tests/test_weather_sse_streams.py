import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from signal_weather_orderbook.api import (  # noqa: E402
    weather_live_orderbooks,
    weather_signal_counts_live,
)


class FakeWeatherService:
    def __init__(self) -> None:
        self.live_queue: asyncio.Queue = asyncio.Queue()
        self.live_subscribed = False
        self.signal_count_queue: asyncio.Queue = asyncio.Queue()
        self.signal_count_subscribed = False


    def live_snapshot(self) -> list[dict]:
        return [{"market_slug": "market"}]

    def subscribe_live_orderbooks(self) -> asyncio.Queue:
        self.live_subscribed = True
        return self.live_queue

    def unsubscribe_live_orderbooks(self, queue: asyncio.Queue) -> None:
        assert queue is self.live_queue
        self.live_subscribed = False

    def signal_count_snapshot(self) -> dict[str, int]:
        return {"market": 1}

    def subscribe_signal_counts(self) -> asyncio.Queue:
        self.signal_count_subscribed = True
        return self.signal_count_queue

    def unsubscribe_signal_counts(self, queue: asyncio.Queue) -> None:
        assert queue is self.signal_count_queue
        self.signal_count_subscribed = False


class TimeoutQueue:
    async def get(self):
        raise asyncio.TimeoutError


def _request(service: FakeWeatherService) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(weather_service=service))
    )


async def _live_stream_cancellation_unsubscribes_queue():
    service = FakeWeatherService()
    response = await weather_live_orderbooks(_request(service))
    iterator = response.body_iterator

    snapshot = await iterator.__anext__()
    assert "event: snapshot" in snapshot
    assert service.live_subscribed

    pending = asyncio.create_task(iterator.__anext__())
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert not service.live_subscribed


async def _signal_counts_stream_cancellation_unsubscribes_queue():
    service = FakeWeatherService()
    response = await weather_signal_counts_live(_request(service))
    iterator = response.body_iterator

    snapshot = await iterator.__anext__()
    assert "event: snapshot" in snapshot
    assert service.signal_count_subscribed

    pending = asyncio.create_task(iterator.__anext__())
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert not service.signal_count_subscribed


def test_live_stream_cancellation_unsubscribes_queue():
    asyncio.run(_live_stream_cancellation_unsubscribes_queue())


def test_signal_counts_stream_cancellation_unsubscribes_queue():
    asyncio.run(_signal_counts_stream_cancellation_unsubscribes_queue())


async def _live_stream_timeout_yields_keepalive():
    service = FakeWeatherService()
    service.live_queue = TimeoutQueue()  # type: ignore[assignment]
    response = await weather_live_orderbooks(_request(service))
    iterator = response.body_iterator

    await iterator.__anext__()
    assert await iterator.__anext__() == ": keepalive\n\n"

    await iterator.aclose()
    assert not service.live_subscribed


async def _signal_counts_stream_timeout_yields_keepalive():
    service = FakeWeatherService()
    service.signal_count_queue = TimeoutQueue()  # type: ignore[assignment]
    response = await weather_signal_counts_live(_request(service))
    iterator = response.body_iterator

    await iterator.__anext__()
    assert await iterator.__anext__() == ": keepalive\n\n"

    await iterator.aclose()
    assert not service.signal_count_subscribed


def test_live_stream_timeout_yields_keepalive():
    asyncio.run(_live_stream_timeout_yields_keepalive())


def test_signal_counts_stream_timeout_yields_keepalive():
    asyncio.run(_signal_counts_stream_timeout_yields_keepalive())

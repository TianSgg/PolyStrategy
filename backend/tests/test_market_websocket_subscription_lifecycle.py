import asyncio
import importlib.util
import json
from pathlib import Path

import pytest


_MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "signal_weather_orderbook"
    / "internal"
    / "market_websocket.py"
)
_SPEC = importlib.util.spec_from_file_location("market_websocket_under_test", _MODULE_PATH)
_MODULE = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MODULE)

SharedMarketWebSocket = _MODULE.SharedMarketWebSocket


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent_frames: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent_frames.append(json.loads(raw))


def connect_shared_ws(shared_ws: SharedMarketWebSocket) -> FakeWebSocket:
    fake_ws = FakeWebSocket()
    shared_ws._ws = fake_ws  # type: ignore[assignment]
    shared_ws.connected = True
    return fake_ws


@pytest.mark.asyncio
async def test_resync_persistent_token_stays_subscribed():
    shared_ws = SharedMarketWebSocket()
    fake_ws = connect_shared_ws(shared_ws)
    shared_ws._subscribed_tokens.add("no-token")

    await shared_ws.resync_token("no-token")

    assert shared_ws._subscribed_tokens == {"no-token"}
    assert shared_ws._pending_unsub == set()
    assert fake_ws.sent_frames == [
        {
            "assets_ids": ["no-token"],
            "type": "market",
            "operation": "unsubscribe",
        },
        {
            "assets_ids": ["no-token"],
            "type": "market",
            "operation": "subscribe",
            "level": 2,
            "initial_dump": True,
        },
    ]


@pytest.mark.asyncio
async def test_resync_temporary_token_auto_unsubscribes_after_initial_dump():
    shared_ws = SharedMarketWebSocket()
    fake_ws = connect_shared_ws(shared_ws)
    shared_ws._subscribed_tokens.add("no-token")

    await shared_ws.resync_token("yes-token")

    assert "yes-token" in shared_ws._pending_unsub
    assert shared_ws.status()["pending_initial_dump_tokens"] == 1

    initial_dump = json.dumps([
        {"asset_id": "yes-token", "bids": [], "asks": [], "tick_size": "0.01"}
    ])
    shared_ws._dispatch(initial_dump)
    # _dispatch schedules the unsubscribe as a task.
    await asyncio.sleep(0)

    assert shared_ws._pending_unsub == set()
    assert fake_ws.sent_frames[-1] == {
        "assets_ids": ["yes-token"],
        "type": "market",
        "operation": "unsubscribe",
    }


@pytest.mark.asyncio
async def test_wire_unsubscribe_includes_market_type():
    shared_ws = SharedMarketWebSocket()
    fake_ws = connect_shared_ws(shared_ws)

    await shared_ws._unsubscribe_wire("token")

    assert fake_ws.sent_frames == [
        {
            "assets_ids": ["token"],
            "type": "market",
            "operation": "unsubscribe",
        }
    ]


@pytest.mark.asyncio
async def test_stop_clears_subscription_state():
    shared_ws = SharedMarketWebSocket()
    connect_shared_ws(shared_ws)
    shared_ws._subscribed_tokens.add("no-token")
    shared_ws._routing["yes-token"] = []
    shared_ws._pending_unsub.add("yes-token")

    await shared_ws.stop()

    assert shared_ws._subscribed_tokens == set()
    assert shared_ws._routing == {}
    assert shared_ws._pending_unsub == set()
    assert shared_ws.connected is False

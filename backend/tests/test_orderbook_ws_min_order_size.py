import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from framework.orderbook_ws import LocalOrderBook, OrderBookWS  # noqa: E402


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payload):
        self._payload = payload

    def get(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return FakeResponse(self._payload)

    async def __aexit__(self, *args):
        return None


@pytest.mark.asyncio
async def test_orderbook_resync_caches_min_order_size():
    ws = OrderBookWS()
    ws._books["token"] = LocalOrderBook()
    ws._http_session = FakeSession({
        "bids": [],
        "asks": [],
        "tick_size": "0.001",
        "min_order_size": "5",
    })

    ok = await ws._fetch_and_replace("token", ws._books["token"])

    assert ok is True
    assert ws.get_min_order_size("token") == Decimal("5")
    assert ws.get_tick_size("token") == Decimal("0.001")


@pytest.mark.asyncio
async def test_orderbook_snapshot_caches_tick_size_and_min_order_size():
    ws = OrderBookWS()
    ws._books["token"] = LocalOrderBook()

    ws._handle_snapshot([{
        "asset_id": "token",
        "bids": [],
        "asks": [],
        "tick_size": "0.001",
        "min_order_size": "5",
    }])

    assert ws.get_tick_size("token") == Decimal("0.001")
    assert ws.get_min_order_size("token") == Decimal("5")


@pytest.mark.asyncio
async def test_orderbook_book_message_caches_tick_size_and_min_order_size():
    ws = OrderBookWS()
    ws._books["token"] = LocalOrderBook()

    ws._dispatch(
        '{"event_type":"book","asset_id":"token","bids":[],"asks":[],"tick_size":"0.001","min_order_size":"5"}'
    )

    assert ws.get_tick_size("token") == Decimal("0.001")
    assert ws.get_min_order_size("token") == Decimal("5")

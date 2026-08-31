import asyncio
import sys
from decimal import Decimal
from pathlib import Path

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


def test_orderbook_resync_caches_min_order_size():
    ws = OrderBookWS()
    ws._books["token"] = LocalOrderBook()
    ws._http_session = FakeSession({
        "bids": [],
        "asks": [],
        "min_order_size": "5",
    })

    ok = asyncio.run(ws._fetch_and_replace("token", ws._books["token"]))

    assert ok is True
    assert ws.get_min_order_size("token") == Decimal("5")

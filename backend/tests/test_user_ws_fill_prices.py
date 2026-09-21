import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from framework.user_ws import UserWS  # noqa: E402


def test_order_update_uses_each_pending_trade_price():
    async def scenario():
        fills = []

        async def on_fill(event):
            fills.append(event)

        ws = UserWS("0xwallet")
        ws.watch_order(
            order_id="order-1",
            token_id="token-1",
            side="BUY",
            price=Decimal("0.99"),
            on_fill=on_fill,
        )

        ws._handle_trade_event("order-1", "trade-1", Decimal("3"), Decimal("0.987"))
        ws._handle_trade_event("order-1", "trade-2", Decimal("2"), Decimal("0.988"))
        ws._handle_order_event("order-1", Decimal("5"))
        await asyncio.sleep(0)

        return fills

    fills = asyncio.run(scenario())

    assert [(fill.fill_size, fill.fill_price, fill.total_matched, fill.trade_id, fill.source)
            for fill in fills] == [
        (Decimal("3"), Decimal("0.987"), Decimal("3"), "trade-1", "ws_trade"),
        (Decimal("2"), Decimal("0.988"), Decimal("5"), "trade-2", "ws_trade"),
    ]

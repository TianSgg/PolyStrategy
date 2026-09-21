import asyncio
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from framework.strategy_runtime.order_executor import OrderExecutor  # noqa: E402
from framework.strategy_runtime.tick_size_service import TickSizeFetchError  # noqa: E402


class FakePoller:
    def __init__(self, wallet):
        self._proxy_wallet = wallet
        self.available_cash = Decimal("1000")


class FakeTickSizeService:
    def __init__(self, tick_size=Decimal("0.001")):
        self.tick_size = tick_size

    async def get(self, token_id, *, max_age_ms=None):
        return self.tick_size


def make_executor(service):
    executor = OrderExecutor(
        proxy_wallet="0xwallet",
        tick_size_service=service,
    )
    executor._poller = FakePoller("0xwallet")
    return executor


def test_place_order_uses_authoritative_tick_size():
    executor = make_executor(FakeTickSizeService(Decimal("0.001")))

    def fake_place_limit_order(*args, **kwargs):
        return {
            "status": "matched",
            "orderID": "0xorder",
            "takingAmount": "10",
            "makingAmount": "10",
        }

    with patch(
        "framework.strategy_runtime.order_executor.place_limit_order",
        side_effect=fake_place_limit_order,
    ) as place_order:
        result = asyncio.run(executor.place_order(
            token_id="token",
            side="SELL",
            price="0.999",
            size="10",
            tick_size="0.001",
            neg_risk=True,
            check_balance=False,
        ))

    assert result.status == "filled"
    assert place_order.call_args.args[5] == "0.001"


def test_matched_order_uses_clob_execution_vwap_not_limit_price():
    executor = make_executor(FakeTickSizeService())

    buy = executor._parse_result(
        "buy-1",
        {
            "status": "matched",
            "orderID": "buy-1",
            "takingAmount": "10",
            "makingAmount": "9.87",
        },
        0,
        Decimal("10"),
        "BUY",
    )
    sell = executor._parse_result(
        "sell-1",
        {
            "status": "matched",
            "orderID": "sell-1",
            "takingAmount": "9.91",
            "makingAmount": "10",
        },
        0,
        Decimal("10"),
        "SELL",
    )

    assert buy.filled_price == "0.987"
    assert sell.filled_price == "0.991"


def test_place_order_rejects_tick_size_mismatch_before_send():
    executor = make_executor(FakeTickSizeService(Decimal("0.01")))

    with patch(
        "framework.strategy_runtime.order_executor.place_limit_order",
    ) as place_order:
        result = asyncio.run(executor.place_order(
            token_id="token",
            side="SELL",
            price="0.999",
            size="10",
            tick_size="0.001",
            neg_risk=True,
            check_balance=False,
        ))

    assert result.status == "failed"
    assert "tick size mismatch" in result.error
    assert place_order.call_count == 0


def test_place_order_fails_closed_when_tick_lookup_fails():
    class FailingService:
        async def get(self, token_id, *, max_age_ms=None):
            raise TickSizeFetchError("network down")

    executor = make_executor(FailingService())

    with patch(
        "framework.strategy_runtime.order_executor.place_limit_order",
    ) as place_order:
        result = asyncio.run(executor.place_order(
            token_id="token",
            side="SELL",
            price="0.999",
            size="10",
            tick_size="0.001",
            neg_risk=True,
            check_balance=False,
        ))

    assert result.status == "failed"
    assert "tick size refresh failed" in result.error
    assert place_order.call_count == 0

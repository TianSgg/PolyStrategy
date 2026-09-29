import asyncio
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from framework.strategy_runtime.balance_poller import BalancePoller  # noqa: E402
from framework.strategy_runtime.order_executor import OrderExecutor  # noqa: E402
from framework.strategy_runtime.interfaces import OrderResult  # noqa: E402
from strategy_follow_weather_sweeper.service import _is_buy_balance_failure  # noqa: E402


@pytest.mark.asyncio
async def test_shared_follower_buy_reservation_is_atomic():
    poller = BalancePoller(
        "0x" + "1" * 40,
        safety_buffer=Decimal("0"),
    )
    poller._collateral_balance = Decimal("50")

    reserved = await asyncio.gather(*(
        poller.reserve_buy_shares(
            Decimal("100"), Decimal("0.99"), Decimal("5"),
        )
        for _ in range(2)
    ))

    assert sorted(reserved) == [Decimal("0"), Decimal("50")]
    assert poller.available_cash == Decimal("0.50")

    await poller.release_buy(Decimal("49.50"))
    assert poller.available_cash == Decimal("50")


@pytest.mark.asyncio
async def test_shared_follower_reservation_respects_minimum_buy_size():
    poller = BalancePoller(
        "0x" + "2" * 40,
        safety_buffer=Decimal("0"),
    )
    poller._collateral_balance = Decimal("4.94")

    assert await poller.reserve_buy_shares(
        Decimal("100"), Decimal("0.99"), Decimal("5"),
    ) == Decimal("0")
    assert poller.available_cash == Decimal("4.94")


@pytest.mark.asyncio
async def test_order_executor_reserves_from_cache_without_refreshing_api():
    wallet = "0x" + "3" * 40
    poller = BalancePoller(wallet, safety_buffer=Decimal("0"))
    poller._collateral_balance = Decimal("6.93")
    poller.refresh = AsyncMock(side_effect=AssertionError("unexpected live refresh"))

    executor = OrderExecutor(proxy_wallet=wallet)
    executor._poller = poller

    reserved = await executor.reserve_buy_shares(
        Decimal("10"), Decimal("0.99"), Decimal("5"),
    )

    assert reserved == Decimal("7")
    assert poller.available_cash == Decimal("0")
    poller.refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_order_executor_skips_cached_balance_below_minimum():
    wallet = "0x" + "4" * 40
    poller = BalancePoller(wallet, safety_buffer=Decimal("0"))
    poller._collateral_balance = Decimal("4.94")
    poller.refresh = AsyncMock(side_effect=AssertionError("unexpected live refresh"))

    executor = OrderExecutor(proxy_wallet=wallet)
    executor._poller = poller

    reserved = await executor.reserve_buy_shares(
        Decimal("10"), Decimal("0.99"), Decimal("5"),
    )

    assert reserved == Decimal("0")
    assert poller.refresh.await_count == 0


def test_buy_balance_failure_normalization():
    assert _is_buy_balance_failure(OrderResult(
        order_id="1", status="insufficient_balance", filled_size="0",
        filled_price=None,
    ))
    assert _is_buy_balance_failure(OrderResult(
        order_id="2", status="failed", filled_size="0", filled_price=None,
        error="not enough balance for order",
    ))
    assert not _is_buy_balance_failure(OrderResult(
        order_id="3", status="failed", filled_size="0", filled_price=None,
        error="tick size mismatch",
    ))

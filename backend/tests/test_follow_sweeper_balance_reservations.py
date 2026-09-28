import asyncio
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from framework.strategy_runtime.balance_poller import BalancePoller  # noqa: E402
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

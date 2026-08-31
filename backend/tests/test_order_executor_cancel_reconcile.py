import asyncio
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from framework.strategy_runtime.order_executor import OrderExecutor  # noqa: E402


def make_executor():
    return OrderExecutor(proxy_wallet="0x" + "0" * 40)


async def fake_sleep(_):
    return None


def test_cancel_reconcile_retries_empty_order_response():
    executor = make_executor()
    responses = [None, {"status": "CANCELED", "size_matched": "2"}]

    async def get_order(order_id):
        return responses.pop(0)

    async def cancel_order(order_id):
        return True

    executor._get_order = get_order
    executor.cancel_order = cancel_order

    with patch("framework.strategy_runtime.order_executor.asyncio.sleep", fake_sleep):
        result = asyncio.run(executor.cancel_order_with_fill_check("order-1"))

    assert result.cancelled is True
    assert result.query_failed is False
    assert result.status == "CANCELED"
    assert result.final_matched == Decimal("2")


def test_cancel_reconcile_fails_closed_after_repeated_errors():
    executor = make_executor()

    async def get_order(order_id):
        raise RuntimeError("query down")

    async def cancel_order(order_id):
        return True

    executor._get_order = get_order
    executor.cancel_order = cancel_order

    with patch("framework.strategy_runtime.order_executor.asyncio.sleep", fake_sleep):
        result = asyncio.run(executor.cancel_order_with_fill_check("order-1"))

    assert result.cancelled is True
    assert result.query_failed is True
    assert result.status == "query_failed"
    assert result.final_matched == Decimal("-1")

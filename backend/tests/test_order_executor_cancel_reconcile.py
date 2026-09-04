import asyncio
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from framework.strategy_runtime.order_executor import OrderExecutor  # noqa: E402
from framework.strategy_runtime.interfaces import CancelResult  # noqa: E402


def make_executor():
    return OrderExecutor(proxy_wallet="0x" + "0" * 40)


async def fake_sleep(_):
    return None


def test_cancel_reconcile_retries_empty_order_response():
    executor = make_executor()
    responses = [None, {"status": "CANCELED", "size_matched": "2"}]

    async def get_order(order_id):
        return responses.pop(0)

    async def cancel_order_detailed(order_id):
        return CancelResult(
            order_id=order_id,
            cancelled=True,
            final_matched=Decimal("-1"),
            status="cancelled",
        )

    executor._get_order = get_order
    executor.cancel_order_detailed = cancel_order_detailed

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

    async def cancel_order_detailed(order_id):
        return CancelResult(
            order_id=order_id,
            cancelled=True,
            final_matched=Decimal("-1"),
            status="cancelled",
        )

    executor._get_order = get_order
    executor.cancel_order_detailed = cancel_order_detailed

    with patch("framework.strategy_runtime.order_executor.asyncio.sleep", fake_sleep):
        result = asyncio.run(executor.cancel_order_with_fill_check("order-1"))

    assert result.cancelled is True
    assert result.query_failed is True
    assert result.status == "query_failed"
    assert result.final_matched == Decimal("-1")
    assert result.query_error_message == "query down"


def test_cancel_order_detailed_preserves_cancel_error_summary():
    executor = make_executor()

    def fail_cancel(wallet, order_id):
        raise RuntimeError('status=503 url=https://clob.polymarket.com/order body={"error":"cancels are disabled"}')

    with patch("framework.strategy_runtime.order_executor._cancel_order", fail_cancel):
        result = asyncio.run(executor.cancel_order_detailed("order-1"))

    assert result.cancelled is False
    assert result.status == "cancel_failed"
    assert result.cancel_error_status_code == 503
    assert result.cancel_error_message == "cancels are disabled"
    assert "cancels are disabled" in result.cancel_error


def test_cancel_reconcile_keeps_cancel_and_query_errors_separate():
    executor = make_executor()

    async def get_order(order_id):
        raise RuntimeError("query down")

    def fail_cancel(wallet, order_id):
        raise RuntimeError('status=503 body={"error":"cancels are disabled"}')

    executor._get_order = get_order

    with patch("framework.strategy_runtime.order_executor._cancel_order", fail_cancel):
        with patch("framework.strategy_runtime.order_executor.asyncio.sleep", fake_sleep):
            result = asyncio.run(executor.cancel_order_with_fill_check("order-1"))

    assert result.cancelled is False
    assert result.query_failed is True
    assert result.cancel_error_message == "cancels are disabled"
    assert result.query_error_message == "query down"

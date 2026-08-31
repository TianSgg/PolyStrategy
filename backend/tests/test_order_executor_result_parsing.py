import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from framework.strategy_runtime.order_executor import OrderExecutor  # noqa: E402


def make_executor():
    return OrderExecutor(proxy_wallet="0x" + "0" * 40)


def test_buy_matched_uses_taker_amount_as_shares():
    result = make_executor()._parse_result(
        "order-1",
        {"status": "matched", "orderID": "clob-1", "takingAmount": "10", "makingAmount": "9.9"},
        0,
        Decimal("10"),
        "BUY",
    )

    assert result.status == "filled"
    assert result.filled_size == "10"
    assert result.clob_taking == "10"
    assert result.clob_making == "9.9"


def test_sell_matched_uses_maker_amount_as_shares():
    result = make_executor()._parse_result(
        "order-1",
        {
            "status": "matched",
            "orderID": "clob-1",
            "takingAmount": "5.04495",
            "makingAmount": "5.05",
        },
        0,
        Decimal("5.05"),
        "SELL",
    )

    assert result.status == "filled"
    assert result.filled_size == "5.05"
    assert result.clob_taking == "5.04495"
    assert result.clob_making == "5.05"


def test_sell_partial_uses_maker_amount_as_shares():
    result = make_executor()._parse_result(
        "order-1",
        {
            "status": "matched",
            "orderID": "clob-1",
            "takingAmount": "2.997",
            "makingAmount": "3",
        },
        0,
        Decimal("5.05"),
        "SELL",
    )

    assert result.status == "partial"
    assert result.filled_size == "3"


def test_matched_response_without_fill_amount_fails():
    result = make_executor()._parse_result(
        "order-1",
        {"status": "matched", "orderID": "clob-1"},
        0,
        Decimal("5"),
        "SELL",
    )

    assert result.status == "failed"
    assert result.filled_size == "0"
    assert "missing fill amount" in result.error

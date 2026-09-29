import asyncio
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from framework.strategy_runtime.order_executor import OrderExecutor  # noqa: E402
from framework.strategy_runtime.tick_size_service import TickSizeFetchError  # noqa: E402
from framework.trading.order import place_limit_order_fast  # noqa: E402


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


def test_place_order_can_skip_failure_triggered_balance_refresh():
    executor = make_executor(FakeTickSizeService())
    executor._poller.refresh = AsyncMock()

    with patch(
        "framework.strategy_runtime.order_executor.place_limit_order",
        return_value={"status": "rejected"},
    ):
        result = asyncio.run(executor.place_order(
            token_id="token",
            side="BUY",
            price="0.99",
            size="5",
            tick_size="0.01",
            neg_risk=True,
            check_balance=False,
            validate_tick_size=False,
            refresh_balance_on_failure=False,
        ))

    assert result.status == "failed"
    executor._poller.refresh.assert_not_awaited()


def test_fast_order_signs_and_posts_without_market_metadata_requests():
    class FakeClient:
        def __init__(self):
            self.order = None
            self.options = None
            self._ClobClient__tick_sizes = {}

        def create_order(self, order, options):
            assert self.get_tick_size(order.token_id) == options.tick_size
            self.order = order
            self.options = options
            return "signed-order"

        def post_order(self, signed, order_type):
            assert signed == "signed-order"
            return {"status": "live", "orderID": "0xorder"}

        def get_tick_size(self, token_id):
            if token_id not in self._ClobClient__tick_sizes:
                raise AssertionError("unexpected tick size API call")
            return self._ClobClient__tick_sizes[token_id]

        def get_neg_risk(self, token_id):
            raise AssertionError("unexpected neg-risk API call")

    client = FakeClient()
    trace = {}
    with patch("framework.trading.order.get_client", return_value=client):
        result = place_limit_order_fast(
            "0xwallet", "token", "BUY", 5.0, 0.99, "0.01", True,
            trace=trace,
        )

    assert result["status"] == "live"
    assert client.options.tick_size == "0.01"
    assert client.options.neg_risk is True
    assert client._ClobClient__tick_sizes["token"] == "0.01"
    assert trace["sign_started_ns"] <= trace["sign_finished_ns"] <= trace["post_finished_ns"]


def test_warmup_primes_sdk_version_and_http_connection():
    calls = []

    class FakeClient:
        def get_server_time(self):
            calls.append("time")

        def _ClobClient__resolve_version(self):
            calls.append("version")

    executor = make_executor(FakeTickSizeService())
    with patch("framework.strategy_runtime.order_executor.get_client", return_value=FakeClient()):
        executor._warmup_sync("0xwallet")

    assert calls == ["time", "version"]


def test_fast_order_preserves_sdk_version_retry_without_extra_metadata_lookup():
    class FakeClient:
        def __init__(self):
            self._ClobClient__tick_sizes = {}
            self.sign_count = 0

        def _retry_on_version_update(self, submit):
            submit()
            return submit()

        def create_order(self, order, options):
            assert self._ClobClient__tick_sizes[order.token_id] == options.tick_size
            self.sign_count += 1
            return f"signed-{self.sign_count}"

        def post_order(self, signed, order_type):
            return {"status": "live", "orderID": signed}

    client = FakeClient()
    with patch("framework.trading.order.get_client", return_value=client):
        result = place_limit_order_fast("0xwallet", "token", "BUY", 5.0, 0.99, "0.01", True)

    assert client.sign_count == 2
    assert result["orderID"] == "signed-2"


def test_fast_executor_skips_tick_and_balance_api_and_records_worker_timing():
    class FailingService:
        async def get(self, token_id, *, max_age_ms=None):
            raise AssertionError("unexpected tick API call")

    executor = OrderExecutor(proxy_wallet="0xwallet", tick_size_service=FailingService())
    trace = {}
    with patch(
        "framework.strategy_runtime.order_executor.place_limit_order_fast",
        return_value={"status": "live", "orderID": "0xorder"},
    ) as place_order:
        result = asyncio.run(executor.place_order(
            token_id="token", side="BUY", price="0.99", size="5",
            tick_size="0.01", neg_risk=True,
            check_balance=False, validate_tick_size=False,
            refresh_balance_on_failure=False, fast=True, trace=trace,
        ))

    assert result.status == "live"
    assert place_order.call_count == 1
    assert trace["executor_submitted_ns"] <= trace["worker_started_ns"] <= trace["executor_returned_ns"]

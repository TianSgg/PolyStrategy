import asyncio
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from framework.strategy_runtime.interfaces import CancelResult, OrderResult  # noqa: E402
from framework.strategy_runtime.tick_verifier import TickVerifyResult  # noqa: E402
from framework.user_ws import FillEvent  # noqa: E402
from strategy_weather_sweep.internal.sell_failure import SellErrorCircuitBreaker  # noqa: E402
from strategy_weather_sweep.service import SweepStrategy, SweepTrade  # noqa: E402


class FakeEventLogger:
    event_id = "event-1"

    def __init__(self):
        self.steps = []

    def log_step(self, step, detail, phase="entry"):
        self.steps.append((phase, step, detail))

    def end_event(self):
        pass


class FakeUserWS:
    def watch_order(self, **kwargs):
        pass

    def unwatch_order(self, order_id):
        pass


class FakeExecutor:
    available_cash = Decimal("1000")

    def __init__(
        self, sell_result=None, sell_results=None, final_matched=Decimal("0"),
        cancel_query_failed=False, cancel_cancelled=True,
    ):
        self.sell_result = sell_result
        self.sell_results = list(sell_results) if sell_results is not None else None
        self.final_matched = final_matched
        self.cancel_query_failed = cancel_query_failed
        self.cancel_cancelled = cancel_cancelled
        self.user_ws = FakeUserWS()
        self.placed_orders = []

    async def ensure_user_ws(self):
        return self.user_ws

    async def place_order(self, **kwargs):
        self.placed_orders.append(kwargs)
        if self.sell_results is not None:
            return self.sell_results.pop(0)
        return self.sell_result

    async def cancel_order_with_fill_check(self, order_id):
        return CancelResult(
            order_id=order_id,
            cancelled=self.cancel_cancelled,
            final_matched=self.final_matched,
            status="query_failed" if self.cancel_query_failed else "CANCELED",
            query_failed=self.cancel_query_failed,
        )


class FakeTickVerifier:
    async def verify(self, token_id):
        return TickVerifyResult(confirmed=True, actual_tick=Decimal("0.001"))


class FakeTickSizeService:
    def __init__(self, tick_size=Decimal("0.001")):
        self.tick_size = tick_size
        self.refresh_count = 0

    async def get(self, token_id, *, max_age_ms=None):
        return self.tick_size

    async def refresh(self, token_id):
        self.refresh_count += 1
        return self.tick_size

    def invalidate(self, token_id):
        return None


def make_trade(
    sell_result=None, sell_results=None, final_matched=Decimal("0"), on_exit_failed=None,
    cancel_query_failed=False, cancel_cancelled=True,
):
    executor = FakeExecutor(
        sell_result=sell_result, sell_results=sell_results,
        final_matched=final_matched,
        cancel_query_failed=cancel_query_failed,
        cancel_cancelled=cancel_cancelled,
    )
    event_logger = FakeEventLogger()
    closed = []
    trade = SweepTrade(
        token_id="token",
        market_slug="market",
        config={},
        executor=executor,
        orderbook_ws=None,
        event_logger=event_logger,
        on_closed=closed.append,
        on_exit_failed=on_exit_failed,
        tick_size_service=FakeTickSizeService(),
    )
    trade.tick_verifier = FakeTickVerifier()
    trade.buy_price = Decimal("0.99")
    trade._order_size = Decimal("20")
    trade._order_placed_ms = 0
    return trade, executor, event_logger, closed


def run(coro):
    return asyncio.run(coro)


async def no_sleep(_):
    return None


def close_reason(event_logger):
    for _, step, detail in event_logger.steps:
        if step in ("event_closed", "exit_failed"):
            return detail["reason"]
    return None


def test_zero_fill_waits_for_entry_timeout_after_tick_change():
    trade, executor, event_logger, closed = make_trade()
    trade.entry_order_id = "buy-1"

    run(trade._on_tick_change(Decimal("0.001")))

    assert trade.state == "entry_working"
    assert trade._tick_verified is True
    assert closed == []
    assert ("monitor", "normal_exit_deferred") in [
        (phase, step) for phase, step, _ in event_logger.steps
    ]

    run(trade._entry_timeout(0))

    assert trade.state == "closed"
    assert closed == ["token"]
    assert close_reason(event_logger) == "timeout_no_fill"
    assert ("entry", "event_closed") in [
        (phase, step) for phase, step, _ in event_logger.steps
    ]


def test_partial_fill_uses_normal_exit_after_entry_timeout():
    sell_result = OrderResult(
        order_id="sell-1",
        status="filled",
        filled_size="15",
        clob_status="matched",
        clob_taking="14.985",
        clob_making="15",
    )
    trade, executor, event_logger, closed = make_trade(
        sell_result=sell_result, final_matched=Decimal("15")
    )
    trade.entry_order_id = "buy-1"
    trade.position_shares = Decimal("15")

    run(trade._on_tick_change(Decimal("0.001")))
    assert trade.state == "entry_working"

    run(trade._entry_timeout(0))

    assert trade.state == "closed"
    assert closed == ["token"]
    assert executor.placed_orders[0]["price"] == "0.999"
    assert executor.placed_orders[0]["tick_size"] == "0.001"
    assert close_reason(event_logger) == "normal_exit"
    assert ("exit", "event_closed") in [
        (phase, step) for phase, step, _ in event_logger.steps
    ]


def test_full_fill_before_tick_waits_for_tick():
    trade, executor, event_logger, closed = make_trade()
    trade.position_shares = Decimal("20")

    run(trade._record_entry_complete("buy-1"))

    assert trade.state == "exit_working"
    assert executor.placed_orders == []
    assert closed == []


def test_full_fill_after_tick_starts_normal_exit():
    sell_result = OrderResult(
        order_id="sell-1",
        status="filled",
        filled_size="20",
        clob_status="matched",
        clob_taking="19.98",
        clob_making="20",
    )
    trade, executor, event_logger, closed = make_trade(sell_result=sell_result)
    trade.entry_order_id = "buy-1"
    trade._tick_verified = True
    trade._tick_size = Decimal("0.001")

    fill = FillEvent(
        order_id="buy-1",
        token_id="token",
        side="BUY",
        fill_size=Decimal("20"),
        fill_price=Decimal("0.99"),
        total_matched=Decimal("20"),
        trade_id=None,
        source="ws_order_update",
        timestamp_ms=0,
    )
    run(trade._on_buy_fill(fill))

    assert trade.state == "closed"
    assert closed == ["token"]
    assert executor.placed_orders[0]["price"] == "0.999"
    assert executor.placed_orders[0]["tick_size"] == "0.001"
    assert close_reason(event_logger) == "normal_exit"


def test_invalid_tick_sell_refreshes_once_then_stops():
    sell_result = OrderResult(
        order_id="sell-failed",
        status="failed",
        filled_size="0",
        error="invalid tick size (0.001), minimum for the market is 0.01",
    )
    trade, executor, event_logger, closed = make_trade(sell_result=sell_result)
    trade.position_shares = Decimal("10")
    trade._tick_size = Decimal("0.01")

    run(trade._start_normal_exit())

    assert len(executor.placed_orders) == 2
    assert executor.placed_orders[0]["price"] == "0.999"
    assert executor.placed_orders[0]["tick_size"] == "0.001"
    assert executor.placed_orders[1]["price"] == "0.999"
    assert executor.placed_orders[1]["tick_size"] == "0.001"
    assert trade.state == "closed"
    assert closed == ["token"]
    assert close_reason(event_logger) == "sell_failed"
    exit_failed = next(
        detail for _, step, detail in event_logger.steps if step == "exit_failed"
    )
    assert exit_failed["position_open"] is True
    assert exit_failed["position_shares"] == "10"
    assert exit_failed["manual_action_required"] is True
    assert exit_failed["failure_reason"] == "sell_placement_failed"

    steps = {(step, detail.get("stop_reason")) for _, step, detail in event_logger.steps}
    assert ("tick_refreshed", None) in steps
    assert ("sell_retry_exhausted", "invalid_tick_retry_exhausted") in steps


def test_insufficient_balance_retries_once_for_settlement():
    failed = OrderResult(
        order_id="sell-settlement",
        status="failed",
        filled_size="0",
        error="not enough balance / allowance",
    )
    success = OrderResult(
        order_id="sell-success",
        status="filled",
        filled_size="10",
        clob_status="matched",
        clob_taking="9.99",
        clob_making="10",
    )
    trade, executor, event_logger, closed = make_trade(
        sell_results=[failed, success],
    )
    trade.position_shares = Decimal("10")
    trade._tick_size = Decimal("0.01")

    with patch("strategy_weather_sweep.service.asyncio.sleep", no_sleep):
        run(trade._start_normal_exit())

    assert trade.state == "closed"
    assert closed == ["token"]
    assert len(executor.placed_orders) == 2
    assert ("exit", "balance_settlement_retry") in [
        (phase, step) for phase, step, _ in event_logger.steps
    ]
    assert ("exit", "event_closed") in [
        (phase, step) for phase, step, _ in event_logger.steps
    ]


def test_force_exit_reconciles_sell_fill_and_closes():
    trade, _, event_logger, closed = make_trade(final_matched=Decimal("10"))
    trade.position_shares = Decimal("10")
    trade.exit_order_id = "sell-1"
    trade.sell_price = Decimal("0.999")

    run(trade.force_exit("config_disabled"))

    assert trade.position_shares == Decimal("0")
    assert trade.state == "closed"
    assert closed == ["token"]
    assert close_reason(event_logger) == "force_exit"
    assert ("exit_force", "fill_reconcile") in [
        (phase, step) for phase, step, _ in event_logger.steps
    ]


def test_force_exit_with_open_position_uses_exit_failed():
    trade, _, event_logger, closed = make_trade(final_matched=Decimal("0"))
    trade.position_shares = Decimal("10")
    trade.exit_order_id = "sell-1"
    trade.sell_price = Decimal("0.999")

    run(trade.force_exit("config_disabled"))

    assert trade.position_shares == Decimal("10")
    assert trade.state == "closed"
    assert closed == ["token"]
    assert close_reason(event_logger) == "force_exit"
    exit_failed = next(
        detail for _, step, detail in event_logger.steps if step == "exit_failed"
    )
    assert exit_failed["position_open"] is True
    assert exit_failed["position_shares"] == "10"
    assert exit_failed["manual_action_required"] is True
    assert exit_failed["failure_reason"] == "exit_order_unfilled"


def test_sell_complete_uses_exit_fill_accumulator():
    trade, _, event_logger, _ = make_trade()
    trade.position_shares = Decimal("20")
    trade._order_size = Decimal("20")

    trade._record_sell_fill(
        "sell-1", Decimal("7.5"), Decimal("0.999"), source="clob_response",
    )
    trade._record_sell_fill(
        "sell-1", Decimal("2.5"), Decimal("0.999"), source="ws_order_update",
    )
    trade._record_sell_complete("sell-1", 2, 0)

    complete = next(
        detail for _, step, detail in event_logger.steps if step == "sell_complete"
    )
    assert Decimal(complete["total_filled"]) == Decimal("10")
    assert Decimal(complete["remaining_position"]) == Decimal("10")
    assert trade._exit_filled_shares == Decimal("10")


def test_entry_timeout_cancel_query_failure_fails_closed():
    trade, _, event_logger, closed = make_trade(
        final_matched=Decimal("-1"), cancel_query_failed=True,
    )
    trade.entry_order_id = "buy-1"

    run(trade._entry_timeout(0))

    assert trade.state == "closed"
    assert closed == ["token"]
    reconcile_failed = next(
        detail for _, step, detail in event_logger.steps
        if step == "exit_reconcile_failed"
    )
    assert reconcile_failed["side"] == "BUY"
    assert reconcile_failed["query_status"] == "query_failed"
    exit_failed = next(
        detail for _, step, detail in event_logger.steps if step == "exit_failed"
    )
    assert exit_failed["query_status"] == "query_failed"
    assert exit_failed["failure_reason"] == "exit_reconcile_failed"


def test_entry_timeout_cancel_failure_fails_closed():
    trade, _, event_logger, closed = make_trade(cancel_cancelled=False)
    trade.entry_order_id = "buy-1"

    run(trade._entry_timeout(0))

    assert trade.state == "closed"
    assert closed == ["token"]
    assert ("entry", "cancel_failed") in [
        (phase, step) for phase, step, _ in event_logger.steps
    ]
    exit_failed = next(
        detail for _, step, detail in event_logger.steps if step == "exit_failed"
    )
    assert exit_failed["failure_reason"] == "cancel_failed"


def test_sell_failure_reason_detects_parse_error():
    result = OrderResult(
        order_id="sell-1",
        status="failed",
        filled_size="0",
        error="matched CLOB response missing fill amount: {}",
    )

    assert SweepTrade._sell_failure_reason(result) == "sell_fill_parse_error"
    assert SweepTrade._sell_failure_reason(None) == "sell_placement_failed"


def test_strategy_pauses_after_three_exit_failures():
    class FakeConfigDAO:
        def __init__(self):
            self.updates = []

        def update(self, config_id, data):
            self.updates.append((config_id, data))
            return True

    strategy = SweepStrategy()
    strategy._config_id = 7
    strategy._config_dao = FakeConfigDAO()
    strategy._sell_error_breaker = SellErrorCircuitBreaker()
    strategy._sell_error_window_sec = 300.0

    assert strategy._on_exit_failed("token-1", "network_timeout") is None
    assert strategy._on_exit_failed("token-2", "network_timeout") is None
    pause_detail = strategy._on_exit_failed("token-3", "network_timeout")

    assert strategy._draining is True
    assert strategy._config_dao.updates == [(7, {"enabled": 0})]
    assert pause_detail == {
        "error_signature": "network_timeout",
        "event_count": 3,
        "window_sec": 300,
        "config_disabled": True,
        "action": "pause_strategy",
    }


def test_exit_failure_reports_strategy_pause_event():
    def on_exit_failed(token_id, error_signature):
        return {
            "error_signature": error_signature,
            "event_count": 3,
            "window_sec": 300,
            "config_disabled": True,
            "action": "pause_strategy",
        }

    trade, _, event_logger, closed = make_trade(on_exit_failed=on_exit_failed)
    trade.position_shares = Decimal("10")

    trade._close_exit_failed("sell_failed", extra={
        "error": "timeout",
        "error_signature": "network_timeout",
    })

    assert trade.state == "closed"
    assert closed == ["token"]
    pause_detail = next(
        detail for _, step, detail in event_logger.steps if step == "strategy_paused"
    )
    assert pause_detail["reason"] == "sell_error_circuit_breaker"
    assert pause_detail["error_signature"] == "network_timeout"
    assert pause_detail["config_disabled"] is True

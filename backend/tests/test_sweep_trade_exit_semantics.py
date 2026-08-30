import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from framework.strategy_runtime.interfaces import CancelResult, OrderResult  # noqa: E402
from framework.strategy_runtime.tick_verifier import TickVerifyResult  # noqa: E402
from framework.user_ws import FillEvent  # noqa: E402
from strategy_weather_sweep.service import SweepTrade  # noqa: E402


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

    def __init__(self, sell_result=None, final_matched=Decimal("0")):
        self.sell_result = sell_result
        self.final_matched = final_matched
        self.user_ws = FakeUserWS()
        self.placed_orders = []

    async def ensure_user_ws(self):
        return self.user_ws

    async def place_order(self, **kwargs):
        self.placed_orders.append(kwargs)
        return self.sell_result

    async def cancel_order_with_fill_check(self, order_id):
        return CancelResult(
            order_id=order_id,
            cancelled=True,
            final_matched=self.final_matched,
            status="CANCELED",
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


def make_trade(sell_result=None, final_matched=Decimal("0")):
    executor = FakeExecutor(sell_result=sell_result, final_matched=final_matched)
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
        tick_size_service=FakeTickSizeService(),
    )
    trade.tick_verifier = FakeTickVerifier()
    trade.buy_price = Decimal("0.99")
    trade._order_size = Decimal("20")
    trade._order_placed_ms = 0
    return trade, executor, event_logger, closed


def run(coro):
    return asyncio.run(coro)


def close_reason(event_logger):
    for _, step, detail in event_logger.steps:
        if step == "event_closed":
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
        clob_taking="15",
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
        clob_taking="20",
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

    steps = {(step, detail.get("stop_reason")) for _, step, detail in event_logger.steps}
    assert ("tick_refreshed", None) in steps
    assert ("sell_retry_exhausted", "invalid_tick_retry_exhausted") in steps

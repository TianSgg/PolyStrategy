import asyncio
import sys
import time
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from framework.strategy_runtime.interfaces import CancelResult, OrderResult  # noqa: E402
from framework.strategy_runtime.interfaces import Signal  # noqa: E402
from framework.strategy_runtime.tick_verifier import TickVerifyResult  # noqa: E402
from framework.user_ws import FillEvent  # noqa: E402
from strategy_weather_sweep.internal.sell_failure import SellErrorCircuitBreaker  # noqa: E402
from strategy_weather_sweep.service import SweepStrategy, SweepTrade  # noqa: E402


class FakeEventLogger:
    event_id = "event-1"

    def __init__(self):
        self.steps = []
        self._config_id = 1
        self._owner_user_id = 1
        self._proxy_wallet = "wallet"

    def start_event(self, **kwargs):
        self.started = kwargs

    def log_step(self, step, detail, phase="entry"):
        self.steps.append((phase, step, detail))

    def end_event(self):
        pass


class FakeTradeDAO:
    def __init__(self):
        self.inserts = []
        self.updates = []

    def insert(self, data):
        self.inserts.append(data)
        return 1

    def update_by_event_id(self, event_id, data):
        self.updates.append((event_id, data))
        return True


class FakeUserWS:
    def __init__(self):
        self.watches = {}
        self.unwatched = []

    def watch_order(self, **kwargs):
        self.watches[kwargs["order_id"]] = kwargs

    def unwatch_order(self, order_id):
        self.unwatched.append(order_id)
        self.watches.pop(order_id, None)

    async def emit_cancel(self, order_id, size_matched=Decimal("0")):
        watch = self.watches[order_id]
        await watch["on_cancel"](
            type("Cancel", (), {
                "order_id": order_id,
                "size_matched": Decimal(str(size_matched)),
            })()
        )

    async def emit_fill(
        self,
        order_id,
        fill_size,
        total_matched,
        price=Decimal("0.999"),
    ):
        watch = self.watches[order_id]
        await watch["on_fill"](FillEvent(
            order_id=order_id,
            token_id=watch["token_id"],
            side="SELL",
            fill_size=Decimal(str(fill_size)),
            fill_price=Decimal(str(price)),
            total_matched=Decimal(str(total_matched)),
            trade_id=None,
            source="ws_order_update",
            timestamp_ms=0,
        ))


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
        self.cancelled_orders = []

    async def ensure_user_ws(self):
        return self.user_ws

    async def cancel_order(self, order_id):
        self.cancelled_orders.append(order_id)
        return self.cancel_cancelled

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
    async def verify(self, token_id, ws_tick_size=None, ws_tick_size_getter=None):
        if ws_tick_size_getter:
            ws_tick_size = ws_tick_size_getter()
        return TickVerifyResult(
            confirmed=True,
            actual_tick=Decimal("0.001"),
            ws_tick_size=ws_tick_size,
            tick_api_size=Decimal("0.001"),
            book_tick_size=Decimal("0.001"),
        )


class FakeTickSizeService:
    def __init__(self, tick_size=Decimal("0.001")):
        self.tick_size = tick_size
        self.refresh_count = 0

    async def get(self, token_id, *, max_age_ms=None):
        return self.tick_size

    async def refresh(self, token_id):
        self.refresh_count += 1
        return self.tick_size

    async def refresh_consensus(self, token_id, ws_tick_size):
        self.refresh_count += 1
        return self.tick_size

    def invalidate(self, token_id):
        return None


class FakeOrderBookWS:
    def __init__(self, min_order_size=None, tick_size=Decimal("0.001")):
        self.min_order_size = min_order_size
        self.tick_size = tick_size

    def get_min_order_size(self, token_id):
        return self.min_order_size

    def get_tick_size(self, token_id):
        return self.tick_size

    def get_book(self, token_id):
        return None


class FakeBookBboClient:
    def __init__(self, pre_bbo=None, aft_bbo=None):
        self.pre_bbo = pre_bbo or {
            "status": "ok",
            "source": "clob_book_api",
            "response_at_ms": 101,
            "best_bid": 0.98,
            "best_ask": 0.99,
        }
        self.aft_bbo = aft_bbo or {
            "status": "ok",
            "source": "clob_book_api",
            "response_at_ms": 202,
            "best_bid": 0.97,
            "best_ask": 0.99,
        }
        self.calls = []

    async def fetch_bbo(self, token_id, offset_origin_ms):
        self.calls.append((token_id, offset_origin_ms))
        return self.aft_bbo if len(self.calls) > 1 else self.pre_bbo


class MismatchTickVerifier:
    async def verify(self, token_id, ws_tick_size=None, ws_tick_size_getter=None):
        if ws_tick_size_getter:
            ws_tick_size = ws_tick_size_getter()
        return TickVerifyResult(
            confirmed=False,
            ws_tick_size=ws_tick_size,
            tick_api_size=Decimal("0.001"),
            book_tick_size=Decimal("0.001"),
            error="tick_source_mismatch",
        )


def make_trade(
    sell_result=None, sell_results=None, final_matched=Decimal("0"), on_exit_failed=None,
    cancel_query_failed=False, cancel_cancelled=True,
    orderbook_ws=None, trade_dao=None,
    book_bbo_client=None,
):
    orderbook_ws = orderbook_ws or FakeOrderBookWS()
    executor = FakeExecutor(
        sell_result=sell_result, sell_results=sell_results,
        final_matched=final_matched,
        cancel_query_failed=cancel_query_failed,
        cancel_cancelled=cancel_cancelled,
    )
    event_logger = FakeEventLogger()
    closed = []
    config = {"clob_sync_grace_ms": 0}
    book_bbo_client = book_bbo_client or FakeBookBboClient()
    trade = SweepTrade(
        token_id="token",
        market_slug="market",
        config=config,
        executor=executor,
        orderbook_ws=orderbook_ws,
        event_logger=event_logger,
        on_closed=closed.append,
        on_exit_failed=on_exit_failed,
        trade_dao=trade_dao,
        tick_size_service=FakeTickSizeService(),
        book_bbo_client=book_bbo_client,
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
        if step == "event_closed":
            return detail["reason"]
    return None


def test_trade_summary_writes_phase_and_close_reason_fields():
    trade_dao = FakeTradeDAO()
    trade, _, event_logger, closed = make_trade(trade_dao=trade_dao)
    trade.position_shares = Decimal("10")

    trade._close_exit_failed("sell_placement_failed", extra={
        "error": "timeout",
    })

    assert trade.state == "closed"
    assert closed == ["token"]
    event_id, update = trade_dao.updates[-1]
    assert event_id == "event-1"
    assert update["phase"] == "closed"
    assert update["close_reason"] == "sell_placement_failed"
    assert update["pnl"] is None
    assert update["pnl_pct"] is None
    assert update["duration_ms"] >= 0
    assert update["closed_at"] is not None
    event_closed = next(
        detail for _, step, detail in event_logger.steps if step == "event_closed"
    )
    assert event_closed["close_reason"] == "sell_placement_failed"
    assert event_closed["error"] == "timeout"


def test_no_cash_entry_is_skipped_not_failed():
    trade_dao = FakeTradeDAO()
    trade, executor, event_logger, closed = make_trade(trade_dao=trade_dao)
    executor.available_cash = Decimal("0")
    signal = Signal(
        signal_id="signal-1",
        signal_type="sweep",
        token_id="token",
        market_slug="market",
        occurred_at_ms=0,
        source="test",
        payload={"event_slug": "event", "city": "city", "direction": "highest"},
    )

    run(trade.enter(signal))

    assert closed == ["token"]
    event_id, update = trade_dao.updates[-1]
    assert event_id == "event-1"
    assert update["phase"] == "closed"
    assert update["close_reason"] == "no_cash"
    assert ("entry", "buy_order_skipped") in [
        (phase, step) for phase, step, _ in event_logger.steps
    ]
    event_closed = next(
        detail for _, step, detail in event_logger.steps if step == "event_closed"
    )
    assert event_closed["close_reason"] == "no_cash"


def test_strategy_ignores_market_resolved_signal():
    strategy = SweepStrategy()
    strategy._draining = False

    class FakeTrade:
        market_slug = "market"

    strategy._trades = {"token": FakeTrade()}
    signal = Signal(
        signal_id="market-resolved-1",
        signal_type="market_resolved",
        token_id="yes-token",
        market_slug="market",
        occurred_at_ms=0,
        source="weather_orderbook",
    )

    run(strategy.on_signal(signal))

    assert strategy.active_trade_count == 1
    assert strategy.active_tokens == ["token"]


def test_insufficient_balance_entry_is_skipped_not_failed():
    trade_dao = FakeTradeDAO()
    book_bbo_client = FakeBookBboClient()
    trade, executor, event_logger, closed = make_trade(
        trade_dao=trade_dao,
        sell_result=OrderResult(
            order_id="buy-1",
            status="insufficient_balance",
            filled_size="0",
        ),
        book_bbo_client=book_bbo_client,
    )
    signal = Signal(
        signal_id="signal-1",
        signal_type="sweep",
        token_id="token",
        market_slug="market",
        occurred_at_ms=0,
        source="test",
        payload={"event_slug": "event", "city": "city", "direction": "highest"},
    )

    run(trade.enter(signal))

    assert closed == ["token"]
    _, update = trade_dao.updates[-1]
    assert update["phase"] == "closed"
    assert update["close_reason"] == "no_cash"
    assert ("entry", "buy_order_skipped") in [
        (phase, step) for phase, step, _ in event_logger.steps
    ]
    assert ("entry", "buy_order_failed") not in [
        (phase, step) for phase, step, _ in event_logger.steps
    ]
    assert len(book_bbo_client.calls) == 1
    assert not any(step == "bbo_snapshot" for _, step, _ in event_logger.steps)


def test_rejected_buy_does_not_wait_for_unused_pre_bbo():
    class BlockingBboClient:
        def __init__(self):
            self.cancelled = False

        async def fetch_bbo(self, token_id, offset_origin_ms):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    bbo_client = BlockingBboClient()
    trade, _, event_logger, closed = make_trade(book_bbo_client=bbo_client)
    trade._executor.sell_result = OrderResult(
        order_id="buy-1",
        status="failed",
        filled_size="0",
        error="HTTP 500",
    )
    signal = Signal(
        signal_id="signal-1",
        signal_type="sweep",
        token_id="token",
        market_slug="market",
        occurred_at_ms=0,
        source="test",
        payload={"event_slug": "event", "city": "city", "direction": "highest"},
    )

    run(asyncio.wait_for(trade.enter(signal), timeout=1))

    assert bbo_client.cancelled is True
    assert closed == ["token"]
    assert any(step == "buy_order_failed" for _, step, _ in event_logger.steps)


def test_buy_order_placed_contains_compact_bbo_snapshots():
    trade_dao = FakeTradeDAO()
    book_bbo_client = FakeBookBboClient()
    trade, executor, event_logger, closed = make_trade(
        trade_dao=trade_dao,
        sell_result=OrderResult(
            order_id="buy-1",
            status="live",
            filled_size="0",
            clob_status="live",
        ),
        book_bbo_client=book_bbo_client,
    )
    signal = Signal(
        signal_id="signal-1",
        signal_type="sweep",
        token_id="token",
        market_slug="market",
        occurred_at_ms=0,
        source="test",
        payload={"event_slug": "event", "city": "city", "direction": "highest"},
    )

    run(trade.enter(signal))

    assert len(book_bbo_client.calls) == 2
    assert all(call[0] == "token" for call in book_bbo_client.calls)
    buy_step = next(
        detail for _, step, detail in event_logger.steps if step == "buy_order_placed"
    )
    assert set(buy_step["pre_bbo"]) == {
        "utc", "best_ask", "best_ask_size", "best_bid", "best_bid_size",
        "offset_ms", "tick_size",
    }
    assert set(buy_step["aft_bbo"]) == set(buy_step["pre_bbo"])
    assert not any(step == "bbo_snapshot" for _, step, _ in event_logger.steps)


def test_order_offset_uses_order_response_timestamp():
    trade, _, _, _ = make_trade()

    order = trade._order_obj(
        "buy-1", "BUY", "0.99", "20", 1000, occurred_at_ms=1056,
    )

    assert order["offset_ms"] == 56


def test_risk_started_records_first_bbo_timeout():
    trade, _, event_logger, _ = make_trade()

    class FakeRisk:
        async def wait_for_first_bbo(self):
            return False

        def first_bbo_snapshot(self):
            return None

    trade.risk = FakeRisk()

    run(trade._log_risk_started(
        phase="entry",
        enter_origin_ms=0,
        order_id="buy-1",
        side="BUY",
        price=Decimal("0.99"),
        size=Decimal("10"),
    ))

    assert ("entry", "first_bbo_ready") not in [
        (phase, step) for phase, step, _ in event_logger.steps
    ]
    assert ("entry", "first_bbo_timeout") not in [
        (phase, step) for phase, step, _ in event_logger.steps
    ]
    assert ("entry", "risk_started") in [
        (phase, step) for phase, step, _ in event_logger.steps
    ]
    risk_step = next(
        detail for _, step, detail in event_logger.steps if step == "risk_started"
    )
    assert risk_step["risk"]["status"] == "not_started"
    assert risk_step["first_bbo"]["status"] == "timeout"


def test_zero_fill_waits_for_entry_timeout_after_tick_change():
    trade, executor, event_logger, closed = make_trade()
    trade.entry_order_id = "buy-1"

    run(trade._on_tick_change(Decimal("0.001")))

    assert trade.state == "entry"
    assert trade._tick_verified is True
    assert closed == []
    assert ("entry", "tick_detect") not in [
        (phase, step) for phase, step, _ in event_logger.steps
    ]
    tick_verified = [
        detail for _, step, detail in event_logger.steps if step == "tick_verified"
    ]
    assert len(tick_verified) == 3
    assert [item["source"] for item in tick_verified] == [
        "market_ws", "tick_size_api", "book_api"
    ]
    assert all(item["token_id"] == "token" for item in tick_verified)
    assert all(item["tick_size"] == "0.001" for item in tick_verified)
    assert ("entry", "exit_trigger_deferred") in [
        (phase, step) for phase, step, _ in event_logger.steps
    ]

    run(trade._entry_timeout(0))

    assert trade.state == "closed"
    assert closed == ["token"]
    assert close_reason(event_logger) == "timeout_no_fill"
    assert ("entry", "event_closed") in [
        (phase, step) for phase, step, _ in event_logger.steps
    ]


def test_http_tick_callback_does_not_masquerade_as_market_ws():
    trade, _, event_logger, _ = make_trade(orderbook_ws=FakeOrderBookWS(tick_size=None))
    trade.tick_verifier = MismatchTickVerifier()

    run(trade._on_tick_change(Decimal("0.001"), source="tick_size_api"))

    assert trade._tick_verified is False
    assert ("entry", "tick_detect") not in [
        (phase, step) for phase, step, _ in event_logger.steps
    ]
    tick_verify_failed = next(
        detail for _, step, detail in event_logger.steps if step == "tick_verify_failed"
    )
    assert tick_verify_failed["ws_tick_size"] is None
    assert tick_verify_failed["http_tick_size"] == "0.001"
    assert tick_verify_failed["book_tick_size"] == "0.001"
    assert tick_verify_failed["error"] == "tick_source_mismatch"


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
    assert trade.state == "entry"

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

    assert trade.state == "entry"
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


def test_first_sell_waits_for_clob_sync_grace():
    sell_result = OrderResult(
        order_id="sell-1",
        status="filled",
        filled_size="10",
        clob_status="matched",
        clob_taking="9.99",
        clob_making="10",
    )
    trade, executor, event_logger, closed = make_trade(sell_result=sell_result)
    trade._config["clob_sync_grace_ms"] = 2000
    trade.position_shares = Decimal("10")
    trade._tick_size = Decimal("0.001")
    trade._last_buy_fill_ms = int(time.time() * 1000)

    sleeps = []

    async def record_sleep(delay):
        sleeps.append(delay)

    with patch("strategy_weather_sweep.service.asyncio.sleep", record_sleep):
        run(trade._start_normal_exit())

    assert trade.state == "closed"
    assert closed == ["token"]
    assert sleeps and sleeps[0] > 0
    assert sleeps[0] <= 2.0
    assert ("exit", "exit_trigger_deferred") in [
        (phase, step) for phase, step, _ in event_logger.steps
    ]


def test_normal_sell_stays_live_until_fill_or_cancellation():
    live = OrderResult(
        order_id="sell-live",
        status="live",
        filled_size="0",
        clob_status="live",
    )
    trade, executor, event_logger, closed = make_trade(sell_result=live)
    trade.position_shares = Decimal("10")
    trade._tick_size = Decimal("0.01")

    async def scenario():
        task = asyncio.create_task(trade._start_normal_exit())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not task.done()
        assert executor.placed_orders[0]["price"] == "0.999"
        assert executor.user_ws.watches["sell-live"]["on_cancel"]
        await executor.user_ws.emit_cancel("sell-live", Decimal("0"))
        await asyncio.sleep(0)
        assert not task.done()
        assert len(executor.placed_orders) == 2
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    run(scenario())
    assert closed == []
    assert ("exit", "sell_cancelled") in [
        (phase, step) for phase, step, _ in event_logger.steps
    ]


def test_balance_lag_partial_sell_order_completes_then_retries_remaining_position():
    failed = OrderResult(
        order_id="sell-settlement",
        status="failed",
        filled_size="0",
        error=(
            "not enough balance / allowance: the balance is not enough "
            "-> balance: 10900000, order amount: 20000000"
        ),
    )
    partial_live = OrderResult(
        order_id="sell-partial",
        status="live",
        filled_size="0",
        clob_status="live",
    )
    remaining_filled = OrderResult(
        order_id="sell-remaining",
        status="filled",
        filled_size="9.1",
        clob_status="matched",
        clob_taking="9.0909",
        clob_making="9.1",
    )
    trade_dao = FakeTradeDAO()
    trade, executor, event_logger, closed = make_trade(
        sell_results=[failed, partial_live, remaining_filled],
        orderbook_ws=FakeOrderBookWS(min_order_size=Decimal("0.01")),
        trade_dao=trade_dao,
    )
    trade.position_shares = Decimal("20")
    trade._tick_size = Decimal("0.001")

    async def scenario():
        task = asyncio.create_task(trade._start_normal_exit())
        for _ in range(10):
            await asyncio.sleep(0)
            if "sell-partial" in executor.user_ws.watches:
                break
        else:
            raise AssertionError("partial SELL order was not watched")

        await executor.user_ws.emit_fill(
            "sell-partial",
            fill_size=Decimal("10.9"),
            total_matched=Decimal("10.9"),
        )
        await task

    run(scenario())

    assert [order["size"] for order in executor.placed_orders] == ["20", "10.90", "9.1"]
    assert trade.state == "closed"
    assert closed == ["token"]
    assert close_reason(event_logger) == "normal_exit"
    assert "sell-partial" in executor.user_ws.unwatched
    assert trade.position_shares == Decimal("0")

    placed_sizes = [
        detail["order"]["size"]
        for _, step, detail in event_logger.steps
        if step == "sell_order_placed"
    ]
    assert placed_sizes == ["10.90", "9.1"]

    exit_order_updates = [
        data["exit_order_size"]
        for _, data in trade_dao.updates
        if "exit_order_size" in data
    ]
    assert exit_order_updates == ["20", "10.90", "9.1"]


def test_risk_exit_cancels_orders_and_uses_floor_price_without_tick_validation():
    sell_result = OrderResult(
        order_id="risk-sell",
        status="filled",
        filled_size="10",
        clob_status="matched",
    )
    trade, executor, event_logger, closed = make_trade(sell_result=sell_result)
    trade.entry_order_id = "buy-pending"
    trade.position_shares = Decimal("10")
    trade._tick_size = Decimal("0.001")

    run(trade._risk_exit("stop_loss"))

    assert executor.cancelled_orders == ["buy-pending"]
    assert len(executor.placed_orders) == 1
    assert executor.placed_orders[0]["price"] == "0.01"
    assert executor.placed_orders[0]["validate_tick_size"] is False
    assert trade._tick_size_service.refresh_count == 0
    assert close_reason(event_logger) == "stop_loss"
    assert closed == ["token"]
    assert ("exit", "fill_reconcile_failed") not in [
        (phase, step) for phase, step, _ in event_logger.steps
    ]


def test_risk_exit_stops_after_one_floor_sell_failure_without_reconciliation():
    failed = OrderResult(
        order_id="risk-sell-failed",
        status="failed",
        filled_size="0",
        error="network timeout",
    )
    trade, executor, event_logger, closed = make_trade(sell_result=failed)
    trade.position_shares = Decimal("10")

    run(trade._risk_exit("stop_loss"))

    assert len(executor.placed_orders) == 1
    assert executor.placed_orders[0]["price"] == "0.01"
    assert executor.placed_orders[0]["validate_tick_size"] is False
    assert trade._tick_size_service.refresh_count == 0
    assert closed == ["token"]
    assert close_reason(event_logger) == "sell_placement_failed"
    assert ("exit", "fill_reconcile_failed") not in [
        (phase, step) for phase, step, _ in event_logger.steps
    ]


def test_invalid_tick_sell_uses_dedicated_long_retry_waits():
    sell_result = OrderResult(
        order_id="sell-failed",
        status="failed",
        filled_size="0",
        error="invalid tick size (0.001), minimum for the market is 0.01",
    )
    trade, executor, event_logger, closed = make_trade(sell_result=sell_result)
    trade.position_shares = Decimal("10")
    trade._tick_size = Decimal("0.01")

    sleeps = []

    async def record_sleep(delay):
        sleeps.append(delay)

    with patch("strategy_weather_sweep.service.asyncio.sleep", record_sleep):
        run(trade._start_normal_exit())

    assert len(executor.placed_orders) == 4
    assert executor.placed_orders[0]["price"] == "0.999"
    assert executor.placed_orders[0]["tick_size"] == "0.001"
    assert executor.placed_orders[3]["price"] == "0.999"
    assert executor.placed_orders[3]["tick_size"] == "0.001"
    assert sleeps == [120.0, 300.0, 600.0]
    assert trade.state == "closed"
    assert closed == ["token"]
    assert close_reason(event_logger) == "sell_placement_failed"
    event_closed = next(
        detail for _, step, detail in event_logger.steps if step == "event_closed"
    )
    assert event_closed["position_open"] is True
    assert event_closed["position_shares"] == "10"
    assert event_closed["error_signature"] == "invalid_tick_size"

    steps = [(step, detail.get("stop_reason")) for _, step, detail in event_logger.steps]
    assert steps.count(("tick_refreshed", None)) == 3
    assert ("sell_circuit_breaker_triggered", "invalid_tick_retry_exhausted") in steps


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

    sleeps = []

    async def record_sleep(delay):
        sleeps.append(delay)

    with patch("strategy_weather_sweep.service.asyncio.sleep", record_sleep):
        run(trade._start_normal_exit())

    assert trade.state == "closed"
    assert closed == ["token"]
    assert len(executor.placed_orders) == 2
    assert sleeps == [120.0]
    assert ("exit", "balance_settlement_retry") in [
        (phase, step) for phase, step, _ in event_logger.steps
    ]
    assert ("exit", "event_closed") in [
        (phase, step) for phase, step, _ in event_logger.steps
    ]


def test_insufficient_balance_shrinks_to_observed_sellable_then_finishes():
    failed = OrderResult(
        order_id="sell-settlement",
        status="failed",
        filled_size="0",
        error="not enough balance / allowance: balance: 4390000, order amount: 10000000",
    )
    first_retry = OrderResult(
        order_id="sell-retry-1",
        status="filled",
        filled_size="4.39",
        clob_status="matched",
        clob_taking="4.38",
        clob_making="4.39",
    )
    second_retry = OrderResult(
        order_id="sell-retry-2",
        status="filled",
        filled_size="5.61",
        clob_status="matched",
        clob_taking="5.60",
        clob_making="5.61",
    )
    trade, executor, event_logger, closed = make_trade(
        sell_results=[failed, first_retry, second_retry],
        orderbook_ws=FakeOrderBookWS(min_order_size=Decimal("0.01")),
    )
    trade.position_shares = Decimal("10")
    trade._tick_size = Decimal("0.01")

    sleeps = []

    async def record_sleep(delay):
        sleeps.append(delay)

    with patch("strategy_weather_sweep.service.asyncio.sleep", record_sleep):
        run(trade._start_normal_exit())

    assert trade.state == "closed"
    assert closed == ["token"]
    assert len(executor.placed_orders) == 3
    assert executor.placed_orders[1]["size"] == "4.39"
    assert executor.placed_orders[2]["size"] == "5.61"
    assert sleeps == []
    assert close_reason(event_logger) == "normal_exit"


def test_dust_position_stops_before_placing_sell():
    trade, executor, event_logger, closed = make_trade(
        orderbook_ws=FakeOrderBookWS(min_order_size=Decimal("5")),
    )
    trade.position_shares = Decimal("4.9")
    trade._tick_size = Decimal("0.01")

    run(trade._start_normal_exit())

    assert trade.state == "closed"
    assert closed == ["token"]
    assert executor.placed_orders == []
    dust = next(
        detail for _, step, detail in event_logger.steps if step == "dust_position_detected"
    )
    assert dust["position_shares"] == "4.9"
    assert dust["min_order_size"] == "5"
    event_closed = next(
        detail for _, step, detail in event_logger.steps if step == "event_closed"
    )
    assert event_closed["close_reason"] == "dust_position"


def test_force_exit_reconciles_open_orders_and_closes_without_selling():
    trade, _, event_logger, closed = make_trade(final_matched=Decimal("10"))
    trade.position_shares = Decimal("10")
    trade.exit_order_id = "sell-1"
    trade.sell_price = Decimal("0.999")

    run(trade.force_exit("config_disabled"))

    assert trade.position_shares == Decimal("0")
    assert trade.state == "closed"
    assert closed == ["token"]
    assert close_reason(event_logger) == "force_exit"
    assert ("exit", "fill_reconciled") in [
        (phase, step) for phase, step, _ in event_logger.steps
    ]
    assert ("exit", "sell_cancelled") in [
        (phase, step) for phase, step, _ in event_logger.steps
    ]
    assert ("exit", "sell_order_placed") not in [
        (phase, step) for phase, step, _ in event_logger.steps
    ]


def test_force_exit_without_open_orders_closes_with_remaining_position():
    trade, executor, event_logger, closed = make_trade()
    trade.position_shares = Decimal("10")

    run(trade.force_exit("config_disabled"))

    assert trade.position_shares == Decimal("10")
    assert trade.state == "closed"
    assert closed == ["token"]
    assert executor.placed_orders == []
    assert close_reason(event_logger) == "force_exit"
    event_closed = next(
        detail for _, step, detail in event_logger.steps if step == "event_closed"
    )
    assert event_closed["position_open"] is True
    assert event_closed["position_shares"] == "10"
    assert event_closed["close_reason"] == "force_exit"
    assert "stop_reason" not in event_closed


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


def test_entry_timeout_cancel_query_failure_uses_memory_position_when_cancelled():
    trade, _, event_logger, closed = make_trade(
        final_matched=Decimal("-1"), cancel_query_failed=True,
    )
    trade.entry_order_id = "buy-1"

    run(trade._entry_timeout(0))

    assert trade.state == "closed"
    assert closed == ["token"]
    reconcile_unavailable = next(
        detail for _, step, detail in event_logger.steps
        if step == "fill_reconcile_unavailable"
    )
    assert reconcile_unavailable["side"] == "BUY"
    assert reconcile_unavailable["query_status"] == "query_failed"
    assert reconcile_unavailable["action"] == "continue_with_memory_position"
    assert reconcile_unavailable["non_fatal"] is True
    event_closed = next(
        detail for _, step, detail in event_logger.steps if step == "event_closed"
    )
    assert event_closed["close_reason"] == "timeout_no_fill"


def test_entry_timeout_cancel_failure_fails_closed():
    trade, _, event_logger, closed = make_trade(cancel_cancelled=False)
    trade.entry_order_id = "buy-1"

    run(trade._entry_timeout(0))

    assert trade.state == "closed"
    assert closed == ["token"]
    assert ("entry", "buy_cancel_failed") in [
        (phase, step) for phase, step, _ in event_logger.steps
    ]
    event_closed = next(
        detail for _, step, detail in event_logger.steps if step == "event_closed"
    )
    assert event_closed["close_reason"] == "buy_cancel_failed"


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

    trade._close_exit_failed("sell_placement_failed", extra={
        "error": "timeout",
        "error_signature": "network_timeout",
    })

    assert trade.state == "closed"
    assert closed == ["token"]
    pause_detail = next(
        detail for _, step, detail in event_logger.steps if step == "strategy_paused"
    )
    assert ("exit", "strategy_paused") in [
        (phase, step) for phase, step, _ in event_logger.steps
    ]
    assert pause_detail["reason"] == "sell_error_circuit_breaker"
    assert pause_detail["error_signature"] == "network_timeout"
    assert pause_detail["config_disabled"] is True

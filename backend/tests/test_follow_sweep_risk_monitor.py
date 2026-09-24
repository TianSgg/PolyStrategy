import asyncio
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from strategy_follow_weather_sweeper.internal.risk_monitor import SweepRiskMonitor  # noqa: E402
from strategy_follow_weather_sweeper.service import SweepTrade  # noqa: E402
from framework.strategy_runtime.event_logger import EventStepHandle  # noqa: E402
from framework.strategy_runtime.interfaces import OrderResult, Signal  # noqa: E402


class FakeOrderBookWS:
    def __init__(self):
        self.snapshot = None
        self.resync_calls = []
        self.subscription = None

    async def subscribe(self, **kwargs):
        self.subscription = kwargs
        return "sub-1"

    async def resync(self, token_id):
        self.resync_calls.append(token_id)
        self.snapshot = {
            "best_bid": 0.80,
            "best_bid_size": 10,
            "best_ask": 0.82,
            "best_ask_size": 12,
        }
        return True

    def get_bbo_snapshot(self, token_id):
        return self.snapshot

    async def unsubscribe(self, sub_id):
        return None


class FakeTickSizeService:
    async def refresh(self, token_id):
        return Decimal("0.01")

    def invalidate(self, token_id):
        return None


class FakeEventLogger:
    event_id = "event-1"

    def __init__(self):
        self.steps = []
        self.updated_steps = []

    def start_event(self, **kwargs):
        return self.event_id

    def log_step(self, step, detail, phase="entry"):
        self.steps.append((phase, step, detail))
        if step == "buy_order_placed":
            return EventStepHandle(self.event_id, len(self.steps), None)

    async def update_step_detail(self, handle, detail):
        self.updated_steps.append((handle, detail))

    def end_event(self):
        return None


class FakeEntryUserWS:
    def __init__(self):
        self.watched = {}
        self.events = []

    def watch_order(self, **kwargs):
        self.events.append("watch_order")
        self.watched[kwargs["order_id"]] = kwargs

    def unwatch_order(self, order_id):
        self.watched.pop(order_id, None)


class FakeEntryExecutor:
    available_cash = Decimal("1000")

    def __init__(self):
        self.user_ws = FakeEntryUserWS()

    async def place_order(self, **kwargs):
        return OrderResult(
            order_id="buy-live-1",
            status="live",
            filled_size="0",
            clob_status="live",
        )

    async def ensure_user_ws(self):
        return self.user_ws


class BlockingEntryBbo:
    def __init__(self):
        self.calls = []
        self.aft_started = asyncio.Event()
        self.release_aft = asyncio.Event()

    async def fetch_bbo(self, token_id, offset_origin_ms):
        self.calls.append(token_id)
        if len(self.calls) == 1:
            return {
                "status": "ok",
                "source": "clob_book_api",
                "captured_at_ms": 100,
                "best_bid": 0.98,
                "best_ask": 0.99,
            }
        self.aft_started.set()
        await self.release_aft.wait()
        return {
            "status": "ok",
            "source": "clob_book_api",
            "captured_at_ms": 200,
            "best_bid": 0.97,
            "best_ask": 0.99,
        }


class NoopRisk:
    started_at_ms = 100
    reference_mid = Decimal("0.985")
    threshold = Decimal("0.5910")

    def __init__(self, events=None):
        self.events = events

    async def start(self, **kwargs):
        if self.events is not None:
            self.events.append("risk_start")

    async def wait_for_first_bbo(self):
        return True

    def first_bbo_snapshot(self):
        return None

    async def stop(self):
        return None


def test_follow_buy_bbo_keeps_only_capture_time():
    compact = SweepTrade._compact_bbo({
        "status": "ok",
        "source": "clob_book_api",
        "request_started_at_ms": 90,
        "response_at_ms": 101,
        "captured_at_ms": 100,
        "best_bid": 0.98,
        "best_ask": 0.99,
    })

    assert compact["captured_at"] == "1970-01-01T00:00:00.100Z"
    assert "utc" not in compact
    assert "request_started_at" not in compact
    assert "response_at" not in compact


@pytest.mark.parametrize(
    ("best_bid", "best_ask", "expected"),
    [
        (Decimal("0.80"), Decimal("0.82"), Decimal("0.81")),
        (None, Decimal("0.82"), Decimal("0.41")),
        (Decimal("0.06"), None, Decimal("0.53")),
        (None, None, Decimal("0.5")),
    ],
)
def test_mid_price_uses_boundary_values_for_missing_quotes(best_bid, best_ask, expected):
    assert SweepRiskMonitor._mid_from_prices(best_bid, best_ask) == expected


@pytest.mark.asyncio
async def test_risk_monitor_bootstraps_from_post_signal_orderbook_when_signal_has_no_snapshot():
    ws = FakeOrderBookWS()
    monitor = SweepRiskMonitor(
        ws,
        tick_size_service=FakeTickSizeService(),
    )

    await monitor.start("token", {
        "best_bid": 0.99,
        "best_ask": 0.99,
    })
    await asyncio.sleep(0)

    assert ws.resync_calls == ["token"]
    assert monitor.is_active is True
    assert monitor.reference_mid == Decimal("0.81")
    assert monitor.threshold == Decimal("0.4860")
    assert await monitor.wait_for_first_bbo() is True

    await monitor.stop()


@pytest.mark.asyncio
async def test_entry_registers_fill_watch_before_waiting_for_after_order_bbo():
    bbo = BlockingEntryBbo()
    executor = FakeEntryExecutor()
    event_logger = FakeEventLogger()
    trade = SweepTrade(
        token_id="token",
        market_slug="market",
        config={"fixed_entry_shares": "20", "entry_wait_ms": 60000},
        executor=executor,
        orderbook_ws=FakeOrderBookWS(),
        event_logger=event_logger,
        on_closed=lambda _token: None,
        book_bbo_client=bbo,
        tick_size_service=FakeTickSizeService(),
    )
    trade.risk = NoopRisk(executor.user_ws.events)

    signal = Signal(
        signal_id="signal-1",
        signal_type="sweep",
        token_id="token",
        market_slug="market",
        occurred_at_ms=0,
        source="test",
        payload={},
    )

    await trade.enter(signal)
    await asyncio.sleep(0)

    assert bbo.aft_started.is_set()
    assert "buy-live-1" in executor.user_ws.watched
    assert [step for _, step, _ in event_logger.steps].count("buy_order_placed") == 1
    assert executor.user_ws.events.index("watch_order") < executor.user_ws.events.index("risk_start")
    buy_detail = next(
        detail for _, step, detail in event_logger.steps
        if step == "buy_order_placed"
    )
    assert "utc" not in buy_detail["order"]
    assert buy_detail["order_response_at"] is not None

    bbo.release_aft.set()
    for _ in range(5):
        await asyncio.sleep(0)
        if event_logger.updated_steps:
            break
    assert len(event_logger.updated_steps) == 1
    updated_detail = event_logger.updated_steps[0][1]
    assert updated_detail["bbo_observation_pending"] is False
    assert updated_detail["pre_bbo"]["captured_at"] == "1970-01-01T00:00:00.100Z"
    assert updated_detail["aft_bbo"]["captured_at"] == "1970-01-01T00:00:00.200Z"
    await trade.stop()

import asyncio
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from framework.strategy_runtime.interfaces import CancelResult  # noqa: E402
import strategy_follow_weather_sweeper.service as service  # noqa: E402


class FakeEventLogger:
    def __init__(self):
        self.event_id = None
        self.steps = []

    def attach_event(self, event_id):
        self.event_id = event_id

    def log_step(self, step, detail, phase="entry"):
        self.steps.append((phase, step, detail))

    def end_event(self):
        self.event_id = None


class FakeUserWS:
    def __init__(self):
        self.unwatched = []

    def unwatch_order(self, order_id):
        self.unwatched.append(order_id)


class FakeExecutor:
    def __init__(self):
        self.user_ws = FakeUserWS()
        self.cancelled = []

    async def ensure_user_ws(self):
        return self.user_ws

    async def cancel_order_with_fill_check(self, order_id):
        self.cancelled.append(order_id)
        return CancelResult(
            order_id=order_id,
            cancelled=True,
            final_matched=Decimal("3.5"),
            status="cancelled",
        )


class FakeTradeDAO:
    def __init__(self):
        self.updates = []

    def update_by_event_id(self, event_id, data):
        self.updates.append((event_id, data))
        return True


def run(coro):
    return asyncio.run(coro)


def test_orphaned_trade_is_cancelled_and_closed_without_selling(monkeypatch):
    event_logger = FakeEventLogger()
    dao = FakeTradeDAO()
    executor = FakeExecutor()
    strategy = object.__new__(service.FollowSweepStrategy)
    strategy._config_id = 7
    strategy._executor = executor
    strategy._trade_dao = dao

    monkeypatch.setattr(service, "EventLogger", lambda **kwargs: event_logger)
    row = {
        "event_id": "event-7",
        "owner_user_id": 1,
        "proxy_wallet": "0xwallet",
        "config_snapshot": {"fixed_entry_shares": 5},
        "phase": "entry",
        "entry_order_id": "buy-7",
        "exit_order_id": None,
        "entry_shares": Decimal("2"),
        "exit_shares": Decimal("0"),
        "entry_price": Decimal("0.99"),
        "exit_price": None,
        "started_at": datetime(2026, 9, 24, 0, 0, 0),
    }

    run(strategy._force_exit_orphaned_trade(row, "config_changed"))

    assert executor.cancelled == ["buy-7"]
    assert executor.user_ws.unwatched == ["buy-7"]
    assert len(dao.updates) == 1
    event_id, updates = dao.updates[0]
    assert event_id == "event-7"
    assert updates["phase"] == "closed"
    assert updates["close_reason"] == "force_exit"
    assert updates["entry_order_id"] is None
    assert updates["entry_shares"] == "3.5"
    assert updates["entry_cost"] == "3.465"
    assert [step for _, step, _ in event_logger.steps] == [
        "force_exit_requested", "buy_cancelled", "event_closed",
    ]

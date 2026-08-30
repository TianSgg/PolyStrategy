import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from strategy_weather_sweep.internal.sell_failure import (  # noqa: E402
    SellFailureTracker,
    classify_sell_error,
)


def test_classify_sell_error_signatures():
    assert classify_sell_error("failed", "invalid tick size (0.001)") == "invalid_tick_size"
    assert classify_sell_error("failed", "tick size mismatch: caller=0.01") == "invalid_tick_size"
    assert classify_sell_error("insufficient_balance", None) == "insufficient_balance"
    assert classify_sell_error("failed", "Read timeout while calling CLOB") == "network_timeout"
    assert classify_sell_error("failed", "401 unauthorized") == "authentication_error"
    assert classify_sell_error("failed", "invalid order price") == "invalid_order_params"
    assert classify_sell_error("failed", "server exploded") == "unknown_api_error"


def test_tracker_stops_after_three_same_errors():
    now = [0.0]
    tracker = SellFailureTracker(clock=lambda: now[0])

    first = tracker.record("invalid_tick_size", "invalid tick size")
    assert first.stop_reason is None

    second = tracker.record("invalid_tick_size", "invalid tick size")
    assert second.stop_reason is None

    third = tracker.record("invalid_tick_size", "invalid tick size")
    assert third.stop_reason == "same_error_repeated"
    assert third.attempt_count == 3
    assert third.consecutive_same_error == 3


def test_tracker_stops_after_five_total_errors():
    tracker = SellFailureTracker()
    signatures = ["network_timeout", "authentication_error", "unknown_api_error", "network_timeout"]

    for signature in signatures:
        assert tracker.record(signature, signature).stop_reason is None

    final = tracker.record("invalid_order_params", "invalid order price")
    assert final.stop_reason == "total_failures_exceeded"
    assert final.consecutive_same_error == 1


def test_tracker_stops_when_deadline_exceeded():
    now = [0.0]
    tracker = SellFailureTracker(clock=lambda: now[0])
    tracker.record("network_timeout", "timeout")

    now[0] = 180.0
    assert tracker.snapshot().stop_reason == "deadline_exceeded"

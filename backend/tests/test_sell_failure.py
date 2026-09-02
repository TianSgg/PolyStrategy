import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from strategy_weather_sweep.internal.sell_failure import (  # noqa: E402
    SellErrorCircuitBreaker,
    SellFailureTracker,
    classify_sell_error,
)


def test_classify_sell_error_signatures():
    assert classify_sell_error("failed", "invalid tick size (0.001)") == "invalid_tick_size"
    assert classify_sell_error("failed", "tick size mismatch: caller=0.01") == "invalid_tick_size"
    assert classify_sell_error("insufficient_balance", None) == "insufficient_balance"
    assert classify_sell_error(
        "failed", "not enough balance / allowance"
    ) == "insufficient_balance"
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


def test_tracker_does_not_stop_for_mixed_errors():
    tracker = SellFailureTracker()
    signatures = [
        "network_timeout", "authentication_error", "unknown_api_error",
        "network_timeout", "invalid_order_params",
    ]

    for signature in signatures:
        assert tracker.record(signature, signature).stop_reason is None

    assert tracker.snapshot().attempt_count == 5
    assert tracker.snapshot().consecutive_same_error == 1


def test_tracker_does_not_stop_on_elapsed_time():
    now = [0.0]
    tracker = SellFailureTracker(clock=lambda: now[0])
    tracker.record("network_timeout", "timeout")

    now[0] = 180.0
    assert tracker.snapshot().stop_reason is None


def test_global_breaker_requires_three_events_in_window():
    now = [0.0]
    breaker = SellErrorCircuitBreaker(clock=lambda: now[0])

    assert breaker.record_event("token-1", "network_timeout") is False
    assert breaker.record_event("token-2", "network_timeout") is False
    assert breaker.record_event("token-3", "network_timeout") is True
    assert breaker.record_event("token-4", "network_timeout") is False


def test_global_breaker_expires_old_events():
    now = [0.0]
    breaker = SellErrorCircuitBreaker(clock=lambda: now[0])
    breaker.record_event("token-1", "invalid_tick_size")

    now[0] = 301.0
    breaker.record_event("token-2", "invalid_tick_size")
    breaker.record_event("token-3", "invalid_tick_size")
    assert breaker.event_count("invalid_tick_size") == 2

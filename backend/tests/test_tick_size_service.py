import asyncio
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from framework.strategy_runtime.tick_size_service import (  # noqa: E402
    TickSizeFetchError,
    TickSizeService,
)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_get_uses_short_ttl_cache():
    service = TickSizeService(default_max_age_ms=60_000)

    with patch(
        "framework.strategy_runtime.tick_size_service.requests.get",
        return_value=FakeResponse({"minimum_tick_size": 0.001}),
    ) as get:
        first = asyncio.run(service.get("token"))
        second = asyncio.run(service.get("token"))

    assert first == Decimal("0.001")
    assert second == Decimal("0.001")
    assert get.call_count == 1


def test_expired_value_is_refetched():
    service = TickSizeService()

    with patch(
        "framework.strategy_runtime.tick_size_service.requests.get",
        return_value=FakeResponse({"minimum_tick_size": 0.001}),
    ) as get:
        first = asyncio.run(service.get("token", max_age_ms=-1))
        second = asyncio.run(service.get("token", max_age_ms=-1))

    assert first == Decimal("0.001")
    assert second == Decimal("0.001")
    assert get.call_count == 2


def test_refresh_bypasses_cache():
    service = TickSizeService(default_max_age_ms=60_000)

    with patch(
        "framework.strategy_runtime.tick_size_service.requests.get",
        return_value=FakeResponse({"minimum_tick_size": 0.01}),
    ):
        assert asyncio.run(service.get("token")) == Decimal("0.01")

    with patch(
        "framework.strategy_runtime.tick_size_service.requests.get",
        return_value=FakeResponse({"minimum_tick_size": 0.001}),
    ) as get:
        assert asyncio.run(service.refresh("token")) == Decimal("0.001")

    assert get.call_count == 1


def test_invalidate_forces_next_get_to_fetch():
    service = TickSizeService(default_max_age_ms=60_000)

    with patch(
        "framework.strategy_runtime.tick_size_service.requests.get",
        return_value=FakeResponse({"minimum_tick_size": 0.01}),
    ) as get:
        assert asyncio.run(service.get("token")) == Decimal("0.01")
        service.invalidate("token")
        assert asyncio.run(service.get("token")) == Decimal("0.01")

    assert get.call_count == 2


def test_concurrent_gets_share_one_refresh():
    service = TickSizeService(default_max_age_ms=60_000)

    def fake_get(*args, **kwargs):
        return FakeResponse({"minimum_tick_size": 0.001})

    with patch(
        "framework.strategy_runtime.tick_size_service.requests.get",
        side_effect=fake_get,
    ) as get:
        async def main():
            return await asyncio.gather(*(service.get("token") for _ in range(10)))

        results = asyncio.run(main())

    assert results == [Decimal("0.001")] * 10
    assert get.call_count == 1


def test_refresh_failure_does_not_return_stale_cache():
    service = TickSizeService(default_max_age_ms=60_000)

    with patch(
        "framework.strategy_runtime.tick_size_service.requests.get",
        return_value=FakeResponse({"minimum_tick_size": 0.01}),
    ):
        assert asyncio.run(service.get("token")) == Decimal("0.01")

    with patch(
        "framework.strategy_runtime.tick_size_service.requests.get",
        side_effect=RuntimeError("network down"),
    ):
        with pytest.raises(TickSizeFetchError):
            asyncio.run(service.refresh("token"))

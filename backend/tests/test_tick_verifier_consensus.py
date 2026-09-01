import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from framework.strategy_runtime.tick_verifier import TickVerifier  # noqa: E402


class FakeTickSizeService:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def refresh_consensus(self, token_id, ws_tick_size):
        self.calls.append(ws_tick_size)
        if self.error is not None:
            raise self.error
        if ws_tick_size is None:
            raise RuntimeError("ws tick missing")
        return self.result


def test_verifier_confirms_when_all_three_sources_match():
    verifier = TickVerifier(
        tick_size_service=FakeTickSizeService(Decimal("0.001"))
    )

    result = asyncio.run(
        verifier.verify("token", ws_tick_size=Decimal("0.001"))
    )

    assert result.confirmed is True
    assert result.actual_tick == Decimal("0.001")
    assert result.ws_tick_size == Decimal("0.001")
    assert result.tick_api_size == Decimal("0.001")
    assert result.book_tick_size == Decimal("0.001")


def test_verifier_reloads_ws_tick_size_between_attempts():
    service = FakeTickSizeService(Decimal("0.001"))
    verifier = TickVerifier(
        tick_size_service=service,
        backoff_ms=1,
        max_backoff_ms=1,
    )
    ws_values = [None, Decimal("0.001")]

    result = asyncio.run(
        verifier.verify(
            "token",
            ws_tick_size=None,
            ws_tick_size_getter=lambda: ws_values.pop(0) if ws_values else Decimal("0.001"),
        )
    )

    assert result.confirmed is True
    assert service.calls == [None, Decimal("0.001")]
    assert result.ws_tick_size == Decimal("0.001")

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

    async def refresh_consensus(self, token_id, ws_tick_size):
        if self.error is not None:
            raise self.error
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

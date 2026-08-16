from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LocalOrderBook:
    """Minimal L2 book used by weather event evaluation."""

    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)

    def apply_snapshot(self, bids: list[dict], asks: list[dict]) -> None:
        self.bids = self._levels(bids)
        self.asks = self._levels(asks)

    def apply_change(self, price: str, size: str, side: str) -> None:
        levels = self.asks if side == "SELL" else self.bids if side == "BUY" else None
        if levels is None:
            return
        numeric_price, numeric_size = float(price), float(size)
        if numeric_size <= 0:
            levels.pop(numeric_price, None)
        else:
            levels[numeric_price] = numeric_size

    def summary(self) -> dict:
        return {
            "bid_count": len(self.bids), "ask_count": len(self.asks),
            "best_bid": max(self.bids, default=None), "best_ask": min(self.asks, default=None),
            "bid_total_size": sum(self.bids.values()), "ask_total_size": sum(self.asks.values()),
        }

    def top_of_book(self) -> dict:
        """Return BBO and level counts for a compact event notification."""
        best_bid = max(self.bids, default=None)
        best_ask = min(self.asks, default=None)
        return {
            "best_bid": {"price": best_bid, "size": self.bids[best_bid]} if best_bid is not None else None,
            "best_ask": {"price": best_ask, "size": self.asks[best_ask]} if best_ask is not None else None,
            "bid_levels": len(self.bids),
            "ask_levels": len(self.asks),
        }

    @staticmethod
    def _levels(levels: list[dict]) -> dict[float, float]:
        return {float(row["price"]): float(row["size"]) for row in levels if float(row.get("size", 0)) > 0}

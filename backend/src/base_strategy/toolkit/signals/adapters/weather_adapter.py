from __future__ import annotations

from base_strategy.interfaces import Signal


class WeatherSweepAdapter:
    """将天气信号源 WS 消息转为通用 Signal。"""

    def adapt(self, raw: dict) -> Signal | None:
        if raw.get("type") != "weather_sweep":
            return None

        data = raw.get("signal", {})
        return Signal(
            signal_id=f"weather:{data.get('event_id', '')}",
            signal_type="sweep",
            token_id=data.get("token_id", ""),
            market_slug=data.get("market_slug", ""),
            occurred_at_ms=data.get("occurred_at_ms", 0),
            source="weather_orderbook",
            payload=data,
        )

from __future__ import annotations

from framework.strategy_runtime.interfaces import Signal


class WeatherSweepAdapter:
    """将天气信号源 WS 消息转为通用 Signal。"""

    def adapt(self, raw: dict) -> Signal | None:
        if raw.get("type") != "weather_sweep":
            return None

        data = raw.get("signal", {})
        if not data.get("token_id"):
            return None
        event_type = data.get("event_type", "sweep")
        signal_id = data.get("signal_id")
        if not signal_id:
            return None
        return Signal(
            signal_id=signal_id,
            signal_type=event_type,
            token_id=data.get("token_id", ""),
            market_slug=data.get("market_slug", ""),
            occurred_at_ms=data.get("occurred_at_ms", 0),
            source="weather_orderbook",
            payload=data,
        )

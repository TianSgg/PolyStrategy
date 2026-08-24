from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Literal, Mapping, Optional

WeatherEventType = Literal["sweep", "no_longer_possible", "market_resolved"]


@dataclass(frozen=True)
class WeatherCity:
    name: str
    slug: str
    timezone: str
    directions: tuple[str, ...]


@dataclass(frozen=True)
class WeatherAsset:
    asset_id: str
    city: str
    event_slug: str
    market_slug: str
    temperature_label: str
    outcome: Literal["yes", "no"]


@dataclass(frozen=True)
class MarketCandidate:
    city: str
    direction: str
    event_slug: str
    market_slug: str
    temperature_label: str
    yes_asset: WeatherAsset
    no_asset: WeatherAsset


@dataclass(frozen=True)
class WeatherEvent:
    event_type: WeatherEventType
    asset: WeatherAsset
    previous_orderbook: dict[str, Any] | None
    current_orderbook: dict[str, Any]
    reason: str

    def payload(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "asset": asdict(self.asset),
            "previous_orderbook": self.previous_orderbook,
            "current_orderbook": self.current_orderbook,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class WeatherSignalRecord:
    """One persisted weather signal record."""

    signal_id: str
    occurred_at: datetime
    signal_type: str
    event_slug: str
    city: str
    city_slug: str
    direction: str
    local_date: date
    market_slug: str | None
    temperature_label: str | None
    outcome: str | None
    main_market_slug: str | None
    main_temperature_label: str | None
    main_outcome: str | None
    token_id: str | None
    status: str | None
    reason: str | None
    payload: dict[str, Any]
    id: int | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class WeatherSweepSignal:
    """天气盘口扫单信号 — 检测到 asks 被完全清扫。

    字段对齐 WeatherTaker 的 WeatherEvent + notification 记录。
    策略侧核心使用: token_id, outcome, event_id。
    """
    event_id: str
    event_type: str  # "sweep" | "no_longer_possible" | "market_resolved"
    token_id: str
    outcome: str  # "yes" | "no"
    city: str
    event_slug: str
    market_slug: Optional[str] = None
    temperature_label: Optional[str] = None
    direction: Optional[str] = None  # "highest" | "lowest"
    reason: Optional[str] = None
    occurred_at_ms: int = 0
    received_at_ns: int = 0
    orderbook_snapshot: Mapping[str, Any] = field(default_factory=dict)

    def dedup_key(self) -> str:
        return f"weather:{self.event_id}"


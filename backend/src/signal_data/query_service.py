"""信号数据查询服务 — 供 REST API 使用的只读检索与回放能力。"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from signal_data.repository import (
    SignalAnalysisFeaturesRepository,
    SignalEventRepository,
    SignalMarketContextRepository,
)

logger = logging.getLogger(__name__)


class SignalQueryService:
    """面向前端和分析的信号数据查询。"""

    def __init__(self) -> None:
        self._event_repo = SignalEventRepository()
        self._context_repo = SignalMarketContextRepository()
        self._feature_repo = SignalAnalysisFeaturesRepository()

    def get_signal(self, signal_id: str) -> dict[str, Any] | None:
        """获取单个信号及其关联的上下文和特征。"""
        event = self._event_repo.find_by_id(signal_id)
        if not event:
            return None
        event["market_contexts"] = self._context_repo.find_by_signal(signal_id)
        event["analysis_features"] = self._feature_repo.find_by_signal(signal_id)
        return event

    def query_signals(
        self,
        *,
        source_type: str | None = None,
        token_id: str | None = None,
        leader_proxy_wallet: str | None = None,
        event_type: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """按条件分页查询信号列表。"""
        return self._event_repo.query(
            source_type=source_type,
            token_id=token_id,
            leader_proxy_wallet=leader_proxy_wallet,
            event_type=event_type,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
            offset=offset,
        )

    def replay(
        self,
        *,
        source_type: str | None = None,
        token_id: str | None = None,
        start_time: datetime,
        end_time: datetime,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """时间范围内信号回放 — 按 occurred_at 升序。"""
        events = self._event_repo.query(
            source_type=source_type,
            token_id=token_id,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
            offset=0,
        )
        events.sort(key=lambda e: e.get("occurred_at", ""))
        return events

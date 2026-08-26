"""策略配置的请求/响应数据结构。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class CreateConfigRequest(BaseModel):
    account_id: int
    name: str
    enabled: bool = False
    fixed_entry_shares: float = 100.0
    entry_wait_ms: int = 30000
    sweep_outcome_filter: str = "no"
    signal_source_filter: str = "all"
    signal_threshold_filter: str = "all"
    stop_loss_ratio: float = 0.60
    exit_wait_ms: int = 5000
    tick_verify_retries: int = 3
    tick_verify_backoff_ms: int = 1000


class UpdateConfigRequest(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    fixed_entry_shares: Optional[float] = None
    entry_wait_ms: Optional[int] = None
    sweep_outcome_filter: Optional[str] = None
    signal_source_filter: Optional[str] = None
    signal_threshold_filter: Optional[str] = None
    stop_loss_ratio: Optional[float] = None
    exit_wait_ms: Optional[int] = None
    tick_verify_retries: Optional[int] = None
    tick_verify_backoff_ms: Optional[int] = None

"""策略配置的请求/响应数据结构。"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel


class CreateConfigRequest(BaseModel):
    account_id: int
    name: str
    enabled: bool = False
    fixed_entry_shares: float = 100.0
    entry_wait_ms: int = 1200000
    stop_loss_ratio: float = 0.60
    exit_wait_ms: int = 5000
    leader_wallets: List[str] = []


class UpdateConfigRequest(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    fixed_entry_shares: Optional[float] = None
    entry_wait_ms: Optional[int] = None
    stop_loss_ratio: Optional[float] = None
    exit_wait_ms: Optional[int] = None
    leader_wallets: Optional[List[str]] = None

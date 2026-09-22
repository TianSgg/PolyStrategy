"""策略配置的请求/响应数据结构。"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel


class ConfigParams(BaseModel):
    fixed_entry_shares: float = 100.0
    entry_wait_ms: int = 1200000
    stop_loss_ratio: float = 0.60
    exit_wait_ms: int = 5000
    leader_wallet: str = ""
    outcome_filter: str = "no"
    slug_script: Optional[str] = None


class CreateConfigRequest(BaseModel):
    account_id: int
    name: str
    enabled: bool = False
    params: ConfigParams = ConfigParams()


class UpdateConfigRequest(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    params: Optional[ConfigParams] = None

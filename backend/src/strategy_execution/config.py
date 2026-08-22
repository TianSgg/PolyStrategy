"""策略配置 Pydantic 模型 — 每种策略有独立的参数表和显式字段。

前端提交的配置参数通过这些模型校验后，以 JSON 快照存入对应策略的配置表。
运行创建时冻结参数副本到 strategy_runs.params_snapshot_json，运行期间不可变。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from strategy_execution.enums import StrategyType


# ─── 通用份额模式 ─────────────────────────────────────────────────────────────

EntrySizeMode = Literal["fixed", "max_available"]
ExitMode = Literal["risk_tick_exit", "sell_099_then_risk"]
OutcomeFilter = Literal["yes", "no", "all"]


# ─── 策略 1: Sweep ────────────────────────────────────────────────────────────


class SweepStrategyParams(BaseModel):
    """策略 1 可配置参数。"""
    strategy_type: Literal[StrategyType.SWEEP] = StrategyType.SWEEP
    params_version: int = 1

    fixed_entry_shares: Decimal = Field(default=Decimal("100"), gt=0, le=Decimal("100000"))
    entry_wait_ms: int = Field(default=30000, ge=1000, le=300000)
    sweep_outcome_filter: OutcomeFilter = "no"

    stop_loss_ratio: Decimal = Field(default=Decimal("0.60"), gt=0, lt=1)
    exit_wait_ms: int = Field(default=5000, ge=1000, le=60000)
    tick_verify_retries: int = Field(default=3, ge=1, le=10)
    tick_verify_backoff_ms: int = Field(default=1000, ge=200, le=10000)

    @field_validator("fixed_entry_shares", "stop_loss_ratio", mode="before")
    @classmethod
    def _coerce_decimal(cls, v):
        return Decimal(str(v)) if not isinstance(v, Decimal) else v


# ─── 策略 2: Leader ───────────────────────────────────────────────────────────


class LeaderStrategyParams(BaseModel):
    """策略 2 可配置参数。"""
    strategy_type: Literal[StrategyType.LEADER] = StrategyType.LEADER
    params_version: int = 1

    entry_size_mode: EntrySizeMode = "fixed"
    fixed_entry_shares: Decimal = Field(default=Decimal("100"), gt=0, le=Decimal("100000"))
    entry_wait_ms: int = Field(default=30000, ge=1000, le=300000)
    leader_outcome_filter: OutcomeFilter = "all"

    stop_loss_ratio: Decimal = Field(default=Decimal("0.60"), gt=0, lt=1)
    exit_wait_ms: int = Field(default=5000, ge=1000, le=60000)
    tick_verify_retries: int = Field(default=3, ge=1, le=10)
    tick_verify_backoff_ms: int = Field(default=1000, ge=200, le=10000)

    @field_validator("fixed_entry_shares", "stop_loss_ratio", mode="before")
    @classmethod
    def _coerce_decimal(cls, v):
        return Decimal(str(v)) if not isinstance(v, Decimal) else v


# ─── 策略 3: Sweep + Leader 确认 ──────────────────────────────────────────────


class SweepLeaderStrategyParams(BaseModel):
    """策略 3 可配置参数 — sweep 试探 + leader 确认追加。"""
    strategy_type: Literal[StrategyType.SWEEP_LEADER] = StrategyType.SWEEP_LEADER
    params_version: int = 1

    # 入场
    fixed_probe_shares: Decimal = Field(default=Decimal("50"), gt=0, le=Decimal("100000"))
    entry_wait_ms: int = Field(default=30000, ge=1000, le=300000)
    sweep_outcome_filter: OutcomeFilter = "no"
    leader_outcome_filter: OutcomeFilter = "all"
    leader_confirm_window_ms: int = Field(default=60000, ge=5000, le=600000)
    post_confirm_wait_ms: int = Field(default=30000, ge=1000, le=300000)

    # 退场
    exit_mode: ExitMode = "risk_tick_exit"
    exit_wait_ms: int = Field(default=5000, ge=1000, le=60000)

    # 风控
    stop_loss_ratio: Decimal = Field(default=Decimal("0.60"), gt=0, lt=1)
    tick_verify_retries: int = Field(default=3, ge=1, le=10)
    tick_verify_backoff_ms: int = Field(default=1000, ge=200, le=10000)

    @field_validator("fixed_probe_shares", "stop_loss_ratio", mode="before")
    @classmethod
    def _coerce_decimal(cls, v):
        return Decimal(str(v)) if not isinstance(v, Decimal) else v


# ─── 工具函数 ─────────────────────────────────────────────────────────────────

STRATEGY_PARAMS_MAP: dict[StrategyType, type[BaseModel]] = {
    StrategyType.SWEEP: SweepStrategyParams,
    StrategyType.LEADER: LeaderStrategyParams,
    StrategyType.SWEEP_LEADER: SweepLeaderStrategyParams,
}


def validate_strategy_params(strategy_type: StrategyType, raw: dict) -> BaseModel:
    """根据策略类型选择对应 Pydantic 模型并校验参数。"""
    model_cls = STRATEGY_PARAMS_MAP.get(strategy_type)
    if model_cls is None:
        raise ValueError(f"Unknown strategy type: {strategy_type}")
    return model_cls.model_validate(raw)

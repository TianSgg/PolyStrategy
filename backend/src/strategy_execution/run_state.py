"""StrategyRun 状态机 — 管理运行生命周期转换、单飞锁与乐观锁。

每个活跃运行在内存中维护一个 RunStateMachine 实例。
状态转换产生 RunEvent 并持久化；并发转换由 asyncio.Lock 串行化。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from shared.time_utils import now_utc8_dt
from strategy_execution.enums import (
    CloseReason,
    RiskState,
    RunEventType,
    RunState,
    RunStatus,
    StrategyType,
)
from strategy_execution.repository import StrategyRunEventRepository, StrategyRunRepository
from strategy_execution.ws import get_strategy_ws_manager

logger = logging.getLogger(__name__)

# 合法状态转换
_TRANSITIONS: Dict[RunState, Set[RunState]] = {
    RunState.CREATED: {RunState.ENTRY_WORKING},
    RunState.ENTRY_WORKING: {
        RunState.EXIT_WORKING,
        RunState.WAITING_LEADER,
        RunState.RISK_EXITING,
        RunState.CLOSED,
    },
    RunState.WAITING_LEADER: {
        RunState.ENTRY_WORKING,
        RunState.EXIT_WORKING,
        RunState.RISK_EXITING,
        RunState.CLOSED,
    },
    RunState.EXIT_WORKING: {
        RunState.RISK_EXITING,
        RunState.CLOSED,
    },
    RunState.RISK_EXITING: {
        RunState.CLOSED,
    },
    RunState.CLOSED: set(),
}


class RunStateMachine:
    """单个 Strategy Run 的状态管理器。

    线程安全由 asyncio.Lock 保证（同一 run 的操作串行）。
    """

    def __init__(
        self,
        run_id: str,
        strategy_type: StrategyType,
        config_id: int,
        token_id: str,
        initial_state: RunState = RunState.CREATED,
        initial_version: int = 1,
        run_repo: Optional[StrategyRunRepository] = None,
        event_repo: Optional[StrategyRunEventRepository] = None,
    ) -> None:
        self.run_id = run_id
        self.strategy_type = strategy_type
        self.config_id = config_id
        self.token_id = token_id
        self._state = initial_state
        self._version = initial_version
        self._risk_state = RiskState.OFF
        self._sequence_no = 0
        self._lock = asyncio.Lock()
        self._closed = False
        self._run_repo = run_repo or StrategyRunRepository()
        self._event_repo = event_repo or StrategyRunEventRepository()

        # 运行期聚合数据
        self.avg_entry_price: Optional[Decimal] = None
        self.entry_shares: Decimal = Decimal("0")
        self.exited_shares: Decimal = Decimal("0")
        self.close_reason: Optional[CloseReason] = None

    @property
    def state(self) -> RunState:
        return self._state

    @property
    def version(self) -> int:
        return self._version

    @property
    def risk_state(self) -> RiskState:
        return self._risk_state

    @property
    def is_closed(self) -> bool:
        return self._state == RunState.CLOSED

    @property
    def active_key(self) -> str:
        return f"{self.strategy_type.value}:{self.config_id}:{self.token_id}"

    async def transition(
        self,
        target: RunState,
        *,
        reason: Optional[str] = None,
        close_reason: Optional[CloseReason] = None,
        extra_payload: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """尝试状态转换。成功返回 True，失败（非法转换或乐观锁冲突）返回 False。"""
        async with self._lock:
            if target not in _TRANSITIONS.get(self._state, set()):
                logger.warning(
                    "Invalid transition %s → %s for run %s",
                    self._state.value, target.value, self.run_id,
                )
                return False

            old_state = self._state
            extra_fields: Dict[str, Any] = {"state": target.value}

            if target == RunState.CLOSED:
                self.close_reason = close_reason
                extra_fields["status"] = RunStatus.CLOSED.value
                extra_fields["close_reason"] = close_reason.value if close_reason else None
                extra_fields["ended_at"] = now_utc8_dt()
                extra_fields["active_key"] = None
            else:
                extra_fields["status"] = RunStatus.ACTIVE.value

            ok = self._run_repo.update_state(
                self.run_id,
                state=target.value,
                status=extra_fields.get("status"),
                expected_version=self._version,
                extra_fields={k: v for k, v in extra_fields.items() if k not in ("state", "status")},
            )
            if not ok:
                logger.error("Optimistic lock failed for run %s v%d", self.run_id, self._version)
                return False

            self._state = target
            self._version += 1
            if target == RunState.CLOSED:
                self._closed = True

            await self._emit_event(
                RunEventType.STATE_TRANSITION,
                {
                    "from": old_state.value,
                    "to": target.value,
                    "reason": reason,
                    **(extra_payload or {}),
                },
            )
            return True

    async def set_risk_state(self, risk_state: RiskState) -> None:
        """更新风控子状态。"""
        async with self._lock:
            old = self._risk_state
            self._risk_state = risk_state
            self._run_repo.update_state(
                self.run_id,
                state=self._state.value,
                expected_version=self._version,
                extra_fields={"risk_state": risk_state.value},
            )
            self._version += 1
            logger.info("Run %s risk: %s → %s", self.run_id, old.value, risk_state.value)

    async def record_fill(
        self,
        fill_price: Decimal,
        fill_shares: Decimal,
        is_buy: bool,
    ) -> None:
        """更新成交后的聚合数据（均价、持仓）。"""
        async with self._lock:
            if is_buy:
                old_total = self.avg_entry_price * self.entry_shares if self.avg_entry_price else Decimal("0")
                self.entry_shares += fill_shares
                new_total = old_total + fill_price * fill_shares
                self.avg_entry_price = new_total / self.entry_shares if self.entry_shares > 0 else None

                self._run_repo.update_state(
                    self.run_id,
                    state=self._state.value,
                    expected_version=self._version,
                    extra_fields={
                        "avg_entry_price": self.avg_entry_price,
                        "entry_shares": self.entry_shares,
                    },
                )
                self._version += 1
            else:
                self.exited_shares += fill_shares
                self._run_repo.update_state(
                    self.run_id,
                    state=self._state.value,
                    expected_version=self._version,
                    extra_fields={"exited_shares": self.exited_shares},
                )
                self._version += 1

    async def _emit_event(self, event_type: RunEventType, payload: Dict[str, Any]) -> None:
        """追加不可变事件到时间线，并推送到前端 WS。"""
        self._sequence_no += 1
        event = {
            "run_id": self.run_id,
            "sequence_no": self._sequence_no,
            "event_type": event_type.value,
            "occurred_at": now_utc8_dt(),
            "monotonic_ns": time.monotonic_ns(),
            "payload_json": payload,
        }
        try:
            self._event_repo.append(event)
        except Exception:
            logger.exception("Failed to persist run event for %s seq %d", self.run_id, self._sequence_no)

        # 推送到前端 WebSocket
        get_strategy_ws_manager().publish({
            "event_type": "strategy_run_event",
            "data": {
                "run_id": self.run_id,
                "strategy_type": self.strategy_type.value,
                "token_id": self.token_id,
                "sequence_no": self._sequence_no,
                "type": event_type.value,
                "state": self._state.value,
                "payload": payload,
            },
        })

    async def emit_event(self, event_type: RunEventType, payload: Dict[str, Any]) -> None:
        """公开接口 — 策略可以追加自定义事件。"""
        async with self._lock:
            await self._emit_event(event_type, payload)


# ─── 单飞锁管理 ───────────────────────────────────────────────────────────────


class RunSingleFlightGuard:
    """防止同一 (config_id, token_id) 并发创建多个运行。"""

    def __init__(self) -> None:
        self._active: Dict[str, RunStateMachine] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    async def acquire(self, active_key: str) -> Optional[asyncio.Lock]:
        """获取 active_key 对应的锁。如果该 key 已有活跃运行，返回 None。"""
        async with self._global_lock:
            if active_key in self._active:
                return None
            if active_key not in self._locks:
                self._locks[active_key] = asyncio.Lock()
            return self._locks[active_key]

    def register(self, machine: RunStateMachine) -> None:
        """注册一个新的活跃运行。"""
        self._active[machine.active_key] = machine

    def unregister(self, active_key: str) -> None:
        """运行关闭后注销。"""
        self._active.pop(active_key, None)
        self._locks.pop(active_key, None)

    def get_active(self, active_key: str) -> Optional[RunStateMachine]:
        return self._active.get(active_key)

    def all_active(self) -> List[RunStateMachine]:
        return list(self._active.values())

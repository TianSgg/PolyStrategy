"""AccountExecutionLedger — 每账户的内存资金/份额保留管理。

核心不变式:
  available_cash = real_cash - reserved_buy_notional - safety_buffer
  available_shares(token) = confirmed_position - reserved_sell_shares

所有修改在同一账户锁下串行执行，保证原子性。
轮询校准只调整 real_cash 和 confirmed_position，不覆盖尚未确认的保留。
"""
from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, Optional

from shared.time_utils import now_utc8_dt
from strategy_execution.enums import LedgerEntryType
from strategy_execution.repository import StrategyLedgerRepository

logger = logging.getLogger(__name__)

SAFETY_BUFFER = Decimal("5")  # 默认安全垫 5 USDC


class AccountExecutionLedger:
    """单个执行账户的内存资金状态。"""

    def __init__(
        self,
        account_id: int,
        initial_cash: Decimal = Decimal("0"),
        safety_buffer: Decimal = SAFETY_BUFFER,
        ledger_repo: Optional[StrategyLedgerRepository] = None,
    ) -> None:
        self.account_id = account_id
        self._lock = asyncio.Lock()
        self._ledger_repo = ledger_repo or StrategyLedgerRepository()

        # 真实余额（由轮询更新）
        self._real_cash = initial_cash
        self._safety_buffer = safety_buffer

        # 保留资金（BUY 挂单占用）
        self._reserved_buy: Decimal = Decimal("0")

        # 每 token 持仓
        self._positions: Dict[str, Decimal] = {}
        # 每 token SELL 保留份额
        self._reserved_sell: Dict[str, Decimal] = {}

        # 最后校准时间
        self._last_calibrated_at: float = 0

    @property
    def available_cash(self) -> Decimal:
        """当前可用于新 BUY 的现金。"""
        return max(Decimal("0"), self._real_cash - self._reserved_buy - self._safety_buffer)

    def available_shares(self, token_id: str) -> Decimal:
        """当前可用于 SELL 的份额。"""
        pos = self._positions.get(token_id, Decimal("0"))
        reserved = self._reserved_sell.get(token_id, Decimal("0"))
        return max(Decimal("0"), pos - reserved)

    def max_buy_shares(self, price: Decimal) -> Decimal:
        """以指定价格可买的最大份额（向下取整）。"""
        if price <= 0:
            return Decimal("0")
        return (self.available_cash / price).quantize(Decimal("1"), rounding=ROUND_DOWN)

    async def reserve_buy(
        self,
        *,
        run_id: str,
        order_id: str,
        token_id: str,
        price: Decimal,
        shares: Decimal,
    ) -> bool:
        """原子保留 BUY 资金。成功返回 True，余额不足返回 False。"""
        notional = price * shares
        async with self._lock:
            if self.available_cash < notional:
                logger.warning(
                    "Insufficient cash for account %d: need %s, available %s",
                    self.account_id, notional, self.available_cash,
                )
                return False

            self._reserved_buy += notional
            self._persist_entry(
                entry_type=LedgerEntryType.BUY_RESERVED,
                run_id=run_id,
                order_id=order_id,
                token_id=token_id,
                reserved_cash_delta=notional,
                dedupe_key=f"reserve_buy:{order_id}",
            )
            return True

    async def release_buy(
        self,
        *,
        run_id: str,
        order_id: str,
        token_id: str,
        price: Decimal,
        unrealized_shares: Decimal,
    ) -> None:
        """释放未成交 BUY 的保留资金（撤单/请求失败）。"""
        notional = price * unrealized_shares
        async with self._lock:
            self._reserved_buy = max(Decimal("0"), self._reserved_buy - notional)
            self._persist_entry(
                entry_type=LedgerEntryType.BUY_RELEASED,
                run_id=run_id,
                order_id=order_id,
                token_id=token_id,
                reserved_cash_delta=-notional,
                dedupe_key=f"release_buy:{order_id}:{unrealized_shares}",
            )

    async def confirm_buy_fill(
        self,
        *,
        run_id: str,
        order_id: str,
        token_id: str,
        price: Decimal,
        filled_shares: Decimal,
        trade_id: str,
    ) -> None:
        """BUY 成交：释放保留并增加持仓。"""
        notional = price * filled_shares
        async with self._lock:
            self._reserved_buy = max(Decimal("0"), self._reserved_buy - notional)
            self._real_cash -= notional
            self._positions[token_id] = self._positions.get(token_id, Decimal("0")) + filled_shares
            self._persist_entry(
                entry_type=LedgerEntryType.FILL_BUY,
                run_id=run_id,
                order_id=order_id,
                token_id=token_id,
                cash_delta=-notional,
                shares_delta=filled_shares,
                reserved_cash_delta=-notional,
                dedupe_key=f"fill_buy:{order_id}:{trade_id}",
            )

    async def reserve_sell(
        self,
        *,
        run_id: str,
        order_id: str,
        token_id: str,
        shares: Decimal,
    ) -> bool:
        """原子保留 SELL 份额。"""
        async with self._lock:
            if self.available_shares(token_id) < shares:
                logger.warning(
                    "Insufficient shares for account %d token %s: need %s, available %s",
                    self.account_id, token_id, shares, self.available_shares(token_id),
                )
                return False

            self._reserved_sell[token_id] = self._reserved_sell.get(token_id, Decimal("0")) + shares
            self._persist_entry(
                entry_type=LedgerEntryType.SELL_RESERVED,
                run_id=run_id,
                order_id=order_id,
                token_id=token_id,
                reserved_shares_delta=shares,
                dedupe_key=f"reserve_sell:{order_id}",
            )
            return True

    async def release_sell(
        self,
        *,
        run_id: str,
        order_id: str,
        token_id: str,
        unrealized_shares: Decimal,
    ) -> None:
        """释放未成交 SELL 的保留份额。"""
        async with self._lock:
            current = self._reserved_sell.get(token_id, Decimal("0"))
            self._reserved_sell[token_id] = max(Decimal("0"), current - unrealized_shares)
            self._persist_entry(
                entry_type=LedgerEntryType.SELL_RELEASED,
                run_id=run_id,
                order_id=order_id,
                token_id=token_id,
                reserved_shares_delta=-unrealized_shares,
                dedupe_key=f"release_sell:{order_id}:{unrealized_shares}",
            )

    async def confirm_sell_fill(
        self,
        *,
        run_id: str,
        order_id: str,
        token_id: str,
        price: Decimal,
        filled_shares: Decimal,
        trade_id: str,
    ) -> None:
        """SELL 成交：释放保留份额、减少持仓、增加现金。"""
        async with self._lock:
            current_reserved = self._reserved_sell.get(token_id, Decimal("0"))
            self._reserved_sell[token_id] = max(Decimal("0"), current_reserved - filled_shares)
            self._positions[token_id] = max(
                Decimal("0"), self._positions.get(token_id, Decimal("0")) - filled_shares
            )
            self._real_cash += price * filled_shares
            self._persist_entry(
                entry_type=LedgerEntryType.FILL_SELL,
                run_id=run_id,
                order_id=order_id,
                token_id=token_id,
                cash_delta=price * filled_shares,
                shares_delta=-filled_shares,
                reserved_shares_delta=-filled_shares,
                dedupe_key=f"fill_sell:{order_id}:{trade_id}",
            )

    async def calibrate(
        self,
        real_cash: Decimal,
        positions: Dict[str, Decimal],
    ) -> None:
        """轮询校准 — 用外部真相更新 baseline，不覆盖保留状态。"""
        async with self._lock:
            old_cash = self._real_cash
            self._real_cash = real_cash

            for token_id, size in positions.items():
                self._positions[token_id] = size
            # 移除已清零的持仓
            for token_id in list(self._positions.keys()):
                if token_id not in positions:
                    self._positions[token_id] = Decimal("0")

            self._last_calibrated_at = time.time()

            if old_cash != real_cash:
                self._persist_entry(
                    entry_type=LedgerEntryType.RECONCILE,
                    run_id=None,
                    order_id=None,
                    token_id=None,
                    cash_delta=real_cash - old_cash,
                    dedupe_key=f"calibrate:{self.account_id}:{int(self._last_calibrated_at * 1000)}",
                    metadata={"old_cash": str(old_cash), "new_cash": str(real_cash)},
                )

    def snapshot(self) -> Dict[str, Any]:
        """返回当前状态快照（调试用）。"""
        return {
            "account_id": self.account_id,
            "real_cash": str(self._real_cash),
            "reserved_buy": str(self._reserved_buy),
            "available_cash": str(self.available_cash),
            "safety_buffer": str(self._safety_buffer),
            "positions": {k: str(v) for k, v in self._positions.items()},
            "reserved_sell": {k: str(v) for k, v in self._reserved_sell.items()},
            "last_calibrated_at": self._last_calibrated_at,
        }

    def _persist_entry(
        self,
        *,
        entry_type: LedgerEntryType,
        run_id: Optional[str],
        order_id: Optional[str],
        token_id: Optional[str],
        cash_delta: Decimal = Decimal("0"),
        shares_delta: Decimal = Decimal("0"),
        reserved_cash_delta: Decimal = Decimal("0"),
        reserved_shares_delta: Decimal = Decimal("0"),
        dedupe_key: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """异步持久化账本条目 — 失败不阻塞内存操作。"""
        try:
            self._ledger_repo.append({
                "account_id": self.account_id,
                "run_id": run_id,
                "order_id": order_id,
                "token_id": token_id,
                "entry_type": entry_type.value,
                "cash_delta": cash_delta,
                "shares_delta": shares_delta,
                "reserved_cash_delta": reserved_cash_delta,
                "reserved_shares_delta": reserved_shares_delta,
                "dedupe_key": dedupe_key,
                "occurred_at": now_utc8_dt(),
                "metadata_json": metadata,
            })
        except Exception:
            logger.exception("Failed to persist ledger entry: %s", dedupe_key)


# ─── 账本管理器 ───────────────────────────────────────────────────────────────


class LedgerManager:
    """管理多个账户的 AccountExecutionLedger 实例。"""

    def __init__(self) -> None:
        self._ledgers: Dict[int, AccountExecutionLedger] = {}

    def get_or_create(self, account_id: int, initial_cash: Decimal = Decimal("0")) -> AccountExecutionLedger:
        if account_id not in self._ledgers:
            self._ledgers[account_id] = AccountExecutionLedger(
                account_id=account_id,
                initial_cash=initial_cash,
            )
        return self._ledgers[account_id]

    def get(self, account_id: int) -> Optional[AccountExecutionLedger]:
        return self._ledgers.get(account_id)

    def all_snapshots(self) -> Dict[int, Dict[str, Any]]:
        return {aid: ledger.snapshot() for aid, ledger in self._ledgers.items()}

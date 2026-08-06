"""PnL 服务：定时拉取账户总金额存库，通过 WS 推送新数据"""
import asyncio
import logging
from typing import Optional, List, Set

from account.service import get_account_service
from shared.balance import fetch_address_value
from shared.time_utils import now_utc8_dt
from .models import batch_insert_balance_history, get_balance_history

logger = logging.getLogger(__name__)

POLL_INTERVAL = 300  # 5 分钟
MAX_CONCURRENT_FETCHES = 5


class PnlService:
    def __init__(self):
        self._poller_task: Optional[asyncio.Task] = None
        self._ws_connections: Set[asyncio.Queue] = set()
        self._last_values: dict[str, float] = {}

    def start(self):
        self._poller_task = asyncio.create_task(self._poll_loop())
        logger.info(f"[PnL] Balance poller started ({POLL_INTERVAL}s interval)")

    def stop(self):
        if self._poller_task:
            self._poller_task.cancel()
            self._poller_task = None

    def add_ws_queue(self, queue: asyncio.Queue):
        self._ws_connections.add(queue)

    def remove_ws_queue(self, queue: asyncio.Queue):
        self._ws_connections.discard(queue)

    async def _poll_loop(self):
        await asyncio.sleep(10)
        while True:
            try:
                account_svc = get_account_service()
                wallets = [acc["proxy_wallet"] for acc in account_svc.get_all_accounts()]
                records = await self._collect_snapshots(wallets)
                if records:
                    await asyncio.to_thread(batch_insert_balance_history, records)
                    await self._broadcast(records)
                    logger.debug(f"[PnL] Recorded {len(records)} balance snapshots")
            except Exception as e:
                logger.error(f"[PnL] Poll error: {e}")
            await asyncio.sleep(POLL_INTERVAL)

    async def _collect_snapshots(self, wallets: List[str]) -> List[dict]:
        now = now_utc8_dt()
        sem = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)

        async def _fetch(w: str):
            async with sem:
                return await fetch_address_value(w)

        results = await asyncio.gather(*(_fetch(w) for w in wallets))
        records = []
        for wallet, (balance, position_value) in zip(wallets, results):
            if balance is None or position_value is None:
                total_value = self._last_values.get(wallet, 0.0)
            else:
                total_value = balance + position_value
                self._last_values[wallet] = total_value
            records.append({
                "proxy_wallet": wallet,
                "total_value": total_value,
                "created_at": now,
            })
        return records

    async def _broadcast(self, records: List[dict]):
        if not self._ws_connections:
            return
        data = [
            {"proxy_wallet": r["proxy_wallet"], "total_value": r["total_value"], "created_at": r["created_at"].isoformat()}
            for r in records
        ]
        msg = {"event_type": "balance_update", "data": data}
        dead = set()
        for queue in self._ws_connections:
            try:
                queue.put_nowait(msg)
            except asyncio.QueueFull:
                dead.add(queue)
        for q in dead:
            self._ws_connections.discard(q)


_pnl_service: Optional[PnlService] = None


def get_pnl_service() -> PnlService:
    global _pnl_service
    if _pnl_service is None:
        _pnl_service = PnlService()
    return _pnl_service

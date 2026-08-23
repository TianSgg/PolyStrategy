from __future__ import annotations

import json
import logging
from typing import Any

from shared.db import get_db_pool

logger = logging.getLogger(__name__)


class MySQLStateStore:
    """策略运行状态持久化 — MySQL 实现。"""

    def __init__(self, table: str = "strategy_state") -> None:
        self._table = table

    async def save(self, run_id: str, state: dict[str, Any]) -> None:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"INSERT INTO {self._table} (run_id, state_json) "
                    f"VALUES (%s, %s) "
                    f"ON DUPLICATE KEY UPDATE state_json = VALUES(state_json)",
                    (run_id, json.dumps(state)),
                )

    async def load(self, run_id: str) -> dict[str, Any] | None:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT state_json FROM {self._table} WHERE run_id = %s",
                    (run_id,),
                )
                row = await cur.fetchone()
                if row:
                    return json.loads(row[0])
                return None

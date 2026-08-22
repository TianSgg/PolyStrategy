"""策略执行领域 DAO — 三个配置表 + 运行 + 订单 + 账本的 SQL 操作。"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from shared.db import get_db
from strategy_execution.enums import StrategyType

logger = logging.getLogger(__name__)

# 策略类型 → 配置表名映射
_CONFIG_TABLE: dict[StrategyType, str] = {
    StrategyType.SWEEP: "strategy_sweep_configs",
    StrategyType.LEADER: "strategy_leader_configs",
    StrategyType.SWEEP_LEADER: "strategy_sweep_leader_configs",
}


# ─── 配置表 DAO ──────────────────────────────────────────────────────────────


class StrategyConfigRepository:
    """通用策略配置 CRUD — 按 strategy_type 路由到对应表。"""

    def create(self, strategy_type: StrategyType, data: dict[str, Any]) -> int:
        table = _CONFIG_TABLE[strategy_type]
        columns = list(data.keys())
        placeholders = ", ".join(f"%({c})s" for c in columns)
        col_str = ", ".join(columns)
        sql = f"INSERT INTO {table} ({col_str}) VALUES ({placeholders})"
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, data)
                conn.commit()
                return cur.lastrowid

    def find_by_id(self, strategy_type: StrategyType, config_id: int) -> dict[str, Any] | None:
        table = _CONFIG_TABLE[strategy_type]
        sql = f"SELECT * FROM {table} WHERE id = %s AND deleted_at IS NULL"
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (config_id,))
                row = cur.fetchone()
                return self._row_to_dict(cur, row) if row else None

    def list_by_owner(
        self,
        strategy_type: StrategyType,
        owner_user_id: int,
        include_disabled: bool = False,
    ) -> list[dict[str, Any]]:
        table = _CONFIG_TABLE[strategy_type]
        conditions = ["owner_user_id = %s", "deleted_at IS NULL"]
        params: list[Any] = [owner_user_id]
        if not include_disabled:
            conditions.append("enabled = 1")
        where = " AND ".join(conditions)
        sql = f"SELECT * FROM {table} WHERE {where} ORDER BY created_at DESC"
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
                return [self._row_to_dict(cur, r) for r in rows]

    def list_all_enabled(self, strategy_type: StrategyType) -> list[dict[str, Any]]:
        table = _CONFIG_TABLE[strategy_type]
        sql = f"SELECT * FROM {table} WHERE enabled = 1 AND deleted_at IS NULL"
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()
                return [self._row_to_dict(cur, r) for r in rows]

    def update(self, strategy_type: StrategyType, config_id: int, data: dict[str, Any]) -> bool:
        table = _CONFIG_TABLE[strategy_type]
        set_clause = ", ".join(f"{k} = %({k})s" for k in data.keys())
        sql = f"UPDATE {table} SET {set_clause}, updated_at = NOW(3) WHERE id = %(id)s AND deleted_at IS NULL"
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {**data, "id": config_id})
                conn.commit()
                return cur.rowcount > 0

    def soft_delete(self, strategy_type: StrategyType, config_id: int) -> bool:
        table = _CONFIG_TABLE[strategy_type]
        sql = f"UPDATE {table} SET deleted_at = NOW(3), enabled = 0, updated_at = NOW(3) WHERE id = %s AND deleted_at IS NULL"
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (config_id,))
                conn.commit()
                return cur.rowcount > 0

    def _row_to_dict(self, cursor, row) -> dict[str, Any]:
        columns = [desc[0] for desc in cursor.description]
        return dict(zip(columns, row))


# ─── 运行 DAO ────────────────────────────────────────────────────────────────


class StrategyRunRepository:

    def create(self, run: dict[str, Any]) -> str:
        sql = """
            INSERT INTO strategy_runs
                (id, strategy_type, strategy_config_id, token_id,
                 market_slug, question, outcome,
                 state, status, active_key,
                 params_snapshot_json, started_by_signal_id,
                 started_at)
            VALUES
                (%(id)s, %(strategy_type)s, %(strategy_config_id)s, %(token_id)s,
                 %(market_slug)s, %(question)s, %(outcome)s,
                 %(state)s, %(status)s, %(active_key)s,
                 %(params_snapshot_json)s, %(started_by_signal_id)s,
                 %(started_at)s)
        """
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {
                    **run,
                    "params_snapshot_json": json.dumps(run.get("params_snapshot_json", {}), ensure_ascii=False),
                })
                conn.commit()
                return run["id"]

    def find_by_id(self, run_id: str) -> dict[str, Any] | None:
        sql = "SELECT * FROM strategy_runs WHERE id = %s"
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (run_id,))
                row = cur.fetchone()
                return self._row_to_dict(cur, row) if row else None

    def find_active_by_key(self, active_key: str) -> dict[str, Any] | None:
        sql = "SELECT * FROM strategy_runs WHERE active_key = %s"
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (active_key,))
                row = cur.fetchone()
                return self._row_to_dict(cur, row) if row else None

    def update_state(
        self,
        run_id: str,
        *,
        state: str,
        status: str | None = None,
        expected_version: int,
        extra_fields: dict[str, Any] | None = None,
    ) -> bool:
        """乐观锁状态更新。"""
        sets = ["state = %s", "version = version + 1", "updated_at = NOW(3)"]
        params: list[Any] = [state]
        if status:
            sets.append("status = %s")
            params.append(status)
        if extra_fields:
            for k, v in extra_fields.items():
                sets.append(f"{k} = %s")
                params.append(v)
        set_clause = ", ".join(sets)
        sql = f"UPDATE strategy_runs SET {set_clause} WHERE id = %s AND version = %s"
        params.extend([run_id, expected_version])
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                conn.commit()
                return cur.rowcount > 0

    def list_by_config(
        self,
        strategy_type: str,
        config_id: int,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conditions = ["strategy_type = %s", "strategy_config_id = %s"]
        params: list[Any] = [strategy_type, config_id]
        if status:
            conditions.append("status = %s")
            params.append(status)
        where = " AND ".join(conditions)
        sql = f"SELECT * FROM strategy_runs WHERE {where} ORDER BY started_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
                return [self._row_to_dict(cur, r) for r in rows]

    def list_active(self) -> list[dict[str, Any]]:
        sql = "SELECT * FROM strategy_runs WHERE status = 'ACTIVE'"
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()
                return [self._row_to_dict(cur, r) for r in rows]

    def _row_to_dict(self, cursor, row) -> dict[str, Any]:
        columns = [desc[0] for desc in cursor.description]
        d = dict(zip(columns, row))
        if isinstance(d.get("params_snapshot_json"), str):
            d["params_snapshot_json"] = json.loads(d["params_snapshot_json"])
        return d


# ─── 订单 DAO ────────────────────────────────────────────────────────────────


class StrategyOrderRepository:

    def create(self, order: dict[str, Any]) -> str:
        sql = """
            INSERT INTO strategy_orders
                (id, run_id, client_order_id, purpose, execution_mode,
                 side, status, limit_price, requested_size, sent_at)
            VALUES
                (%(id)s, %(run_id)s, %(client_order_id)s, %(purpose)s, %(execution_mode)s,
                 %(side)s, %(status)s, %(limit_price)s, %(requested_size)s, %(sent_at)s)
        """
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, order)
                conn.commit()
                return order["id"]

    def update_response(
        self,
        client_order_id: str,
        *,
        clob_order_id: str | None,
        status: str,
        responded_at: datetime | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        sql = """
            UPDATE strategy_orders
            SET clob_order_id = %s, status = %s, responded_at = %s,
                error_code = %s, error_message = %s, updated_at = NOW(3)
            WHERE client_order_id = %s
        """
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (clob_order_id, status, responded_at, error_code, error_message, client_order_id))
                conn.commit()
                return cur.rowcount > 0

    def update_fill(
        self,
        client_order_id: str,
        *,
        matched_size: Decimal,
        avg_matched_price: Decimal | None,
        status: str,
    ) -> bool:
        sql = """
            UPDATE strategy_orders
            SET matched_size = %s, avg_matched_price = %s, status = %s, updated_at = NOW(3)
            WHERE client_order_id = %s
        """
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (matched_size, avg_matched_price, status, client_order_id))
                conn.commit()
                return cur.rowcount > 0

    def find_by_run(self, run_id: str) -> list[dict[str, Any]]:
        sql = "SELECT * FROM strategy_orders WHERE run_id = %s ORDER BY created_at"
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (run_id,))
                rows = cur.fetchall()
                return [self._row_to_dict(cur, r) for r in rows]

    def find_open_by_run(self, run_id: str) -> list[dict[str, Any]]:
        sql = "SELECT * FROM strategy_orders WHERE run_id = %s AND status IN ('PENDING', 'LIVE', 'DELAYED')"
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (run_id,))
                rows = cur.fetchall()
                return [self._row_to_dict(cur, r) for r in rows]

    def _row_to_dict(self, cursor, row) -> dict[str, Any]:
        columns = [desc[0] for desc in cursor.description]
        return dict(zip(columns, row))


# ─── 运行事件 DAO ─────────────────────────────────────────────────────────────


class StrategyRunEventRepository:

    def append(self, event: dict[str, Any]) -> int:
        sql = """
            INSERT INTO strategy_run_events
                (run_id, sequence_no, event_type, occurred_at, monotonic_ns, payload_json)
            VALUES
                (%(run_id)s, %(sequence_no)s, %(event_type)s, %(occurred_at)s, %(monotonic_ns)s, %(payload_json)s)
        """
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {
                    **event,
                    "payload_json": json.dumps(event.get("payload_json", {}), ensure_ascii=False),
                })
                conn.commit()
                return cur.lastrowid

    def list_by_run(self, run_id: str) -> list[dict[str, Any]]:
        sql = "SELECT * FROM strategy_run_events WHERE run_id = %s ORDER BY sequence_no"
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (run_id,))
                rows = cur.fetchall()
                return [self._row_to_dict(cur, r) for r in rows]

    def next_sequence_no(self, run_id: str) -> int:
        sql = "SELECT COALESCE(MAX(sequence_no), 0) + 1 FROM strategy_run_events WHERE run_id = %s"
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (run_id,))
                return cur.fetchone()[0]

    def _row_to_dict(self, cursor, row) -> dict[str, Any]:
        columns = [desc[0] for desc in cursor.description]
        d = dict(zip(columns, row))
        if isinstance(d.get("payload_json"), str):
            d["payload_json"] = json.loads(d["payload_json"])
        return d


# ─── 账本 DAO ─────────────────────────────────────────────────────────────────


class StrategyLedgerRepository:

    def append(self, entry: dict[str, Any]) -> bool:
        """插入账本条目。dedupe_key 冲突时幂等忽略。"""
        sql = """
            INSERT INTO strategy_account_ledger
                (account_id, run_id, order_id, token_id,
                 entry_type, cash_delta, shares_delta,
                 reserved_cash_delta, reserved_shares_delta,
                 dedupe_key, occurred_at, metadata_json)
            VALUES
                (%(account_id)s, %(run_id)s, %(order_id)s, %(token_id)s,
                 %(entry_type)s, %(cash_delta)s, %(shares_delta)s,
                 %(reserved_cash_delta)s, %(reserved_shares_delta)s,
                 %(dedupe_key)s, %(occurred_at)s, %(metadata_json)s)
            ON DUPLICATE KEY UPDATE id = id
        """
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {
                    **entry,
                    "metadata_json": json.dumps(entry.get("metadata_json"), ensure_ascii=False)
                    if entry.get("metadata_json") else None,
                })
                conn.commit()
                return cur.rowcount > 0

    def list_by_account(self, account_id: int, limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT * FROM strategy_account_ledger WHERE account_id = %s ORDER BY occurred_at DESC LIMIT %s"
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (account_id, limit))
                rows = cur.fetchall()
                return [self._row_to_dict(cur, r) for r in rows]

    def list_by_run(self, run_id: str) -> list[dict[str, Any]]:
        sql = "SELECT * FROM strategy_account_ledger WHERE run_id = %s ORDER BY occurred_at"
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (run_id,))
                rows = cur.fetchall()
                return [self._row_to_dict(cur, r) for r in rows]

    def sum_by_account(self, account_id: int) -> dict[str, Decimal]:
        """计算账户维度的累计 delta。"""
        sql = """
            SELECT
                COALESCE(SUM(cash_delta), 0) AS total_cash_delta,
                COALESCE(SUM(reserved_cash_delta), 0) AS total_reserved_cash
            FROM strategy_account_ledger
            WHERE account_id = %s
        """
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (account_id,))
                row = cur.fetchone()
                return {
                    "total_cash_delta": Decimal(str(row[0])),
                    "total_reserved_cash": Decimal(str(row[1])),
                }

    def _row_to_dict(self, cursor, row) -> dict[str, Any]:
        columns = [desc[0] for desc in cursor.description]
        d = dict(zip(columns, row))
        if isinstance(d.get("metadata_json"), str):
            d["metadata_json"] = json.loads(d["metadata_json"])
        return d

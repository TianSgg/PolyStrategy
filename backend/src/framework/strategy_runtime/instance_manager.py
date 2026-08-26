"""策略实例管理器 — 从 DB 加载配置、创建/停止/热更新实例。"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from framework.db import get_db

logger = logging.getLogger(__name__)


@dataclass
class StrategyInstanceConfig:
    """一个策略实例的完整配置。"""

    id: int
    owner_user_id: int
    account_id: int
    proxy_wallet: str
    name: str
    enabled: bool
    params: dict[str, Any] = field(default_factory=dict)
    params_version: int = 1


class InstanceManager:
    """从 DB 加载指定策略类型的所有 enabled 配置。"""

    def __init__(self, strategy_type: str, config_table: str) -> None:
        self._strategy_type = strategy_type
        self._config_table = config_table

    def load_enabled(self) -> list[StrategyInstanceConfig]:
        sql = f"""
            SELECT c.id, c.owner_user_id, c.account_id, c.name, c.enabled,
                   c.fixed_entry_shares, c.entry_wait_ms, c.sweep_outcome_filter,
                   c.signal_source_filter, c.signal_threshold_filter,
                   c.stop_loss_ratio, c.exit_wait_ms, c.tick_verify_retries,
                   c.tick_verify_backoff_ms, c.params_version,
                   a.proxy_wallet
            FROM {self._config_table} c
            JOIN accounts a ON a.id = c.account_id
            WHERE c.enabled = 1 AND c.deleted_at IS NULL
            ORDER BY c.id
        """
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                columns = [desc[0] for desc in cur.description]
                rows = cur.fetchall()

        configs: list[StrategyInstanceConfig] = []
        for row in rows:
            d = dict(zip(columns, row))
            params = {
                "fixed_entry_shares": str(d["fixed_entry_shares"]),
                "entry_wait_ms": int(d["entry_wait_ms"]),
                "sweep_outcome_filter": d.get("sweep_outcome_filter", "no"),
                "signal_source_filter": d.get("signal_source_filter", "all"),
                "signal_threshold_filter": d.get("signal_threshold_filter", "all"),
                "stop_loss_ratio": str(d["stop_loss_ratio"]),
                "exit_wait_ms": int(d["exit_wait_ms"]),
                "tick_verify_retries": int(d["tick_verify_retries"]),
                "tick_verify_backoff_ms": int(d["tick_verify_backoff_ms"]),
            }
            configs.append(StrategyInstanceConfig(
                id=d["id"],
                owner_user_id=d["owner_user_id"],
                account_id=d["account_id"],
                proxy_wallet=d["proxy_wallet"] or "",
                name=d["name"],
                enabled=bool(d["enabled"]),
                params=params,
                params_version=d["params_version"],
            ))

        logger.info(
            "[%s] Loaded %d enabled instance configs",
            self._strategy_type, len(configs),
        )
        return configs

    def load_by_id(self, config_id: int) -> StrategyInstanceConfig | None:
        sql = f"""
            SELECT c.id, c.owner_user_id, c.account_id, c.name, c.enabled,
                   c.fixed_entry_shares, c.entry_wait_ms, c.sweep_outcome_filter,
                   c.signal_source_filter, c.signal_threshold_filter,
                   c.stop_loss_ratio, c.exit_wait_ms, c.tick_verify_retries,
                   c.tick_verify_backoff_ms, c.params_version,
                   a.proxy_wallet
            FROM {self._config_table} c
            JOIN accounts a ON a.id = c.account_id
            WHERE c.id = %s AND c.deleted_at IS NULL
        """
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (config_id,))
                columns = [desc[0] for desc in cur.description]
                row = cur.fetchone()

        if not row:
            return None

        d = dict(zip(columns, row))
        params = {
            "fixed_entry_shares": str(d["fixed_entry_shares"]),
            "entry_wait_ms": int(d["entry_wait_ms"]),
            "sweep_outcome_filter": d.get("sweep_outcome_filter", "no"),
            "signal_source_filter": d.get("signal_source_filter", "all"),
            "signal_threshold_filter": d.get("signal_threshold_filter", "all"),
            "stop_loss_ratio": str(d["stop_loss_ratio"]),
            "exit_wait_ms": int(d["exit_wait_ms"]),
            "tick_verify_retries": int(d["tick_verify_retries"]),
            "tick_verify_backoff_ms": int(d["tick_verify_backoff_ms"]),
        }
        return StrategyInstanceConfig(
            id=d["id"],
            owner_user_id=d["owner_user_id"],
            account_id=d["account_id"],
            proxy_wallet=d["proxy_wallet"] or "",
            name=d["name"],
            enabled=bool(d["enabled"]),
            params=params,
            params_version=d["params_version"],
        )

    def config_snapshot(self, cfg: StrategyInstanceConfig) -> dict[str, Any]:
        """生成用于事件记录的配置快照。"""
        return {
            "config_id": cfg.id,
            "name": cfg.name,
            "params_version": cfg.params_version,
            **cfg.params,
        }

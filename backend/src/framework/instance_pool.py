"""通用多实例管理器 — 策略按需使用。

不强制任何接口。策略提供三个回调函数：
  - config_loader: 从哪里加载配置（DB、文件、whatever）
  - create_instance: 怎么创建一个实例
  - destroy_instance: 怎么销毁一个实例（含强制退出逻辑）

用法：
    pool = InstancePool(
        config_loader=lambda: dao.load_enabled_configs(),
        create_instance=create_strategy_instance,
        destroy_instance=destroy_strategy_instance,
    )
    await pool.start()
    await pool.reload()  # 热更新
    await pool.stop()
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass
class InstanceConfig:
    """实例配置的最小约束 — 只需要 id 和 version 来判断变更。"""
    id: int
    version: int
    data: dict[str, Any]


class InstancePool:
    """通用多实例生命周期管理器。"""

    def __init__(
        self,
        config_loader: Callable[[], list[InstanceConfig]],
        create_instance: Callable[[InstanceConfig], Awaitable[Any]],
        destroy_instance: Callable[[Any, str], Awaitable[None]],
    ) -> None:
        self._load = config_loader
        self._create = create_instance
        self._destroy = destroy_instance
        self._instances: dict[int, _Running] = {}
        self._running = False

    @property
    def instance_count(self) -> int:
        return len(self._instances)

    @property
    def instance_ids(self) -> list[int]:
        return list(self._instances.keys())

    def get_instance(self, config_id: int) -> Any | None:
        r = self._instances.get(config_id)
        return r.instance if r else None

    def all_instances(self) -> list[Any]:
        return [r.instance for r in self._instances.values()]

    async def start(self) -> None:
        configs = self._load()
        for cfg in configs:
            await self._start_one(cfg)
        self._running = True
        logger.info("InstancePool started: %d instances", len(self._instances))

    async def reload(self) -> dict[str, list[int]]:
        """热更新：增/删/改实例。返回变动详情。"""
        configs = self._load()
        config_map = {c.id: c for c in configs}

        stopped, started, reloaded = [], [], []

        for cid in list(self._instances.keys()):
            if cid not in config_map:
                await self._stop_one(cid, "config_disabled")
                stopped.append(cid)

        for cfg in configs:
            existing = self._instances.get(cfg.id)
            if existing is None:
                await self._start_one(cfg)
                started.append(cfg.id)
            elif existing.version != cfg.version:
                await self._stop_one(cfg.id, "config_changed")
                await self._start_one(cfg)
                reloaded.append(cfg.id)

        result = {"stopped": stopped, "started": started, "reloaded": reloaded}
        logger.info("InstancePool reload: %s", result)
        return result

    async def stop(self) -> None:
        self._running = False
        for cid in list(self._instances.keys()):
            await self._stop_one(cid, "shutdown")
        logger.info("InstancePool stopped")

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok" if self._running else "stopped",
            "instances": len(self._instances),
            "instance_ids": list(self._instances.keys()),
        }

    async def _start_one(self, cfg: InstanceConfig) -> None:
        try:
            instance = await self._create(cfg)
            self._instances[cfg.id] = _Running(
                config_id=cfg.id, version=cfg.version, instance=instance,
            )
        except Exception:
            logger.exception("Failed to create instance %d", cfg.id)

    async def _stop_one(self, config_id: int, reason: str) -> None:
        r = self._instances.pop(config_id, None)
        if not r:
            return
        try:
            await self._destroy(r.instance, reason)
        except Exception:
            logger.exception("Failed to destroy instance %d", config_id)


@dataclass
class _Running:
    config_id: int
    version: int
    instance: Any

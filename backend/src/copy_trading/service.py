"""跟单服务核心逻辑"""
import asyncio
import hashlib
import logging
import math
import pymysql
import aiohttp
import requests
import time
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
from enum import Enum
from typing import Dict, Set, Optional, List


from .types import CopyTradingConfig, ActivitySignal, PlaceOrderResult
from market import get_market_service
from .predexon import get_copy_trading_predexon
from account.service import get_account_service
from leader.service import get_leader_service
from event_bus import get_event_bus
from .models import (
    get_copy_trading_configs,
    delete_follower_positions,
    record_copy_trading_order,
    get_order_by_id,
    update_copy_trading_order,
    update_order_sweep_to_leader,
    create_copy_trading_config,
    update_copy_trading_config,
    delete_copy_trading_config,
    get_orders_by_config_id,
    get_asset_question_and_outcome,
    batch_upsert_asset_questions,
    upsert_follower_position,
    batch_upsert_follower_positions,
    upsert_follower_pending_sell,
    upsert_follower_pending_buy,
    batch_upsert_follower_pending_sell,
    batch_upsert_follower_pending_buy,
    batch_upsert_config_asset,
    get_assets_of_cfg_from_db,
)

logger = logging.getLogger(__name__)
ORDER_MATCH_EPSILON = 0.0001


class WeatherAssetState(str, Enum):
    IDLE = "idle"
    SWEEP_PENDING = "sweep_pending"
    ACTIVE = "active"


@dataclass
class SweepEntry:
    state: WeatherAssetState = WeatherAssetState.IDLE
    order_id: Optional[str] = None
    entry_time: float = 0
    filled_size: float = 0
    timer_task: Optional[asyncio.Task] = None


# 全局服务实例（供 RTDS 和 API 使用）
copy_trading_service: Optional['CopyTradingService'] = None


class CopyTradingService:
    """
    跟单服务
    """

    def __init__(self):
        # 配置缓存: {leader_proxy_wallet: CopyTradingConfig}（一个 leader 只对应一个 config）
        self._leader_addr_to_configs: Dict[str, CopyTradingConfig] = {}

        # 配置缓存: {follower_proxy_wallet: {CopyTradingConfig, ...}}
        self._follower_addr_to_configs: Dict[str, Set[CopyTradingConfig]] = {}

        # 当前所有 follower 地址（去重）。一个 follower 可以跟多个 leader。
        self._followers: Set[str] = set()


        # Follower 持仓: {follower_addr: {asset_id: size}}
        self._follower_positions: Dict[str, Dict[str, float]] = {}

        # Follower 待成交 SELL 锁单: {follower_addr: {asset_id: locked_size}}
        # PLACEMENT 时增加，CANCELLATION 和 trade CONFIRMED 时减少
        self._pending_sell_orders: Dict[str, Dict[str, float]] = {}

        # Follower 待成交 BUY 锁单: {follower_addr: {asset_id: locked_size}}
        self._pending_buy_orders: Dict[str, Dict[str, float]] = {}

        # 单个 order_id 当前未成交数量，用于处理 WS 重复/增量 PLACEMENT。
        self._order_pending_sizes: Dict[str, float] = {}


        # 已处理的 transaction_hash 集合（按 leader 地址 + tx_hash 去重）
        self._processed_txs: Set[str] = set()
        # 已处理的 order_hash 集合（防止下单入口和 WS PLACEMENT 重复更新 pending）
        self._order_live_on_post_ids: Set[str] = set()
        # delayed 返回的程序下单 order_id，等待 WS 回补 PLACEMENT/UPDATE/TRADE
        self._order_delayed_on_post_ids: Set[str] = set()
        # 下单入口已处理的成交量（防止 WS trade 重复更新 position）
        # key: order_id, value: 剩余需跳过的成交量
        self._order_post_filled: Dict[str, float] = {}
        # 已处理的取消 order_id 集合（防止重复 CANCELLATION 反复释放额度）
        self._processed_canceled_order_ids: Set[str] = set()
        # 程序下单 order_id 的运行时索引，避免 WS 事件早于订单落库时无法定位 config/price
        self._order_id_to_config_id: Dict[str, int] = {}
        self._order_id_to_follow_price: Dict[str, float] = {}
        self._tx_locks: Dict[str, asyncio.Lock] = {}  # 按 leader 地址分锁

        # 账户级别的锁，用于 follower 持仓、pending 和下单状态的统一互斥
        self._addr_locks: Dict[str, asyncio.Lock] = {}


        # asset question 查询去重：{asset_id: asyncio.Event} 防止同一 asset 并发多次查询
        self._asset_fetch_events: Dict[str, asyncio.Event] = {}

        # asset_id -> 可读标签缓存: "question[Yes]" / "question[No]"
        self._asset_labels: Dict[str, str] = {}

        # config_id -> CopyTradingConfig 映射，O(1) 查找
        self._config_id_to_config: Dict[int, CopyTradingConfig] = {}

        # {(f_addr, l_addr) -> CopyTradingConfig}，解决一个 f_addr 跟多个 leader 的索引问题
        self._fl_key_to_config: Dict[str, CopyTradingConfig] = {}

        # asset_id -> 持有该 asset 的 config 集合，用于 sell 信号反查
        self._asset_to_configs: Dict[str, Set[int]] = {}

        # config_id -> 剩余可用余额（近似 1 share ≈ 1 USDC）
        self._config_balances: Dict[int, float] = {}

        # 清扫: 单次最大吃单量
        self._sweep_max_size: float = 50.0

        # 天气 asset 状态机: {(config_id, asset_id): SweepEntry}
        self._weather_states: Dict[tuple, SweepEntry] = {}
        # 已超时退出但可能有延迟成交的 sweep order: {order_id: (config_id, asset_id)}
        self._sweep_exited_orders: Dict[str, tuple] = {}

        # 天气 EventBus 订阅
        self._weather_sweep_queue: Optional[asyncio.Queue] = None
        self._weather_sweep_task: Optional[asyncio.Task] = None

        # 启动持仓轮询兜底任务（依赖 _config_id_to_config，由 initialize 填充）
        self._follower_poller_task: Optional[asyncio.Task] = None
        self._pending_poller_task: Optional[asyncio.Task] = None

        # Leader 名字解析服务
        self._leader_service = get_leader_service()

        # 账户名解析服务
        self._account_service = get_account_service()

    def _get_config_by_fl(self, f_addr: str, l_addr: str) -> Optional[CopyTradingConfig]:
        return self._fl_key_to_config.get(f_addr + "_" + l_addr)

    def _add_fl_key(self, config: CopyTradingConfig):
        key = config.follower_proxy_wallet + "_" + config.leader_proxy_wallet
        self._fl_key_to_config[key] = config

    def _remove_fl_key(self, f_addr: str, l_addr: str):
        self._fl_key_to_config.pop(f_addr + "_" + l_addr, None)

    def _asset_label(self, asset_id: str) -> str:
        """返回 asset 可读标签，如 '1234455612 - Will X happen?[Yes]'。未缓存时降级为 asset_id[:8]"""
        label = self._asset_labels.get(asset_id)
        if label:
            return label
        row = get_asset_question_and_outcome(asset_id)
        if row:
            question, outcome = row
            label = f"{asset_id[:8]} - {question}[{outcome}]"
            self._asset_labels[asset_id] = label
            return label
        return f"{asset_id[:8]}"

    def _cache_asset_label(self, asset_id: str, question: str, outcome: str = ""):
        """主动填充 asset 标签缓存"""
        label = f"{asset_id[:8]} - {question}[{outcome}]"
        self._asset_labels[asset_id] = label

    def _remove_follower_config_index(self, config: CopyTradingConfig):
        f_addr = config.follower_proxy_wallet
        configs = self._follower_addr_to_configs.get(f_addr)
        if not configs:
            return
        configs.discard(config)
        if not configs:
            self._follower_addr_to_configs.pop(f_addr, None)

    @staticmethod
    def _min_order_size(side: str, price: float, role: str) -> float:
        """BUY taker uses 1 USDC notional; maker orders keep the 5-share floor."""
        if role == "maker":
            return 5
        if side == "SELL":
            return 0.01
        return math.ceil(1.01 / price * 100) / 100

    @staticmethod
    def _is_taker_price(price: float, tick_size: str) -> bool:
        """判断 leader 是否为 taker：价格精度高于 tick_size 即为 taker"""
        return Decimal(str(price)) % Decimal(tick_size) != 0



    def _start_follower_position_poller(self, interval=3600):
        """启动 follower 仓位轮询兜底同步（WS CONFIRMED 事件的补充）"""
        async def _poll():
            for f_addr in list(self._followers):
                try:
                    await self._sync_follower_positions_from_poly(f_addr)
                except Exception as e:
                    logger.error(f"[PositionPoller] Initial follower sync error for {self._account_service.get_acc_name(f_addr)}: {e}")
            self._restore_exit_watches()
            while True:
                await asyncio.sleep(interval)
                for f_addr in list(self._followers):
                    try:
                        await self._sync_follower_positions_from_poly(f_addr)
                    except Exception as e:
                        logger.warning(f"[PositionPoller] Follower poller sync error for {self._account_service.get_acc_name(f_addr)}: {e}")

        self._follower_poller_task = asyncio.create_task(_poll())
        logger.info(f"[PositionPoller] Follower position poller started ({interval}s interval)")


    def _start_pending_poller(self, interval=3600):
        """启动 pending 轮询兜底（WS order 事件遗漏时的补充）"""
        async def _poll():
            # 启动时立即同步一次
            for f_addr in list(self._followers):
                try:
                    await self._sync_pending_orders_from_poly(f_addr)
                except Exception as e:
                    logger.error(f"[PositionPoller] Initial pending sync error for {self._account_service.get_acc_name(f_addr)}: {e}")
            while True:
                await asyncio.sleep(interval)
                for f_addr in list(self._followers):
                    try:
                        await self._sync_pending_orders_from_poly(f_addr)
                    except Exception as e:
                        logger.warning(f"[PositionPoller] Pending poller error for {self._account_service.get_acc_name(f_addr)}: {e}")

        self._pending_poller_task = asyncio.create_task(_poll())
        logger.info(f"[PositionPoller] Pending poller started ({interval}s interval)")

    def stop(self):
        """停止后台任务"""
        if self._follower_poller_task:
            self._follower_poller_task.cancel()
            self._follower_poller_task = None
        if self._pending_poller_task:
            self._pending_poller_task.cancel()
            self._pending_poller_task = None
        if self._weather_sweep_task:
            self._weather_sweep_task.cancel()
            self._weather_sweep_task = None
        if self._weather_sweep_queue:
            get_event_bus().unsubscribe("weather.sweep", self._weather_sweep_queue)
            self._weather_sweep_queue = None
        for entry in self._weather_states.values():
            if entry.timer_task:
                entry.timer_task.cancel()
        asyncio.create_task(get_market_service().stop())

    async def initialize(self):
        """启动时加载配置，各 poller 自行初始化同步"""
        market_svc = get_market_service()
        market_svc.set_exit_callback(self._on_market_exit)
        # market_svc.set_sweep_callback(self._on_sweep_signal)  # 抢筹策略暂停
        await market_svc.start()
        await self._load_configs()
        self._start_follower_position_poller(interval=120)
        self._start_pending_poller(interval=120)

        event_bus = get_event_bus()
        self._weather_sweep_queue = event_bus.subscribe("weather.sweep")
        self._weather_sweep_task = asyncio.create_task(self._consume_weather_sweep())

    def _on_market_exit(self, no_asset_id: str):
        """MarketService 窗口到期确认 tick_size=0.001，触发卖出"""
        logger.info(f"[CopyTrade] Window expired, selling {self._asset_label(no_asset_id)}")
        asyncio.create_task(self.execute_sell(no_asset_id))

    def _restore_exit_watches(self):
        """重启后从已有持仓恢复 exit watch 注册和 asset→config 映射，并扣减余额"""
        market_svc = get_market_service()
        watched = set()
        config_position_totals: Dict[int, float] = {}
        for f_addr, positions in self._follower_positions.items():
            configs = self._follower_addr_to_configs.get(f_addr, set())
            for asset_id, size in positions.items():
                if size > 0:
                    if asset_id not in watched:
                        market_svc.watch_for_exit(asset_id)
                        watched.add(asset_id)
                    for config in configs:
                        self._asset_to_configs.setdefault(asset_id, set()).add(config.id)
                        config_position_totals[config.id] = config_position_totals.get(config.id, 0) + size
        for config_id, held in config_position_totals.items():
            self._config_balances[config_id] = max(0, self._config_balances.get(config_id, 0) - held)
            logger.info(f"[CopyTrade] balance adjusted for existing position: config={config_id} held={held:.2f} remaining={self._config_balances[config_id]:.2f}")
        if watched:
            logger.info(f"[CopyTrade] Restored exit watches for {len(watched)} assets")

    async def _load_configs(self):
        """从数据库加载配置（async，供 initialize 调用）"""
        configs = get_copy_trading_configs(enabled_only=False)
        predexon = get_copy_trading_predexon()
        for config in configs:
            self._leader_addr_to_configs[config.leader_proxy_wallet] = config
            self._follower_addr_to_configs.setdefault(config.follower_proxy_wallet, set()).add(config)
            self._followers.add(config.follower_proxy_wallet)
            self._config_id_to_config[config.id] = config
            self._config_balances[config.id] = config.buy_size
            self._add_fl_key(config)
            predexon.add_leader(config.leader_proxy_wallet)

        logger.info(f"[CopyTrade] Loaded {len(self._leader_addr_to_configs)} leaders, {len(self._followers)} followers")


    async def process_signal(self, payload: dict):
        """处理 insider BUY NO 信号 — 固定价格 0.99 买入，size 由 config.buy_size 决定"""
        tx_hash = payload.get("transactionHash")
        leader_addr = payload.get("proxyWallet")

        if not tx_hash or not leader_addr:
            logger.debug(f"[CopyTrade] process_signal: missing tx_hash or leader_addr, skipping")
            return

        leader_addr = leader_addr.lower()

        config = self._leader_addr_to_configs.get(leader_addr)
        if not config or not config.enabled:
            return

        async with self._get_tx_lock(leader_addr):
            if tx_hash in self._processed_txs:
                logger.debug(f"[CopyTrade] process_signal: tx {tx_hash[:8]} already processed, skipping")
                return
            self._processed_txs.add(tx_hash)

        signal = ActivitySignal.from_payload(payload)
        if not signal:
            logger.warning(f"[CopyTrade] process_signal: failed to parse signal, skipping")
            return
        if signal.price >= 1.0 or signal.price <= 0:
            logger.debug(f"[CopyTrade] process_signal: price out of range, skipping")
            return

        if signal.side != "BUY":
            logger.debug(f"[CopyTrade] process_signal: ignoring {signal.side} signal (only BUY NO is followed)")
            return

        outcome = payload.get("outcome", "")
        if outcome.upper() != "NO":
            logger.debug(f"[CopyTrade] process_signal: ignoring outcome={outcome} (only NO is followed)")
            return

        logger.debug(f"[CopyTrade] process_signal: BUY {signal.size} @ {signal.price}, asset={self._asset_label(signal.asset)}, source={signal.source}")

        asyncio.create_task(asyncio.to_thread(batch_upsert_config_asset, [config.id], signal.asset))
        if signal.asset not in self._asset_labels:
            asyncio.create_task(self._get_asset_label(signal.asset))

        # 天气状态机检查
        state_key = (config.id, signal.asset)
        entry = self._weather_states.get(state_key)
        if entry and entry.state == WeatherAssetState.SWEEP_PENDING:
            # sweep 先入场，leader 信号到达 → 确认，转 ACTIVE，取消定时器
            if entry.timer_task:
                entry.timer_task.cancel()
                entry.timer_task = None
            sweep_to_leader_ms = round((time.time() - entry.entry_time) * 1000)
            entry.state = WeatherAssetState.ACTIVE
            if entry.order_id:
                asyncio.create_task(asyncio.to_thread(update_order_sweep_to_leader, entry.order_id, sweep_to_leader_ms))
            entry.order_id = None
            logger.info(f"[WeatherState] SWEEP_PENDING→ACTIVE: leader confirmed {self._asset_label(signal.asset)} config={config.id} delay={sweep_to_leader_ms}ms")
            return
        if entry and entry.state == WeatherAssetState.ACTIVE:
            logger.debug(f"[WeatherState] Already ACTIVE for {self._asset_label(signal.asset)} config={config.id}, skip")
            return

        await self._execute_buy(config, signal)
        self._weather_states[state_key] = SweepEntry(state=WeatherAssetState.ACTIVE)

    async def _execute_buy(self, config: CopyTradingConfig, signal: ActivitySignal):
        """对单个 config 执行 BUY NO 下单"""
        asset_id = signal.asset
        follow_price = 0.99

        balance = self._config_balances.get(config.id, 0)
        if balance < 5:
            logger.info(f"[CopyTrade] BUY skipped: balance exhausted ({balance:.2f}) config={config.id}")
            return
        follow_buy_size = balance

        f_addr = config.follower_proxy_wallet
        async with self._get_addr_lock(f_addr):
            follower_name = self._account_service.get_acc_name(config.follower_proxy_wallet)
            leader_name = self._leader_service.get_leader_name(config.leader_proxy_wallet)

            logger.info(
                f"[CopyTrade] BUY {self._asset_label(asset_id)} | "
                f"leader={leader_name} {signal.size:.2f}@{signal.price} | "
                f"follower={follower_name} {follow_buy_size:.2f}@{follow_price}"
            )

            order_start = time.time()
            result = await self._place_order(
                config, asset_id, "BUY", follow_buy_size,
                price=follow_price, tick_size="0.01", neg_risk=True
            )
            order_elapsed_ms = (time.time() - order_start) * 1000
            signal_to_result_ms = (time.time() - signal.signal_time) * 1000
            logger.debug(
                f"[CopyTrade] timer: signal_to_order_result={signal_to_result_ms:.1f}ms "
                f"order_elapsed={order_elapsed_ms:.1f}ms side=BUY asset={self._asset_label(asset_id)} "
                f"status={result.raw_status}"
            )
            self._register_post_order_result(config, result)

            new_pending = self._pending_buy_orders.setdefault(f_addr, {}).get(asset_id, 0) + result.pending_delta
            self._pending_buy_orders.setdefault(f_addr, {})[asset_id] = new_pending
            new_pos = self._follower_positions.setdefault(f_addr, {}).get(asset_id, 0) + result.position_delta
            self._follower_positions.setdefault(f_addr, {})[asset_id] = new_pos

        if result.pending_delta:
            logger.debug(f"[CopyTrade] BUY pending: {result.pending_delta:+.2f} {self._asset_label(asset_id)} (position={new_pos} pending={new_pending})")
            asyncio.create_task(self._save_pending_buy_with_question(f_addr, asset_id, new_pending))
        if result.position_delta:
            logger.info(f"[CopyTrade] BUY position: {result.position_delta:+.2f} {self._asset_label(asset_id)} (position={new_pos} pending={new_pending})")
            asyncio.create_task(asyncio.to_thread(upsert_follower_position, f_addr, asset_id, new_pos))

        if result.raw_status in ("LIVE", "MATCHED", "DELAYED"):
            get_market_service().watch_for_exit(asset_id)
            self._asset_to_configs.setdefault(asset_id, set()).add(config.id)

        self._record_and_notify_order_result(
            config=config,
            signal=signal,
            follow_price=follow_price,
            result=result,
            follower_name=follower_name,
            signal_latency_ms=round(signal_to_result_ms),
        )

    async def execute_sell(self, asset_id: str):
        """市场触发卖出 — tick_size 变为 0.001 时，以 0.999 卖出全部 NO 持仓"""
        config_ids = self._asset_to_configs.get(asset_id)
        if not config_ids:
            return
        for config_id in list(config_ids):
            config = self._config_id_to_config.get(config_id)
            if not config:
                continue
            f_addr = config.follower_proxy_wallet

            async with self._get_addr_lock(f_addr):
                follower_pos = self._follower_positions.get(f_addr, {}).get(asset_id, 0)
                pending_sell = self._pending_sell_orders.get(f_addr, {}).get(asset_id, 0)
                available_pos = max(0, follower_pos - pending_sell)
                if available_pos <= 0.01:
                    continue

                follow_price = 0.999
                follow_sell_size = available_pos
                follower_name = self._account_service.get_acc_name(f_addr)

                logger.info(
                    f"[CopyTrade] EXIT SELL {self._asset_label(asset_id)} | "
                    f"follower={follower_name} {follow_sell_size:.2f}@{follow_price}"
                )

                result = await self._place_order(
                    config, asset_id, "SELL", follow_sell_size,
                    price=follow_price, tick_size="0.001", neg_risk=True
                )
                self._register_post_order_result(config, result)

                new_pending = self._pending_sell_orders.setdefault(f_addr, {}).get(asset_id, 0) + result.pending_delta
                self._pending_sell_orders.setdefault(f_addr, {})[asset_id] = new_pending
                new_pos = max(0, self._follower_positions.setdefault(f_addr, {}).get(asset_id, 0) - result.position_delta)
                self._follower_positions.setdefault(f_addr, {})[asset_id] = new_pos

            if result.pending_delta:
                logger.debug(f"[CopyTrade] SELL pending: {result.pending_delta:+.2f} {self._asset_label(asset_id)} (position={new_pos} pending={new_pending})")
                asyncio.create_task(self._save_pending_sell_with_question(f_addr, asset_id, new_pending))
            if result.position_delta:
                logger.info(f"[CopyTrade] SELL position: {result.position_delta:+.2f} {self._asset_label(asset_id)} (position={new_pos} pending={new_pending})")
                asyncio.create_task(asyncio.to_thread(upsert_follower_position, f_addr, asset_id, new_pos))

            self._record_sell_order(config, asset_id, follow_price, follow_sell_size, result)

        market_svc = get_market_service()
        market_svc.unwatch_exit(asset_id)
        market_svc.unsubscribe(asset_id)
        # ACTIVE → IDLE
        for config_id in list(config_ids):
            state_key = (config_id, asset_id)
            if state_key in self._weather_states:
                self._weather_states.pop(state_key, None)
        logger.info(f"[CopyTrade] EXIT done, unsubscribed {self._asset_label(asset_id)}")

    def _record_sell_order(self, config: CopyTradingConfig, asset_id: str, follow_price: float, follow_sell_size: float, result: PlaceOrderResult, source: str = "EXIT_TICK_SIZE"):
        """记录 SELL 订单到 order 表"""
        order_id = result.order_id
        if result.raw_status in ("ERROR", "SKIPPED"):
            raw = f"EXIT_{asset_id}_{config.id}_{time.time()}"
            order_id = f"{result.raw_status}_0x" + hashlib.sha256(raw.encode()).hexdigest()

        asyncio.create_task(asyncio.to_thread(
            record_copy_trading_order,
            order_id=order_id,
            config_id=config.id,
            leader=config.leader_proxy_wallet,
            follower=config.follower_proxy_wallet,
            leader_tx_hash=source,
            asset_id=asset_id,
            side="SELL",
            leader_size=0,
            leader_price=0,
            follow_size=result.size,
            follow_price=follow_price,
            size_matched=result.position_delta,
            status=result.raw_status,
            err_msg=result.err_msg,
        ))

    def _record_buy_order(self, config: CopyTradingConfig, asset_id: str, follow_price: float, follow_buy_size: float, result: PlaceOrderResult, source: str):
        """记录 BUY 订单到 order 表"""
        order_id = result.order_id
        if result.raw_status in ("ERROR", "SKIPPED"):
            raw = f"{source}_{asset_id}_{config.id}_{time.time()}"
            order_id = f"{result.raw_status}_0x" + hashlib.sha256(raw.encode()).hexdigest()

        asyncio.create_task(asyncio.to_thread(
            record_copy_trading_order,
            order_id=order_id,
            config_id=config.id,
            leader=config.leader_proxy_wallet,
            follower=config.follower_proxy_wallet,
            leader_tx_hash=source,
            asset_id=asset_id,
            side="BUY",
            leader_size=0,
            leader_price=0,
            follow_size=result.size,
            follow_price=follow_price,
            size_matched=result.position_delta,
            status=result.raw_status,
            err_msg=result.err_msg,
        ))

    # ==================== 清扫策略 ====================

    def _on_sweep_signal(self, asset_id: str, price: float, size: float):
        """market svc 回调：订单簿出现可清扫 ask"""
        sweep_size = min(size, self._sweep_max_size)
        asyncio.create_task(self._execute_sweep_buy(asset_id, price, sweep_size))


    async def _execute_sweep_buy(self, asset_id: str, price: float, size: float):
        """对所有持有该 asset 的 config 执行清扫买单"""
        config_ids = self._asset_to_configs.get(asset_id)
        if not config_ids:
            return
        for config_id in list(config_ids):
            config = self._config_id_to_config.get(config_id)
            if not config or not config.enabled:
                continue
            f_addr = config.follower_proxy_wallet

            async with self._get_addr_lock(f_addr):
                follower_name = self._account_service.get_acc_name(f_addr)
                logger.info(
                    f"[Sweep] BUY {self._asset_label(asset_id)} | "
                    f"follower={follower_name} {size:.2f}@{price}"
                )
                result = await self._place_order(
                    config, asset_id, "BUY", size,
                    price=price, tick_size="0.01", neg_risk=True
                )
                self._register_post_order_result(config, result)

                new_pending = self._pending_buy_orders.setdefault(f_addr, {}).get(asset_id, 0) + result.pending_delta
                self._pending_buy_orders.setdefault(f_addr, {})[asset_id] = new_pending
                new_pos = self._follower_positions.setdefault(f_addr, {}).get(asset_id, 0) + result.position_delta
                self._follower_positions.setdefault(f_addr, {})[asset_id] = new_pos

            if result.raw_status == "ERROR":
                logger.warning(f"[Sweep] BUY failed: {self._asset_label(asset_id)} err={result.err_msg}")
            elif result.pending_delta and not result.position_delta:
                logger.info(f"[Sweep] BUY pending: {result.pending_delta:+.2f} {self._asset_label(asset_id)} (position={new_pos} pending={new_pending})")

            if result.pending_delta:
                asyncio.create_task(self._save_pending_buy_with_question(f_addr, asset_id, new_pending))
            if result.position_delta:
                logger.info(f"[Sweep] BUY filled: {result.position_delta:+.2f} {self._asset_label(asset_id)} (position={new_pos})")
                asyncio.create_task(asyncio.to_thread(upsert_follower_position, f_addr, asset_id, new_pos))

            self._record_buy_order(config, asset_id, price, size, result, "SWEEP_RUSH")



    # ==================== 天气扫单入场 ====================

    async def _consume_weather_sweep(self):
        """从 EventBus 消费 weather.sweep 事件，筛选 outcome=no 后触发入场

        payload schema (from coordinator event.payload()):
            {"event_type": "sweep", "asset": {"asset_id": str, "outcome": "yes"|"no", ...}, ...}
        """
        while True:
            try:
                payload = await self._weather_sweep_queue.get()
                asset = payload.get("asset", {})
                asset_id = asset.get("asset_id")
                reason = payload.get("reason", "")
                outcome = asset.get("outcome")
                logger.info(f"[WeatherSweep] Received: asset={asset_id[:8] if asset_id else '?'} outcome={outcome} reason={reason}")
                if outcome == "no" and asset_id:
                    await self._execute_weather_sweep(asset_id)
                else:
                    logger.debug(f"[WeatherSweep] Skipped: outcome={outcome} reason={reason}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[WeatherSweep] Consumer error: {e}")

    async def _execute_weather_sweep(self, asset_id: str):
        """天气模块检测到 NO token 被扫，触发入场 + 确认窗口"""
        now = time.time()
        for config in list(self._config_id_to_config.values()):
            if not config.enabled or config.sweep_confirm_window_ms <= 0:
                continue

            state_key = (config.id, asset_id)
            entry = self._weather_states.get(state_key)
            if entry and entry.state != WeatherAssetState.IDLE:
                continue

            f_addr = config.follower_proxy_wallet
            if self._follower_positions.get(f_addr, {}).get(asset_id, 0) > 0:
                continue

            follow_price = 0.99
            follow_buy_size = config.buy_size

            async with self._get_addr_lock(f_addr):
                follower_name = self._account_service.get_acc_name(f_addr)
                logger.info(
                    f"[WeatherSweep] BUY {self._asset_label(asset_id)} | "
                    f"follower={follower_name} {follow_buy_size:.2f}@{follow_price} "
                    f"window={config.sweep_confirm_window_ms}ms"
                )

                result = await self._place_order(
                    config, asset_id, "BUY", follow_buy_size,
                    price=follow_price, tick_size="0.01", neg_risk=True
                )
                self._register_post_order_result(config, result)

                new_pending = self._pending_buy_orders.setdefault(f_addr, {}).get(asset_id, 0) + result.pending_delta
                self._pending_buy_orders.setdefault(f_addr, {})[asset_id] = new_pending
                new_pos = self._follower_positions.setdefault(f_addr, {}).get(asset_id, 0) + result.position_delta
                self._follower_positions.setdefault(f_addr, {})[asset_id] = new_pos

            if result.raw_status == "ERROR":
                logger.warning(f"[WeatherSweep] BUY failed: {self._asset_label(asset_id)} err={result.err_msg}")
                self._record_buy_order(config, asset_id, follow_price, follow_buy_size, result, "WEATHER_SWEEP")
                continue

            if result.pending_delta:
                asyncio.create_task(self._save_pending_buy_with_question(f_addr, asset_id, new_pending))
            if result.position_delta:
                asyncio.create_task(asyncio.to_thread(upsert_follower_position, f_addr, asset_id, new_pos))

            self._record_buy_order(config, asset_id, follow_price, follow_buy_size, result, "WEATHER_SWEEP")

            if result.raw_status in ("LIVE", "MATCHED", "DELAYED"):
                get_market_service().watch_for_exit(asset_id)
                self._asset_to_configs.setdefault(asset_id, set()).add(config.id)
                entry = SweepEntry(
                    state=WeatherAssetState.SWEEP_PENDING,
                    order_id=result.order_id,
                    entry_time=now,
                    filled_size=result.position_delta,
                )
                entry.timer_task = asyncio.create_task(
                    self._sweep_timeout(state_key, config, entry)
                )
                self._weather_states[state_key] = entry
                logger.info(f"[WeatherState] IDLE→SWEEP_PENDING: {self._asset_label(asset_id)} config={config.id} window={config.sweep_confirm_window_ms}ms")

        if asset_id not in self._asset_labels:
            asyncio.create_task(self._get_asset_label(asset_id))

    async def _sweep_timeout(self, state_key: tuple, config: CopyTradingConfig, entry: SweepEntry):
        """单个入场的确认窗口定时器，到期触发 cancel + sell"""
        config_id, asset_id = state_key
        window_sec = config.sweep_confirm_window_ms / 1000.0
        try:
            await asyncio.sleep(window_sec)
        except asyncio.CancelledError:
            return
        # 到期时再检查状态是否仍为 SWEEP_PENDING（可能已被 leader 确认）
        current = self._weather_states.get(state_key)
        if current is not entry or entry.state != WeatherAssetState.SWEEP_PENDING:
            return
        logger.info(f"[WeatherState] SWEEP_PENDING→IDLE (timeout): {self._asset_label(asset_id)} config={config_id}")
        self._weather_states.pop(state_key, None)
        if entry.order_id:
            self._sweep_exited_orders[entry.order_id] = (config_id, asset_id)
        try:
            await self._execute_sweep_exit(asset_id, config, entry)
        except Exception as e:
            logger.error(f"[SweepExit] Error during exit for {self._asset_label(asset_id)} config={config_id}: {e}")

    async def _execute_sweep_exit(self, asset_id: str, config: CopyTradingConfig, entry: SweepEntry):
        """sweep 确认窗口到期：cancel 挂单 + sell 已成交部分 → 回 IDLE"""
        f_addr = config.follower_proxy_wallet

        # 1. cancel 未成交的 buy order（pending 释放由 WS CANCELLATION 事件处理）
        if entry.order_id:
            try:
                cancel_result = await asyncio.to_thread(
                    self._account_service.cancel_order, f_addr, entry.order_id
                )
                logger.info(f"[SweepExit] Canceled order {entry.order_id[:8]} for {self._asset_label(asset_id)} result={cancel_result}")
            except Exception as e:
                logger.warning(f"[SweepExit] Cancel failed for {entry.order_id[:8]}: {e}")

        # 2. sell 已成交的持仓
        async with self._get_addr_lock(f_addr):
            follower_pos = self._follower_positions.get(f_addr, {}).get(asset_id, 0)
            pending_sell = self._pending_sell_orders.get(f_addr, {}).get(asset_id, 0)
            available_pos = max(0, follower_pos - pending_sell)
            if available_pos <= 0.01:
                logger.debug(f"[SweepExit] No position to sell for {self._asset_label(asset_id)} config={config.id}")
                return

            follow_price = 0.99
            follow_sell_size = available_pos
            follower_name = self._account_service.get_acc_name(f_addr)

            logger.info(
                f"[SweepExit] SELL {self._asset_label(asset_id)} | "
                f"follower={follower_name} {follow_sell_size:.2f}@{follow_price}"
            )

            result = await self._place_order(
                config, asset_id, "SELL", follow_sell_size,
                price=follow_price, tick_size="0.01", neg_risk=True
            )
            self._register_post_order_result(config, result)

            new_pending = self._pending_sell_orders.setdefault(f_addr, {}).get(asset_id, 0) + result.pending_delta
            self._pending_sell_orders.setdefault(f_addr, {})[asset_id] = new_pending
            new_pos = max(0, self._follower_positions.setdefault(f_addr, {}).get(asset_id, 0) - result.position_delta)
            self._follower_positions.setdefault(f_addr, {})[asset_id] = new_pos

        if result.pending_delta:
            asyncio.create_task(self._save_pending_sell_with_question(f_addr, asset_id, new_pending))
        if result.position_delta:
            asyncio.create_task(asyncio.to_thread(upsert_follower_position, f_addr, asset_id, new_pos))

        self._record_sell_order(config, asset_id, follow_price, follow_sell_size, result, "SWEEP_TIMEOUT_EXIT")

        market_svc = get_market_service()
        market_svc.unwatch_exit(asset_id)
        market_svc.unsubscribe(asset_id)
        logger.info(f"[SweepExit] unsubscribed {self._asset_label(asset_id)}")

    async def _get_asset_label(self, asset_id: str) -> str:
        """根据 asset_id 返回可读标签 'id[:8] - question[outcome]'
        内存 → DB → API fetch → _cache_asset_label 填充内存缓存"""
        label = self._asset_labels.get(asset_id)
        if label:
            return label

        row = get_asset_question_and_outcome(asset_id)
        if row:
            self._cache_asset_label(asset_id, row[0], row[1] or "")
            return self._asset_labels[asset_id]

        # 防止同一 asset 并发多次查询
        if asset_id in self._asset_fetch_events:
            event = self._asset_fetch_events[asset_id]
            await event.wait()
            return self._asset_label(asset_id)

        # 创建事件并开始查询
        event = asyncio.Event()
        self._asset_fetch_events[asset_id] = event
        try:
            await self._fetch_and_cache_asset_question(asset_id)
            return self._asset_label(asset_id)
        finally:
            event.set()
            self._asset_fetch_events.pop(asset_id, None)

    async def _fetch_and_cache_asset_question(self, asset_id: str):
        """从 MarketService 获取 market 元数据，写入 DB 并填充标签缓存。"""
        assets = await get_market_service().fetch_asset_question_assets(asset_id)
        batch_upsert_asset_questions(assets)
        for asset in assets:
            self._cache_asset_label(asset["asset_id"], asset["question"], asset.get("outcome", ""))
        if not any(a["asset_id"] == asset_id for a in assets):
            logger.warning(f"[AssetQuestion] No title for {asset_id[:8]}")

    def _register_post_order_result(self, config: CopyTradingConfig, result: PlaceOrderResult):
        """登记程序下单返回结果，供 WS 去重和后续 config/price 反查使用。"""
        if result.raw_status not in ("LIVE", "MATCHED", "DELAYED"):
            return

        order_id = result.order_id
        if not order_id:
            logger.warning(f"[CopyTrade] successful order result missing order_id: status={result.raw_status}")
            return

        self._order_id_to_config_id[order_id] = config.id
        self._order_id_to_follow_price[order_id] = result.price

        if result.raw_status == "LIVE":
            self._order_live_on_post_ids.add(order_id)
        elif result.raw_status == "DELAYED":
            self._order_delayed_on_post_ids.add(order_id)

        if result.position_delta > 0:
            self._order_post_filled[order_id] = result.position_delta

    def _record_and_notify_order_result(
        self,
        config: CopyTradingConfig,
        signal: ActivitySignal,
        follow_price: float,
        result: PlaceOrderResult,
        follower_name: str,
        signal_latency_ms: Optional[int] = None,
    ):
        order_id = result.order_id

        if result.raw_status in ("ERROR", "SKIPPED"):
            raw = f"{signal.transaction_hash}_{signal.asset}_{signal.side}_{time.time()}"
            order_id = f"{result.raw_status}_0x" + hashlib.sha256(raw.encode()).hexdigest()

        asyncio.create_task(asyncio.to_thread(
            record_copy_trading_order,
            order_id=order_id,
            config_id=config.id,
            leader=config.leader_proxy_wallet,
            follower=config.follower_proxy_wallet,
            leader_tx_hash=signal.transaction_hash,
            asset_id=signal.asset,
            side=signal.side,
            leader_size=signal.size,
            leader_price=signal.price,
            follow_size=result.size,
            follow_price=follow_price,
            size_matched=result.position_delta,
            status=result.raw_status,
            err_msg=result.err_msg,
            signal_latency_ms=signal_latency_ms,
        ))

        if result.raw_status in ("ERROR", "SKIPPED", "DELAYED"):
            logger.warning(f"[CopyTrade] order {result.raw_status}: {signal.side} {signal.asset[:8]} size={result.size} err={result.err_msg}")

    async def _patch_order_match_from_trade(self, order_id: str, matched_amount: float, order=None):
        """trade CONFIRMED 增量推进订单表的 size_matched；仅在未终态时补 status。"""
        if order is None:
            order = await asyncio.to_thread(get_order_by_id, order_id)
        if not order:
            return None

        if order.status in ("MATCHED", "CANCELED"):
            return order

        new_size_matched = min(order.follow_size, order.size_matched + matched_amount)
        if new_size_matched >= order.follow_size - ORDER_MATCH_EPSILON:
            new_status = "MATCHED"
        elif order.status == "DELAYED":
            new_status = "LIVE"
        else:
            new_status = order.status

        if abs(new_size_matched - order.size_matched) <= ORDER_MATCH_EPSILON and new_status == order.status:
            return order

        updated = await asyncio.to_thread(
            update_copy_trading_order,
            order_id=order_id,
            size_matched=new_size_matched,
            status=new_status,
        )
        if updated:
            logger.debug(
                f"[CopyTrade] trade patched order row: order_id={order_id[:8]} "
                f"size_matched={order.size_matched:.4f}->{new_size_matched:.4f} status={order.status}->{new_status}"
            )
            order.size_matched = new_size_matched
            order.status = new_status
        return order

    async def _place_order(self, config: CopyTradingConfig, asset_id: str, side: str, size: float, price: float, tick_size: str=None, neg_risk: bool=None) -> PlaceOrderResult:
        """
        纯下单函数，返回 PlaceOrderResult（状态登记和 pending_delta 由调用方处理）。
        BUY 成功时自动扣减 config 余额；SELL 余额不足时内部重试一次（使用实际余额）。
        """
        order_result = await self._do_place_order(config, asset_id, side, size, price, tick_size, neg_risk)

        # BUY 成功：扣减余额
        if side == "BUY":
            used = order_result.pending_delta + order_result.position_delta
            if used > 0:
                self._config_balances[config.id] = max(0, self._config_balances.get(config.id, 0) - used)
                logger.info(f"[CopyTrade] balance: -{used:.2f} config={config.id} remaining={self._config_balances[config.id]:.2f}")

        # SELL 即时成交：回补余额
        if side == "SELL" and order_result.position_delta > 0:
            self._config_balances[config.id] = self._config_balances.get(config.id, 0) + order_result.position_delta
            logger.info(f"[CopyTrade] balance: +{order_result.position_delta:.2f} (sell immediate) config={config.id} remaining={self._config_balances[config.id]:.2f}")

        return order_result

    async def _do_place_order(self, config: CopyTradingConfig, asset_id: str, side: str, size: float, price: float, tick_size: str=None, neg_risk: bool=None) -> PlaceOrderResult:
        """实际下单逻辑（含重试）"""
        order_price = price
        try:
            result = await asyncio.to_thread(
                self._account_service.place_limit_order,
                config.follower_proxy_wallet,
                asset_id,
                side,
                size,
                order_price,
                tick_size,
                neg_risk,
                config.gtd_expiration_sec,
            )
            logger.debug(f"[CopyTrade] Order result: {result}")

            status = result.get("status") if result else None
            order_id = result.get("orderID") if result else None
            if status == "live":
                logger.info(f"[CopyTrade] order LIVE: {side} {size} @ {order_price} asset={self._asset_label(asset_id)} order_id={order_id[:8]}")
                return PlaceOrderResult(pending_delta=size, position_delta=0, size=size, price=order_price, raw_status="LIVE", order_id=order_id, err_msg=None)
            elif status == "matched":
                taking = float(result.get("takingAmount") or 0)
                making = float(result.get("makingAmount") or 0)
                filled_size = taking if side == "BUY" else making
                remaining = size - filled_size
                logger.info(f"[CopyTrade] order MATCHED: {side} {size} @ {order_price} filled={filled_size} remaining={remaining} asset={self._asset_label(asset_id)} order_id={order_id[:8]}")
                return PlaceOrderResult(pending_delta=remaining, position_delta=filled_size, size=size, price=order_price, raw_status="MATCHED" if remaining == 0 else "LIVE", order_id=order_id, err_msg=None)
            elif status == "delayed":
                logger.info(f"[CopyTrade] order DELAYED: {side} {size} @ {order_price} asset={self._asset_label(asset_id)} order_id={order_id[:8]}")
                return PlaceOrderResult(pending_delta=size, position_delta=0, size=size, price=order_price, raw_status="DELAYED", order_id=order_id, err_msg=None)

            err_msg = f"unexpected order status: {status}, result={result}"
            logger.error(f"[CopyTrade] {err_msg}")
            return PlaceOrderResult(pending_delta=0, position_delta=0, size=0, price=order_price, raw_status="ERROR", order_id=order_id, err_msg=err_msg)

        except Exception as e:
            logger.error(f"[CopyTrade] Failed to place order: {e}")
            err_str = str(e)

            # Size 低于最低限额时，用最低限额重试
            # error_message={'error': 'order ... is invalid. Size (2.04) lower than the minimum: 5'}
            if "lower than the minimum" in err_str:
                try:
                    retry_size = 5
                    logger.warning(f"[CopyTrade] Size below minimum, retry with min_size={retry_size}")
                    retry_result = await asyncio.to_thread(
                        self._account_service.place_limit_order,
                        config.follower_proxy_wallet, asset_id, side, retry_size,
                        order_price, tick_size, neg_risk, config.gtd_expiration_sec,
                    )
                    retry_status = retry_result.get("status")
                    retry_order_id = retry_result.get("orderID")
                    if retry_status == "live":
                        logger.info(f"[CopyTrade] order RETRY LIVE: {side} {retry_size} @ {order_price} asset={self._asset_label(asset_id)} order_id={retry_order_id[:8]}")
                        return PlaceOrderResult(pending_delta=retry_size, position_delta=0, size=retry_size, price=order_price, raw_status="LIVE", order_id=retry_order_id, err_msg=None)
                    elif retry_status == "matched":
                        taking = float(retry_result.get("takingAmount") or 0)
                        making = float(retry_result.get("makingAmount") or 0)
                        retry_filled = taking if side == "BUY" else making
                        retry_remaining = retry_size - retry_filled
                        logger.info(f"[CopyTrade] order RETRY MATCHED: {side} {retry_size} @ {order_price} filled={retry_filled} remaining={retry_remaining} asset={self._asset_label(asset_id)} order_id={retry_order_id[:8]}")
                        return PlaceOrderResult(pending_delta=retry_remaining, position_delta=retry_filled, size=retry_size, price=order_price, raw_status="MATCHED" if retry_remaining == 0 else "LIVE", order_id=retry_order_id, err_msg=None)
                    elif retry_status == "delayed":
                        logger.info(f"[CopyTrade] order RETRY DELAYED: {side} {retry_size} @ {order_price} asset={self._asset_label(asset_id)} order_id={retry_order_id[:8]}")
                        return PlaceOrderResult(pending_delta=retry_size, position_delta=0, size=retry_size, price=order_price, raw_status="DELAYED", order_id=retry_order_id, err_msg=None)
                    err_msg = f"unexpected retry order status: {retry_status}, result={retry_result}"
                    logger.error(f"[CopyTrade] {err_msg}")
                    return PlaceOrderResult(pending_delta=0, position_delta=0, size=0, price=order_price, raw_status="ERROR", order_id=retry_order_id, err_msg=err_msg)
                except Exception as retry_e:
                    logger.error(f"[CopyTrade] min size retry failed: {retry_e}")
                    return PlaceOrderResult(pending_delta=0, position_delta=0, size=0, price=order_price, raw_status="ERROR", order_id=None, err_msg=str(retry_e))

            # SELL 余额不足时重试一次（取实际可用余额）
            # 1. error_message={'error': 'not enough balance / allowance: the balance is not enough -> balance: 79380000, sum of matched orders: 21760000, order amount (inc. fees): 72840000'}
            # 2. error_message={'error': 'not enough balance / allowance: the balance is not enough -> balance: 74760000, order amount: 99999000000'}
            if (side == "SELL" and "not enough balance" in err_str):
                try:
                    balance_raw = int(err_str.split("balance: ")[1].split(",")[0])
                    matched_raw = 0
                    if "sum of matched orders" in err_str:
                        matched_raw = int(err_str.split("sum of matched orders: ")[1].split(",")[0])
                    actual_shares = (balance_raw - matched_raw) / 1e6
                    retry_size = math.floor(actual_shares * 100) / 100
                    logger.warning(f"[CopyTrade] SELL balance insufficient, retry with actual balance: {retry_size} (balance={balance_raw}, matched={matched_raw})")

                    retry_result = await asyncio.to_thread(
                        self._account_service.place_limit_order,
                        config.follower_proxy_wallet, asset_id, side, retry_size,
                        order_price, tick_size, neg_risk,
                    )
                    retry_status = retry_result.get("status")
                    retry_order_id = retry_result.get("orderID")
                    if retry_status == "live":
                        logger.info(f"[CopyTrade] order RETRY LIVE: SELL {retry_size} @ {order_price} asset={self._asset_label(asset_id)} order_id={retry_order_id[:8]}")
                        return PlaceOrderResult(pending_delta=retry_size, position_delta=0, size=retry_size, price=order_price, raw_status="LIVE", order_id=retry_order_id, err_msg=None)
                    elif retry_status == "matched":
                        retry_filled = float(retry_result.get("makingAmount") or 0)
                        retry_remaining = retry_size - retry_filled
                        logger.info(f"[CopyTrade] order RETRY MATCHED: SELL {retry_size} @ {order_price} filled={retry_filled} remaining={retry_remaining} asset={self._asset_label(asset_id)} order_id={retry_order_id[:8]}")
                        return PlaceOrderResult(pending_delta=retry_remaining, position_delta=retry_filled, size=retry_size, price=order_price, raw_status="MATCHED" if retry_remaining == 0 else "LIVE", order_id=retry_order_id, err_msg=None)
                    elif retry_status == "delayed":
                        logger.info(f"[CopyTrade] order RETRY DELAYED: SELL {retry_size} @ {order_price} asset={self._asset_label(asset_id)} order_id={retry_order_id[:8]}")
                        return PlaceOrderResult(pending_delta=retry_size, position_delta=0, size=retry_size, price=order_price, raw_status="DELAYED", order_id=retry_order_id, err_msg=None)

                    err_msg = f"unexpected SELL retry order status: {retry_status}, result={retry_result}"
                    logger.error(f"[CopyTrade] {err_msg}")
                    return PlaceOrderResult(pending_delta=0, position_delta=0, size=0, price=order_price, raw_status="ERROR", order_id=retry_order_id, err_msg=err_msg)
                except Exception as e:
                    logger.error(f"[CopyTrade] SELL retry failed: {e}")
                    return PlaceOrderResult(pending_delta=0, position_delta=0, size=0, price=order_price, raw_status="ERROR", order_id=None, err_msg=str(e))

            return PlaceOrderResult(pending_delta=0, position_delta=0, size=0, price=order_price, raw_status="ERROR", order_id=None, err_msg=err_str)

    # ==================== 配置管理（供 API 调用） ====================

    async def create_config(
        self,
        leader_proxy_wallet: str,
        follower_proxy_wallet: str,
        owner_user_id: int = 0,
    ) -> int:
        """创建跟单配置"""
        leader_proxy_wallet = leader_proxy_wallet.lower()
        follower_proxy_wallet = follower_proxy_wallet.lower()
        if leader_proxy_wallet in self._leader_addr_to_configs:
            raise ValueError(f"Leader {leader_proxy_wallet} already has a config")

        # 注册 leader 到 Predexon
        get_copy_trading_predexon().add_leader(leader_proxy_wallet)

        # 启动 follower 的 User Channel WS

        from .ws import CopyTradingWS, add_copy_trading_ws, get_all_copy_trading_ws
        wss = get_all_copy_trading_ws()
        if follower_proxy_wallet not in wss:
            creds = self._account_service.get_account_credentials_by_proxy_wallet(follower_proxy_wallet)
            if not creds:
                raise RuntimeError(f"无法获取 follower {follower_proxy_wallet} 的凭据，请检查账户是否有效后重试")
            ws = CopyTradingWS(follower_proxy_wallet, creds)
            add_copy_trading_ws(ws)
            await ws.start()

        # 落库
        try:
            config_id = create_copy_trading_config(leader_proxy_wallet, follower_proxy_wallet, owner_user_id)
        except pymysql.err.IntegrityError as e:
            raise RuntimeError(f"Follower {follower_proxy_wallet} already follows leader {leader_proxy_wallet}") from e

        new_config = CopyTradingConfig(
            id=config_id,
            leader_proxy_wallet=leader_proxy_wallet,
            follower_proxy_wallet=follower_proxy_wallet,
            enabled=False,
            owner_user_id=owner_user_id,
        )

        # 增量更新缓存
        self._leader_addr_to_configs[leader_proxy_wallet] = new_config
        self._follower_addr_to_configs.setdefault(follower_proxy_wallet, set()).add(new_config)
        self._followers.add(follower_proxy_wallet)
        self._config_id_to_config[config_id] = new_config
        self._config_balances.setdefault(config_id, new_config.buy_size)
        self._add_fl_key(new_config)
        self._follower_positions.setdefault(follower_proxy_wallet, {})

        # 同步 follower 仓位和 pending
        await self._sync_pending_orders_from_poly(follower_proxy_wallet)
        await self._sync_follower_positions_from_poly(follower_proxy_wallet)

        logger.info(f"[CopyTrade] Created config: follower={follower_proxy_wallet[:8]} follows leader={leader_proxy_wallet[:8]}")
        return config_id

    async def _save_pending_sell_with_question(self, f_addr: str, asset_id: str, pending: float):
        """异步获取 market label 并写入 pending 到 DB"""
        try:
            label = await self._get_asset_label(asset_id)
            await asyncio.to_thread(upsert_follower_pending_sell, f_addr, asset_id, pending, label)
            logger.debug(f"[CopyTrade] Saved pending_sell to DB: {f_addr[:8]} {label} pending={pending}")
        except Exception as e:
            logger.error(f"[CopyTrade] Failed to save pending_sell to DB: {e}")

    async def _save_pending_buy_with_question(self, f_addr: str, asset_id: str, pending: float):
        """异步获取 market label 并写入 BUY pending 到 DB"""
        try:
            label = await self._get_asset_label(asset_id)
            await asyncio.to_thread(upsert_follower_pending_buy, f_addr, asset_id, pending, label)
            logger.debug(f"[CopyTrade] Saved pending_buy to DB: {f_addr[:8]} {label} pending={pending}")
        except Exception as e:
            logger.error(f"[CopyTrade] Failed to save pending_buy to DB: {e}")

    async def handle_order_event(self, f_addr: str, asset_id: str, original_size: float, size_matched: float, type: str, status: str, side: str, order_id: str, price: float):
        """处理 WS order 事件（PLACEMENT / CANCELLATION / UPDATE）

        PLACEMENT：更新 pending + 落库（为了记录网站手动下单）
        CANCELLATION：更新 pending
        position 变化统一由 trade CONFIRMED 处理
        """
        if type == "PLACEMENT":
            async with self._get_addr_lock(f_addr):
                if status != "LIVE":
                    logger.warning(f"观测到新status: {status}")
                # 去重：程序发起的单已在下单入口返回落库并记录id
                # 只有手动下单才会通过
                if order_id in self._order_live_on_post_ids or order_id in self._order_delayed_on_post_ids or order_id in self._order_post_filled:
                    logger.debug(f"[CopyTrade] Order {order_id[:8]} already processed by _place_order, skip WS PLACEMENT")
                    return
                self._order_live_on_post_ids.add(order_id)

                if side == "BUY":
                    cur_pos = self._follower_positions.setdefault(f_addr, {}).get(asset_id, 0)
                    cur_pending = self._pending_buy_orders.setdefault(f_addr, {}).get(asset_id, 0)
                    new_pending = cur_pending + original_size
                    self._pending_buy_orders.setdefault(f_addr, {})[asset_id] = new_pending
                    asyncio.create_task(self._save_pending_buy_with_question(f_addr, asset_id, new_pending))
                else:
                    cur_pos = self._follower_positions.setdefault(f_addr, {}).get(asset_id, 0)
                    cur_pending = self._pending_sell_orders.setdefault(f_addr, {}).get(asset_id, 0)
                    new_pending = cur_pending + original_size
                    self._pending_sell_orders.setdefault(f_addr, {})[asset_id] = new_pending
                    asyncio.create_task(self._save_pending_sell_with_question(f_addr, asset_id, new_pending))
            logger.info(f"[CopyTrade] order PLACEMENT: {side:>4} {original_size:>7.2f} @ {price:<5} asset={self._asset_label(asset_id)} order_id={order_id[:8]} (new_pos={cur_pos:>7.2f}, new_pending={new_pending:>7.2f})")

            asyncio.create_task(asyncio.to_thread(record_copy_trading_order,
                order_id=order_id,
                config_id=0,
                leader=f"null",
                follower=f_addr,
                leader_tx_hash="null",
                asset_id=asset_id,
                side=side,
                leader_size=0,
                leader_price=0,
                follow_size=original_size,
                follow_price=price,
                size_matched=size_matched,
                status=status,
            ))
            return

        if type == "CANCELLATION":
            released = max(0, original_size - size_matched)
            if released <= 0.01:
                return
            async with self._get_addr_lock(f_addr):
                order = await asyncio.to_thread(get_order_by_id, order_id)
                if order and (order.status or "") == "MATCHED":
                    logger.debug(f"[CopyTrade] Order {order_id[:8]} already matched, skip cancellation")
                    return
                if order_id in self._processed_canceled_order_ids:
                    logger.debug(f"[CopyTrade] Order {order_id[:8]} cancellation already processed, skip")
                    return
                self._processed_canceled_order_ids.add(order_id)

                if side == "BUY":
                    cur_pos = self._follower_positions.setdefault(f_addr, {}).get(asset_id, 0)
                    cur_pending = self._pending_buy_orders.setdefault(f_addr, {}).get(asset_id, 0)
                    new_pending = max(0, cur_pending - released)
                    if new_pending <= 0.01:
                        self._pending_buy_orders[f_addr].pop(asset_id, None)
                    else:
                        self._pending_buy_orders[f_addr][asset_id] = new_pending
                    asyncio.create_task(self._save_pending_buy_with_question(f_addr, asset_id, new_pending))
                    # 回补余额
                    config_id = self._order_id_to_config_id.get(order_id)
                    if config_id and config_id in self._config_balances:
                        self._config_balances[config_id] += released
                        logger.info(f"[CopyTrade] balance: +{released:.2f} (cancel) config={config_id} remaining={self._config_balances[config_id]:.2f}")
                    # 完全取消的 sweep 订单不再需要延迟成交补偿
                    if size_matched <= 0.01:
                        self._sweep_exited_orders.pop(order_id, None)

                else:
                    cur_pos = self._follower_positions.setdefault(f_addr, {}).get(asset_id, 0)
                    cur_pending = self._pending_sell_orders.setdefault(f_addr, {}).get(asset_id, 0)
                    new_pending = max(0, cur_pending - released)
                    if new_pending <= 0.01:
                        self._pending_sell_orders[f_addr].pop(asset_id, None)
                    else:
                        self._pending_sell_orders[f_addr][asset_id] = new_pending
                    asyncio.create_task(self._save_pending_sell_with_question(f_addr, asset_id, new_pending))
            logger.info(f"[CopyTrade] order CANCELED : {side:>4} {released:>7.2f} @ {price:<5} asset={self._asset_label(asset_id)} order_id={order_id[:8]} (position={cur_pos:>7.2f}, pending={new_pending:>7.2f})")

            asyncio.create_task(asyncio.to_thread(update_copy_trading_order,
                order_id=order_id,
                size_matched=size_matched,
                status=status,
                err_msg="canceled",
            ))
            return

        if type == "UPDATE" and status == "MATCHED":
            asyncio.create_task(asyncio.to_thread(update_copy_trading_order,
                order_id=order_id,
                size_matched=size_matched,
                status=status,
            ))
            return

    async def handle_trade_confirmed(self, f_addr: str, asset_id: str, matched_amount: float, side: str, price: float, order_id: str):
        """处理 trade CONFIRMED 消息，更新成交账并推进订单表 matched 生命周期。"""
        async with self._get_addr_lock(f_addr):
            order = None
            config_id = self._order_id_to_config_id.get(order_id, 0)
            if not config_id:
                order = await asyncio.to_thread(get_order_by_id, order_id)
                if order:
                    config_id = order.config_id

            order = await self._patch_order_match_from_trade(order_id, matched_amount, order=order)
            if order and not config_id:
                config_id = order.config_id
            already_filled = self._order_post_filled.get(order_id, 0)
            if already_filled > 0:
                skip_amount = min(already_filled, matched_amount)
                matched_amount -= skip_amount
                remaining_skip = already_filled - skip_amount
                if remaining_skip <= 0.001:
                    self._order_post_filled.pop(order_id, None)
                else:
                    self._order_post_filled[order_id] = remaining_skip
                if matched_amount <= 0.001:
                    logger.debug(f"[CopyTrade] Trade {order_id[:8]} already processed by _place_order, skip position update (skipped={skip_amount:.4f})")
                    return
                logger.debug(f"[CopyTrade] Trade {order_id[:8]} partially skipped (skipped={skip_amount:.4f}, remaining={matched_amount:.4f})")

            if side == "BUY":
                new_size = self._follower_positions.setdefault(f_addr, {}).get(asset_id, 0) + matched_amount
                self._follower_positions.setdefault(f_addr, {})[asset_id] = new_size
                upsert_follower_position(f_addr, asset_id, new_size)
                # 释放 pending_buy（加锁防与轮询并发）
                pending = self._pending_buy_orders.setdefault(f_addr, {}).get(asset_id, 0)
                new_pending = max(0, pending - matched_amount)
                if new_pending <= 0.01:
                    self._pending_buy_orders[f_addr].pop(asset_id, None)
                else:
                    self._pending_buy_orders[f_addr][asset_id] = new_pending
                asyncio.create_task(self._save_pending_buy_with_question(f_addr, asset_id, new_pending))
            elif side == "SELL":
                old_size = self._follower_positions.setdefault(f_addr, {}).get(asset_id, 0)
                new_size = max(0, old_size - matched_amount)
                self._follower_positions.setdefault(f_addr, {})[asset_id] = new_size
                upsert_follower_position(f_addr, asset_id, new_size)
                # 释放 pending（加锁防与轮询并发）

                pending = self._pending_sell_orders.setdefault(f_addr, {}).get(asset_id, 0)
                new_pending = max(0, pending - matched_amount)
                if new_pending <= 0.01:
                    self._pending_sell_orders[f_addr].pop(asset_id, None)
                else:
                    self._pending_sell_orders[f_addr][asset_id] = new_pending
                asyncio.create_task(self._save_pending_sell_with_question(f_addr, asset_id, new_pending))
                # 回补余额
                if config_id and config_id in self._config_balances:
                    self._config_balances[config_id] += matched_amount
                    logger.info(f"[CopyTrade] balance: +{matched_amount:.2f} (sell filled) config={config_id} remaining={self._config_balances[config_id]:.2f}")

        logger.info(f"[CopyTrade] Trade CONFIRMED: {side:>4} {matched_amount:>7.2f} @ {price:<5} asset={self._asset_label(asset_id)} order_id={order_id[:8]} (new_pos={new_size:>7.2f}, new_pending={new_pending:>7.2f})")

        # 延迟成交补偿：已超时退出的 sweep 订单成交后触发 SELL
        if side == "BUY" and order_id in self._sweep_exited_orders:
            exited_config_id, exited_asset_id = self._sweep_exited_orders.pop(order_id)
            config = self._config_id_to_config.get(exited_config_id)
            if config and exited_asset_id == asset_id:
                logger.info(f"[SweepExit] Delayed fill detected for timed-out sweep: {self._asset_label(asset_id)} order={order_id[:8]} filled={matched_amount:.2f}")
                asyncio.create_task(self._sell_sweep_delayed_fill(config, asset_id, matched_amount))

    async def _sell_sweep_delayed_fill(self, config: CopyTradingConfig, asset_id: str, size: float):
        """已超时的 sweep 订单延迟成交后，立即挂 SELL 退出"""
        f_addr = config.follower_proxy_wallet
        follow_price = 0.99
        async with self._get_addr_lock(f_addr):
            follower_name = self._account_service.get_acc_name(f_addr)
            logger.info(
                f"[SweepExit] SELL (delayed fill) {self._asset_label(asset_id)} | "
                f"follower={follower_name} {size:.2f}@{follow_price}"
            )
            result = await self._place_order(
                config, asset_id, "SELL", size,
                price=follow_price, tick_size="0.01", neg_risk=True
            )
            self._register_post_order_result(config, result)
            new_pending = self._pending_sell_orders.setdefault(f_addr, {}).get(asset_id, 0) + result.pending_delta
            self._pending_sell_orders.setdefault(f_addr, {})[asset_id] = new_pending
            new_pos = max(0, self._follower_positions.setdefault(f_addr, {}).get(asset_id, 0) - result.position_delta)
            self._follower_positions.setdefault(f_addr, {})[asset_id] = new_pos

        if result.pending_delta:
            asyncio.create_task(self._save_pending_sell_with_question(f_addr, asset_id, new_pending))
        if result.position_delta:
            asyncio.create_task(asyncio.to_thread(upsert_follower_position, f_addr, asset_id, new_pos))

        self._record_sell_order(config, asset_id, follow_price, size, result, "SWEEP_DELAYED_FILL")

    async def _sync_follower_positions_from_poly(self, follower_addr: str, delay: int = 0) -> int:
        """从 Polymarket API 拉取 follower 实际持仓，用真实数据覆盖程序记录"""
        if delay > 0:
            await asyncio.sleep(delay)  # 等待链上数据生效
        f_addr = follower_addr.lower()
        async with self._get_addr_lock(f_addr):
            try:
                fetched_positions = []
                limit = 500
                offset = 0
                full_sync = True
                async with aiohttp.ClientSession() as session:
                    while True:
                        async with session.get(
                            f"https://data-api.polymarket.com/positions?user={f_addr}&limit={limit}&offset={offset}",
                            timeout=aiohttp.ClientTimeout(total=10)
                        ) as resp:
                            if resp.status != 200:
                                logger.warning(
                                    f"[PositionSync] Failed to fetch follower positions page offset={offset} for "
                                    f"{self._account_service.get_acc_name(f_addr)}: status={resp.status}"
                                )
                                full_sync = False
                                break
                            page = await resp.json()
                        if not page:
                            break
                        fetched_positions.extend(page)
                        if len(page) < limit:
                            break
                        offset += limit

                new_pos_cache = {}
                to_insert = []
                for pos in fetched_positions:
                    asset_id = pos.get("asset")
                    size = float(pos.get("size", 0))
                    if asset_id and size > 0:
                        new_pos_cache[asset_id] = size
                        to_insert.append({"asset_id": asset_id, "size": size})

                if full_sync:
                    self._follower_positions[f_addr] = new_pos_cache
                else:
                    self._follower_positions.setdefault(f_addr, {}).update(new_pos_cache)
                synced = len(new_pos_cache)
                asyncio.create_task(asyncio.to_thread(batch_upsert_follower_positions, f_addr, to_insert, full_sync))
                logger.debug(f"[PositionSync] Synced {synced} follower positions ({'full' if full_sync else 'partial'}) from API for {self._account_service.get_acc_name(f_addr)}")
                return synced
            except Exception as e:
                logger.warning(f"[PositionSync] Failed to sync follower positions for {self._account_service.get_acc_name(f_addr)}: {e}")
            return 0

    def _get_addr_lock(self, addr: str) -> asyncio.Lock:
        addr = addr.lower()
        if addr not in self._addr_locks:
            self._addr_locks[addr] = asyncio.Lock()
        return self._addr_locks[addr]

    async def _sync_pending_orders_from_poly(self, f_addr: str):
        """从 Polymarket API 全量同步 follower pending 到内存（轮询兜底用）"""
        client = self._account_service.get_or_create_clob_client(f_addr)
        if not client:
            logger.warning(f"[PendingSync] No client for {self._account_service.get_acc_name(f_addr)}, skip pending load")
            return

        # get_open_orders() 返回所有 asset 的订单，必须与整个处理过程保持在同一把锁内
        # 否则拉取和处理之间的空隙会导致新 live 单丢失
        async with self._get_addr_lock(f_addr):
            pending_buy, pending_sell = {}, {}
            try:
                orders = client.get_open_orders()
            except Exception as e:
                logger.error(f"[PendingSync] Failed to get open orders for {self._account_service.get_acc_name(f_addr)}: {e}")
                return

            # 清空该地址所有 pending，以 API 返回的为准重建
            self._pending_buy_orders.pop(f_addr, None)
            self._pending_sell_orders.pop(f_addr, None)

            # 遍历所有订单，按 asset_id 累加
            for order in orders:
                asset_id = order.get("asset_id", "")
                if not asset_id:
                    continue
                side = order.get("side", "").upper()
                original_size = float(order.get("original_size", 0))
                size_matched = float(order.get("size_matched", 0))
                pending = max(0, original_size - size_matched)
                if side == "BUY":
                    self._pending_buy_orders.setdefault(f_addr, {})[asset_id] = \
                        self._pending_buy_orders.setdefault(f_addr, {}).get(asset_id, 0) + pending
                else:
                    self._pending_sell_orders.setdefault(f_addr, {})[asset_id] = \
                        self._pending_sell_orders.setdefault(f_addr, {}).get(asset_id, 0) + pending

        pending_buy = dict(self._pending_buy_orders.get(f_addr, {}))
        pending_sell = dict(self._pending_sell_orders.get(f_addr, {}))

        logger.debug(f"[PendingSync] Synced pending from API for {self._account_service.get_acc_name(f_addr)}: "
                     f"buy={len(pending_buy)}, sell={len(pending_sell)}")
        # 批量落库（fire-and-forget）
        asyncio.create_task(asyncio.to_thread(batch_upsert_follower_pending_buy, f_addr, pending_buy))
        asyncio.create_task(asyncio.to_thread(batch_upsert_follower_pending_sell, f_addr, pending_sell))


    def get_config_by_id(self, config_id: int) -> Optional[CopyTradingConfig]:
        """根据 config_id O(1) 查找配置"""
        return self._config_id_to_config.get(config_id)

    def update_config(self, config_id: int, **kwargs) -> bool:
        """更新配置"""
        success = update_copy_trading_config(config_id, **kwargs)
        if success:
            config = self._config_id_to_config.get(config_id)
            if config:
                if "enabled" in kwargs:
                    config.enabled = kwargs["enabled"]
                if "gtd_expiration_sec" in kwargs:
                    config.gtd_expiration_sec = int(kwargs["gtd_expiration_sec"])
                if "buy_size" in kwargs:
                    config.buy_size = float(kwargs["buy_size"])
                    self._config_balances[config_id] = config.buy_size
                if "sweep_confirm_window_ms" in kwargs:
                    config.sweep_confirm_window_ms = int(kwargs["sweep_confirm_window_ms"])
        return success

    def delete_config(self, config_id: int) -> bool:
        """删除配置"""
        # 删db
        success = delete_copy_trading_config(config_id)
        if success:
            cfg = self._config_id_to_config[config_id]
            f_addr, l_addr = cfg.follower_proxy_wallet, cfg.leader_proxy_wallet
            self._config_id_to_config.pop(config_id, None)
            self._fl_key_to_config.pop(f"{f_addr}_{l_addr}", None)
            self._remove_follower_config_index(cfg)
            # 从缓存中移除
            if l_addr in self._leader_addr_to_configs:
                del self._leader_addr_to_configs[l_addr]
                get_copy_trading_predexon().remove_leader(l_addr)

            # 检查 f_addr 是否还有其他 config（没有则清理 follower 资源）
            if not self._follower_addr_to_configs.get(f_addr):
                self._followers.discard(f_addr)
                delete_follower_positions(f_addr)
                self._follower_positions.pop(f_addr, None)

                from .ws import get_copy_trading_ws, remove_copy_trading_ws
                ws = get_copy_trading_ws(f_addr)
                if ws:
                    asyncio.create_task(ws.stop())
                    remove_copy_trading_ws(f_addr)

        return success


    def _get_tx_lock(self, leader_addr: str) -> asyncio.Lock:
            if leader_addr not in self._tx_locks:
                self._tx_locks[leader_addr] = asyncio.Lock()
            return self._tx_locks[leader_addr]

    def get_positions(self, config_id: int) -> dict:
        """获取跟单持仓（内存优先，follower 以 Polymarket API 为准）"""
        config = self._config_id_to_config.get(config_id)
        if not config:
            return {"follower_positions": {}}
        follower_positions = self._follower_positions.get(config.follower_proxy_wallet, {})
        return {
            "follower_positions": follower_positions,
        }

    def get_assets_belongs_to_cfg(self, config_id: int, since: Optional[str] = None) -> List[dict]:
        """获取指定配置可用于仓位历史查询的 asset 列表。"""
        return get_assets_of_cfg_from_db(config_id, since=since)

    def get_order_history(self, config_id: int, limit: int = 100) -> List[dict]:
        """获取跟单订单历史"""
        return get_orders_by_config_id(config_id, limit)



def get_copy_trading_service() -> CopyTradingService:
    """获取或创建跟单服务实例"""
    global copy_trading_service
    if copy_trading_service is None:
        copy_trading_service = CopyTradingService()
    return copy_trading_service

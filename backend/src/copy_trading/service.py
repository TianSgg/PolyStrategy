"""跟单服务核心逻辑"""
import asyncio
import hashlib
import logging
import math
import pymysql
import aiohttp
import requests
import time
from decimal import Decimal, ROUND_DOWN
from typing import Dict, Set, Optional, List

from py_clob_client_v2 import ClobClient

from .types import CopyTradingConfig, ActivitySignal, PlaceOrderResult, DEFAULT_TAKER_SPREAD_THRESHOLD, DEFAULT_EXCEED_THR, DEFAULT_BUY_PRICE_MIN, DEFAULT_BUY_PRICE_MAX, DEFAULT_SELL_PRICE_MIN, DEFAULT_SELL_PRICE_MAX, DEFAULT_BUY_PRICE_FILTER_MIN, DEFAULT_BUY_PRICE_FILTER_MAX
from market import get_market_service
from .chain import get_copy_trading_chain_monitor
from .predexon import get_copy_trading_predexon
from account.service import get_account_service
from leader.service import get_leader_service
from .models import (
    get_copy_trading_configs,
    delete_follower_positions,
    record_copy_trading_order,
    get_order_by_id,
    update_copy_trading_order,
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
    record_position_history,
    batch_upsert_config_asset,
    get_position_history_from_db,
    get_assets_of_cfg_from_db,
    get_live_buy_order_ids_by_asset,
)

logger = logging.getLogger(__name__)
ORDER_MATCH_EPSILON = 0.0001


# 全局服务实例（供 RTDS 和 API 使用）
copy_trading_service: Optional['CopyTradingService'] = None


class CopyTradingService:
    """
    跟单服务
    """

    def __init__(self):
        # 配置缓存: {leader_proxy_wallet: {CopyTradingConfig, ...}}
        self._leader_addr_to_configs: Dict[str, Set[CopyTradingConfig]] = {}

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
        # leader 退出主动撤单的 order_id 集合（用于 CANCELLATION 标注原因）
        self._leader_exit_canceled_ids: Set[str] = set()
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


        # 启动持仓轮询兜底任务（依赖 _config_id_to_config，由 initialize 填充）
        self._follower_poller_task: Optional[asyncio.Task] = None
        self._pending_poller_task: Optional[asyncio.Task] = None
        self._position_history_poller_task: Optional[asyncio.Task] = None
        self._schedule_poller_task: Optional[asyncio.Task] = None

        # Leader 名字解析服务
        self._leader_service = get_leader_service()

        # 账户名解析服务
        self._account_service = get_account_service()

        # 无 L1 认证的只读 CLOB Client，用于盘口/市场元信息查询
        self._read_only_clob_client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
        )

        # 市场数据服务
        # self.market_service = get_market_service()

    def _get_config_by_fl(self, f_addr: str, l_addr: str) -> Optional[CopyTradingConfig]:
        return self._fl_key_to_config.get(f_addr + "_" + l_addr)

    def _add_fl_key(self, config: CopyTradingConfig):
        key = config.follower_proxy_wallet + "_" + config.leader_proxy_wallet
        self._fl_key_to_config[key] = config

    def _remove_fl_key(self, f_addr: str, l_addr: str):
        self._fl_key_to_config.pop(f_addr + "_" + l_addr, None)

    def _asset_label(self, asset_id: str) -> str:
        """返回 asset 可读标签，如 '1234455612 - Will X happen?[Yes]'。未缓存时降级为 asset_id[:10]"""
        label = self._asset_labels.get(asset_id)
        if label:
            return label
        row = get_asset_question_and_outcome(asset_id)
        if row:
            question, outcome = row
            label = f"{asset_id[:10]} - {question}[{outcome}]"
            self._asset_labels[asset_id] = label
            return label
        return f"{asset_id[:10]}"

    def _cache_asset_label(self, asset_id: str, question: str, outcome: str = ""):
        """主动填充 asset 标签缓存"""
        label = f"{asset_id[:10]} - {question}[{outcome}]"
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

    @staticmethod
    def _buy_follow_price_role(leader_price: float, tick_size: Optional[str], best_ask: Optional[float], has_best_ask: bool, spread_thr: float, exceed_order: bool, follow_taker: bool, leader_role: Optional[str] = None) -> Optional[tuple[float, str]]:
        if tick_size is None:
            return leader_price, "maker"
        ts = float(tick_size)
        is_taker = leader_role == "taker" if leader_role else CopyTradingService._is_taker_price(leader_price, tick_size)
        if follow_taker and is_taker and has_best_ask:
            if best_ask - leader_price <= spread_thr:
                return best_ask, "taker"
            if not exceed_order:
                return None
            return leader_price + spread_thr, "maker"
        maker_price = round(min(leader_price + ts, 1 - ts), len(tick_size) - 2)
        if has_best_ask and maker_price >= best_ask:
            return best_ask, "taker"
        return maker_price, "maker"

    @staticmethod
    def _sell_follow_price_role(leader_price: float, tick_size: Optional[str], best_bid: Optional[float], has_best_bid: bool, spread_thr: float, exceed_order: bool, follow_taker: bool, leader_role: Optional[str] = None) -> Optional[tuple[float, str]]:
        if tick_size is None:
            return leader_price, "maker"
        ts = float(tick_size)
        is_taker = leader_role == "taker" if leader_role else CopyTradingService._is_taker_price(leader_price, tick_size)
        if follow_taker and is_taker and has_best_bid:
            if leader_price - best_bid <= spread_thr:
                return best_bid, "taker"
            if not exceed_order:
                return None
            return leader_price - spread_thr, "maker"
        maker_price = round(max(leader_price - ts, ts), len(tick_size) - 2)
        if has_best_bid and maker_price <= best_bid:
            return best_bid, "taker"
        return maker_price, "maker"

    def _record_position_snapshot(
        self,
        config: CopyTradingConfig,
        asset_id: str,
        source: str,
        side: Optional[str] = None,
        event_size: Optional[float] = None,
        event_price: Optional[float] = None,
        order_id: Optional[str] = None,
        leader_tx_hash: Optional[str] = None,
        raw_context: Optional[dict] = None,
    ):
        """记录 config+asset 的 leader/follower 当前仓位快照，供历史曲线查询。"""
        if not config or not asset_id:
            return

        l_addr = config.leader_proxy_wallet
        f_addr = config.follower_proxy_wallet
        follower_position = self._follower_positions.get(f_addr, {}).get(asset_id, 0)
        follower_pending_buy = self._pending_buy_orders.get(f_addr, {}).get(asset_id, 0)
        follower_pending_sell = self._pending_sell_orders.get(f_addr, {}).get(asset_id, 0)

        asyncio.create_task(asyncio.to_thread(
            record_position_history,
            config_id=config.id,
            asset_id=asset_id,
            leader_proxy_wallet=l_addr,
            follower_proxy_wallet=f_addr,
            share_ratio=config.share_ratio,
            leader_position=0,
            follower_position=follower_position,
            follower_pending_buy=follower_pending_buy,
            follower_pending_sell=follower_pending_sell,
            source=source,
            side=side,
            event_size=event_size,
            event_price=event_price,
            order_id=order_id,
            leader_tx_hash=leader_tx_hash,
            raw_context=raw_context,
        ))

    def _record_config_baseline_snapshots(self, config: CopyTradingConfig):
        l_addr = config.leader_proxy_wallet.lower()
        f_addr = config.follower_proxy_wallet.lower()
        assets = (
            set(self._follower_positions.get(f_addr, {}).keys())
            | set(self._pending_buy_orders.get(f_addr, {}).keys())
            | set(self._pending_sell_orders.get(f_addr, {}).keys())
        )
        for asset_id in assets:
            self._record_position_snapshot(config, asset_id, "baseline")

    def _record_all_position_history_snapshots(self):
        """只用于poller记录快照"""
        snapshot_count = 0
        for config in list(self._config_id_to_config.values()):
            if not config.enabled:
                continue
            f_addr = config.follower_proxy_wallet
            assets = (
                set(self._follower_positions.get(f_addr, {}).keys())
                | set(self._pending_buy_orders.get(f_addr, {}).keys())
                | set(self._pending_sell_orders.get(f_addr, {}).keys())
            )
            for asset_id in assets:
                self._record_position_snapshot(config, asset_id, "position_history_poller")
                snapshot_count += 1
        logger.debug(f"[PositionHistory] Recorded {snapshot_count} snapshots from poller")

    def _start_position_history_poller(self, interval=120):
        """启动仓位历史快照定时记录，独立于 position sync。"""
        async def _poll():
            while True:
                await asyncio.sleep(interval)
                try:
                    self._record_all_position_history_snapshots()
                except Exception as e:
                    logger.warning(f"[PositionHistory] Poller error: {e}")

        self._position_history_poller_task = asyncio.create_task(_poll())
        logger.info(f"[PositionHistory] Position history poller started ({interval}s interval)")

    def _start_follower_position_poller(self, interval=3600):
        """启动 follower 仓位轮询兜底同步（WS CONFIRMED 事件的补充）"""
        async def _poll():
            for f_addr in list(self._followers):
                try:
                    await self._sync_follower_positions_from_poly(f_addr)
                except Exception as e:
                    logger.error(f"[PositionPoller] Initial follower sync error for {self._account_service.get_acc_name(f_addr)}: {e}")
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

    def _start_schedule_poller(self, interval=60):
        """启动定时调度检查器，每分钟检查 cron 表达式决定启停"""
        from croniter import croniter
        from .models import get_all_schedules, update_schedule_last_triggered
        from shared.time_utils import now_utc8_dt

        def _cron_fired_in_window(cron_expr: str, last_check, now) -> bool:
            """判断 (last_check, now] 窗口内是否存在 cron 触发点"""
            it = croniter(cron_expr, last_check)
            next_fire = it.get_next(type(now))
            return next_fire <= now

        async def _poll():
            last_check = now_utc8_dt()
            while True:
                try:
                    now = now_utc8_dt()
                    schedules = get_all_schedules(enabled_only=True)
                    for sched in schedules:
                        config_id = sched["config_id"]
                        config = self._config_id_to_config.get(config_id)
                        if not config:
                            continue

                        should_enable = None

                        if sched["start_cron"] and _cron_fired_in_window(sched["start_cron"], last_check, now):
                            should_enable = True
                        if sched["stop_cron"] and _cron_fired_in_window(sched["stop_cron"], last_check, now):
                            should_enable = False

                        if should_enable is not None and config.enabled != should_enable:
                            self.update_config(config_id, enabled=should_enable)
                            update_schedule_last_triggered(sched["id"])
                            action = "启动" if should_enable else "停止"
                            leader_name = self._leader_service.get_leader_name(config.leader_proxy_wallet)
                            follower_name = self._account_service.get_acc_name(config.follower_proxy_wallet)
                            logger.info(f"[Schedule] 定时{action} config#{config_id} ({leader_name} -> {follower_name})")
                    last_check = now
                except Exception as e:
                    logger.warning(f"[Schedule] Poller error: {e}")
                await asyncio.sleep(interval)

        self._schedule_poller_task = asyncio.create_task(_poll())
        logger.info(f"[Schedule] Schedule poller started ({interval}s interval)")

    def stop(self):
        """停止后台任务"""
        if self._follower_poller_task:
            self._follower_poller_task.cancel()
            self._follower_poller_task = None
        if self._pending_poller_task:
            self._pending_poller_task.cancel()
            self._pending_poller_task = None
        if self._position_history_poller_task:
            self._position_history_poller_task.cancel()
            self._position_history_poller_task = None
        if self._schedule_poller_task:
            self._schedule_poller_task.cancel()
            self._schedule_poller_task = None
        # asyncio.create_task(self.market_service.stop())

    async def initialize(self):
        """启动时加载配置，各 poller 自行初始化同步"""
        # await self.market_service.start()
        await self._load_configs()
        self._start_follower_position_poller(interval=120)
        self._start_pending_poller(interval=120)
        self._start_position_history_poller(interval=120)
        self._start_schedule_poller(interval=60)

    async def _load_configs(self):
        """从数据库加载配置（async，供 initialize 调用）"""
        configs = get_copy_trading_configs(enabled_only=False)
        chain_monitor = get_copy_trading_chain_monitor()
        predexon = get_copy_trading_predexon()
        for config in configs:
            self._leader_addr_to_configs.setdefault(config.leader_proxy_wallet, set()).add(config)
            self._follower_addr_to_configs.setdefault(config.follower_proxy_wallet, set()).add(config)
            self._followers.add(config.follower_proxy_wallet)
            self._config_id_to_config[config.id] = config
            self._add_fl_key(config)
            chain_monitor.add_leader(config.leader_proxy_wallet)
            chain_monitor.add_follower(config.follower_proxy_wallet)
            predexon.add_leader(config.leader_proxy_wallet)

        logger.info(f"[CopyTrade] Loaded {len(self._leader_addr_to_configs)} leaders, {len(self._followers)} followers")


    async def process_signal(self, payload: dict):
        """处理 RTDS activity 信号（异步）"""
        # 1. 提取关键字段
        tx_hash = payload.get("transactionHash")
        leader_addr = payload.get("proxyWallet")

        if not tx_hash or not leader_addr:
            logger.debug(f"[CopyTrade] process_signal: missing tx_hash or leader_addr, skipping")
            return
        
        leader_addr = leader_addr.lower()

        # 2. 获取该 leader 下的所有 follower 配置
        configs = self._leader_addr_to_configs.get(leader_addr)
        if not configs:
            return
        logger.debug(f"[CopyTrade] process_signal: found {len(configs)} configs for leader, signal from [{payload.get('source')}]")

        # 3. 内存去重（按 leader 分锁防竞态）
        async with self._get_tx_lock(leader_addr):
            if tx_hash in self._processed_txs:
                logger.debug(f"[CopyTrade] process_signal: tx {tx_hash[:10]} already processed, skipping")
                return
            self._processed_txs.add(tx_hash)

        # 4. 解析信号对象
        signal = ActivitySignal.from_payload(payload)
        if not signal:
            logger.warning(f"[CopyTrade] process_signal: failed to parse signal, skipping")
            return
        if signal.price >= 1.0 or signal.price <= 0:
            logger.debug(f"[CopyTrade] process_signal: price=1.0, skipping (likely settlement)")
            return
        logger.debug(f"[CopyTrade] process_signal: parsed signal: {signal.side} {signal.size} @ {signal.price}, asset={self._asset_label(signal.asset)}, source={payload.get('source')}")

        # self.market_service.subscribe([signal.asset])

        async with self._get_addr_lock(leader_addr):
            enabled_config_ids = [c.id for c in configs if c.enabled]
            if enabled_config_ids:
                asyncio.create_task(asyncio.to_thread(batch_upsert_config_asset, enabled_config_ids, signal.asset))
                if signal.asset not in self._asset_labels:
                    asyncio.create_task(self._get_asset_label(signal.asset))

            for config in configs:
                if not config.enabled:
                    continue
                self._record_position_snapshot(
                    config,
                    signal.asset,
                    "leader_signal",
                    side=signal.side,
                    event_size=signal.size,
                    event_price=signal.price,
                    leader_tx_hash=signal.transaction_hash,
                    raw_context={"source": signal.source},
                )

                # 6. 为每个 follower 执行跟单（只处理 follower 侧，不再更新 leader）
                if signal.side == "BUY":
                    logger.debug(f"[CopyTrade] process_signal: dispatching BUY for config {config.id}")
                    await self._handle_buy(config, signal)
                else:
                    # Leader 全部清仓后，异步撤销 follower 该 asset 的 BUY 挂单
                    if new_leader_size < 5:
                        asyncio.create_task(self._cancel_follower_orders_for_asset(config, signal.asset))
                    logger.debug(f"[CopyTrade] process_signal: dispatching SELL for config {config.id}")
                    await self._handle_sell(config, signal, old_leader_pos)

    async def _handle_buy(self, config: CopyTradingConfig, signal: ActivitySignal):
        """处理 BUY 信号 - 跟 leader 买单"""
        asset_id = signal.asset

        if not (config.buy_price_filter_min <= signal.price <= config.buy_price_filter_max):
            reason = f"price {signal.price} outside filter [{config.buy_price_filter_min}, {config.buy_price_filter_max}]"
            logger.info(f"[CopyTrade] BUY skipped ({reason})")
            self._record_and_notify_order_result(
                config=config,
                signal=signal,
                follow_price=signal.price,
                result=PlaceOrderResult(pending_delta=0, position_delta=0, size=0, price=signal.price, raw_status="SKIPPED", order_id=None, err_msg=reason),
                follower_name=self._account_service.get_acc_name(config.follower_proxy_wallet),
            )
            return

        # 计算跟单数量（含债务缓冲）
        f_addr = config.follower_proxy_wallet
        async with self._get_addr_lock(f_addr):
            leader_price = signal.price

            y = math.floor(signal.size * config.share_ratio * 100) / 100
            follower_name = self._account_service.get_acc_name(config.follower_proxy_wallet)
            leader_name = self._leader_service.get_leader_name(config.leader_proxy_wallet)

            order_book = await self._get_order_book_with_retry(asset_id, "BUY")
            has_best_ask = False
            if not order_book:
                logger.warning(
                    f"[CopyTrade] BUY order book unavailable "
                    f"asset={self._asset_label(asset_id)}, leader={leader_name}, "
                    f"follower={follower_name}, signal_price={signal.price}"
                )
                tick_size = None
                neg_risk = None
                best_ask = None
            else:
                tick_size = order_book["tick_size"]
                neg_risk = order_book["neg_risk"]
                asks = order_book["asks"]
                if not asks:
                    best_ask = None
                    logger.warning(f"[CopyTrade] no asks in order book")
                else:
                    logger.info(
                        f"[CopyTrade] BUY order book values: asset={self._asset_label(asset_id)} "
                        f"tick_size={tick_size}, neg_risk={neg_risk}, asks[-1]={asks[-1]}"
                    )
                    best_ask = float(asks[-1]["price"])
                    has_best_ask = True

            price_role = self._buy_follow_price_role(leader_price, tick_size, best_ask, has_best_ask, config.buy_spread_thr, config.buy_exceed_thr, config.buy_follow_taker, signal.role)
            if price_role is None:
                reason = "spread exceeded, order disabled"
                logger.warning(f"[CopyTrade] BUY skipped ({reason})")
                self._record_and_notify_order_result(
                    config=config,
                    signal=signal,
                    follow_price=leader_price,
                    result=PlaceOrderResult(pending_delta=0, position_delta=0, size=0, price=leader_price, raw_status="SKIPPED", order_id=None, err_msg=reason),
                    follower_name=follower_name,
                )
                return
            follow_price, follower_role = price_role
            follow_price = max(config.buy_price_min, min(follow_price, config.buy_price_max))
            min_size = self._min_order_size("BUY", follow_price, follower_role)
            follow_buy_size = max(y, min_size)

            logger.info(f"""
{'='*50}
[{signal.source}] 🔔 BUY {self._asset_label(asset_id)}
  leader  : {leader_name:<20}{signal.size:>7.2f} @ {leader_price:<6}    [{signal.role or ("taker" if tick_size and self._is_taker_price(leader_price, tick_size) else "maker")}]
  follower: {follower_name:<20}{follow_buy_size:>7.2f} @ {follow_price:<6}    [{follower_role}]
{'='*50}""")

            # 下单
            order_start = time.time()
            result = await self._place_order(
                config, asset_id, "BUY", follow_buy_size,
                price=follow_price, tick_size=tick_size, neg_risk=neg_risk
            )
            signal_to_result_ms = (time.time() - signal.signal_time) * 1000
            order_elapsed_ms = (time.time() - order_start) * 1000
            logger.debug(
                f"[CopyTrade] timer: signal_to_order_result={signal_to_result_ms:.1f}ms "
                f"order_elapsed={order_elapsed_ms:.1f}ms side=BUY asset={self._asset_label(asset_id)} "
                f"status={result.raw_status}"
            )
            self._register_post_order_result(config, result)

            # 统一从 deltas 计算并更新状态
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

        self._record_and_notify_order_result(
            config=config,
            signal=signal,
            follow_price=follow_price,
            result=result,
            follower_name=follower_name,
            follower_role=follower_role if result.raw_status == "DELAYED" else None,
        )

    async def _handle_sell(self, config: CopyTradingConfig, signal: ActivitySignal, old_leader_pos: float):
        """处理 SELL 信号 - 按比例跟卖"""
        asset_id = signal.asset

        f_addr = config.follower_proxy_wallet
        # （读 - 下单 - 返回 - 写）按 follower 账户加锁
        async with self._get_addr_lock(f_addr):
            follower_pos = self._follower_positions.setdefault(f_addr, {}).get(asset_id, 0)
            pending_sell = self._pending_sell_orders.setdefault(f_addr, {}).get(asset_id, 0)
            available_pos = max(0, follower_pos - pending_sell)

            leader_price = signal.price

            follower_name = self._account_service.get_acc_name(config.follower_proxy_wallet)
            leader_name = self._leader_service.get_leader_name(config.leader_proxy_wallet)

            order_book = await self._get_order_book_with_retry(asset_id, "SELL")
            has_best_bid = False
            if not order_book:
                logger.warning(
                    f"[CopyTrade] SELL order book unavailable "
                    f"asset={self._asset_label(asset_id)}, leader={leader_name}, "
                    f"follower={follower_name}, signal_price={signal.price}, "
                )
                tick_size = None
                neg_risk = None
                best_bid = None
            else:
                tick_size = order_book["tick_size"]
                neg_risk = order_book["neg_risk"]
                bids = order_book["bids"]
                if not bids:
                    best_bid = None
                    logger.warning(f"[CopyTrade] no bids in order book")
                else:
                    logger.info(
                        f"[CopyTrade] SELL order book values: asset={self._asset_label(asset_id)} "
                        f"tick_size={tick_size}, neg_risk={neg_risk}, bids[-1]={bids[-1]}"
                    )
                    best_bid = float(bids[-1]["price"])
                    has_best_bid = True

            price_role = self._sell_follow_price_role(leader_price, tick_size, best_bid, has_best_bid, config.sell_spread_thr, config.sell_exceed_thr, config.sell_follow_taker, signal.role)
            if price_role is None:
                reason = "spread exceeded, order disabled"
                logger.warning(f"[CopyTrade] SELL skipped ({reason})")
                self._record_and_notify_order_result(
                    config=config,
                    signal=signal,
                    follow_price=leader_price,
                    result=PlaceOrderResult(pending_delta=0, position_delta=0, size=0, price=leader_price, raw_status="SKIPPED", order_id=None, err_msg=reason),
                    follower_name=follower_name
                )
                return
            follow_price, follower_role = price_role
            follow_price = max(config.sell_price_min, min(follow_price, config.sell_price_max))

            ratio = signal.size / old_leader_pos
            y = math.floor(ratio * available_pos * 100) / 100
            min_size = self._min_order_size("SELL", follow_price, follower_role)
            follow_sell_size = max(y, min_size)
            follow_sell_size = min(follow_sell_size, available_pos)

            if available_pos - follow_sell_size <= 5:
                follow_sell_size = available_pos

            logger.info(f"""
{'='*90}
[{signal.source}] 🔔 SELL {self._asset_label(asset_id)}
  leader  : {leader_name:<20} {signal.size:>7.2f} / {old_leader_pos:>7.2f} @ {leader_price:<6}, ratio={ratio:.4f}    [{signal.role or ("taker" if tick_size and self._is_taker_price(leader_price, tick_size) else "maker")}]
  follower: {follower_name:<20} {follow_sell_size:>7.2f} / {available_pos:>7.2f} @ {follow_price:<6}   [{follower_role}]
{'='*90}""")

            if follow_sell_size <= 0.01:
                reason = "no balance"
                logger.warning(
                    f"[CopyTrade] SELL skipped: no balance "
                    f"(follower={follower_name}, available={available_pos:.2f})"
                )
                self._record_and_notify_order_result(
                    config=config,
                    signal=signal,
                    follow_price=follow_price,
                    result=PlaceOrderResult(pending_delta=0, position_delta=0, size=0, price=follow_price, raw_status="SKIPPED", order_id=None, err_msg=reason),
                    follower_name=follower_name,
                )
                return

            # 下单
            order_start = time.time()
            result = await self._place_order(
                config, asset_id, "SELL", follow_sell_size,
                price=follow_price, tick_size=tick_size, neg_risk=neg_risk
            )
            signal_to_result_ms = (time.time() - signal.signal_time) * 1000
            order_elapsed_ms = (time.time() - order_start) * 1000
            logger.debug(
                f"[CopyTrade] timer: signal_to_order_result={signal_to_result_ms:.1f}ms "
                f"order_elapsed={order_elapsed_ms:.1f}ms side=SELL asset={self._asset_label(asset_id)} "
                f"status={result.raw_status}"
            )
            self._register_post_order_result(config, result)

            # 统一从 deltas 计算并更新状态
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

        self._record_and_notify_order_result(
            config=config,
            signal=signal,
            follow_price=follow_price,
            result=result,
            follower_name=follower_name,
            follower_role=follower_role if result.raw_status == "DELAYED" else None,
        )

    async def _cancel_follower_orders_for_asset(self, config: CopyTradingConfig, asset_id: str):
        """Leader 清仓后，撤销 follower 该 asset 所有挂单"""
        f_addr = config.follower_proxy_wallet
        f_name = self._account_service.get_acc_name(f_addr)
        try:
            order_ids = await asyncio.to_thread(get_live_buy_order_ids_by_asset, config.id, asset_id)
            if not order_ids:
                return
            client = self._account_service.get_or_create_clob_client(f_addr)
            if not client:
                logger.error(f"[CopyTrade] No client for {f_name}, cannot cancel orders on leader exit")
                return
            result = await asyncio.to_thread(client.cancel_orders, order_ids)
            canceled = result.get("canceled", []) if result else []
            not_canceled = result.get("not_canceled", {}) if result else {}
            self._leader_exit_canceled_ids.update(canceled)
            logger.info(
                f"[CopyTrade] Leader exited, canceled {len(canceled)}/{len(order_ids)} orders for "
                f"{f_name} asset={self._asset_label(asset_id)}"
            )
            if not_canceled:
                logger.warning(f"[CopyTrade] Leader exit not_canceled: {not_canceled}")
            logger.info(f"[CopyTrade] Leader exit: canceled {len(canceled)} orders for {f_name} on {asset_id[:10]}")
        except Exception as e:
            logger.error(f"[CopyTrade] Failed to cancel orders on leader exit: {e}")


    async def _get_order_book_with_retry(self, asset_id: str, side: str) -> Optional[dict]:
        """Get CLOB order book with a small retry budget for transient network failures."""
        for attempt in range(3):
            try:
                order_book = await asyncio.to_thread(self._read_only_clob_client.get_order_book, asset_id)
                if order_book:
                    return order_book
                logger.warning(
                    f"[CopyTrade] {side} get_order_book returned empty: "
                    f"asset={self._asset_label(asset_id)}, attempt={attempt + 1}/3"
                )
            except Exception as e:
                logger.warning(
                    f"[CopyTrade] {side} get_order_book failed: "
                    f"asset={self._asset_label(asset_id)}, attempt={attempt + 1}/3, error={e}"
                )
            if attempt < 2:
                await asyncio.sleep(0.1 * (attempt + 1))
        return None

    async def _get_asset_label(self, asset_id: str) -> str:
        """根据 asset_id 返回可读标签 'id[:10] - question[outcome]'
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
            logger.warning(f"[AssetQuestion] No title for {asset_id[:10]}")

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
        follower_role: Optional[str] = None,
    ):
        order_id = result.order_id

        if result.raw_status in ("ERROR", "SKIPPED"):
            raw = f"{signal.transaction_hash}_{signal.asset}_{signal.side}_{time.time()}"
            order_id = f"{result.raw_status}_0x" + hashlib.sha256(raw.encode()).hexdigest()

        # follower_role 由订单状态决定: live→maker, matched→taker, delayed→传入的 role
        if follower_role is None:
            if result.raw_status == "LIVE":
                follower_role = "maker"
            elif result.raw_status == "MATCHED":
                follower_role = "taker"

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
            leader_role=signal.role,
            follower_role=follower_role,
        ))

        if result.raw_status in ("ERROR", "SKIPPED", "DELAYED"):
            logger.warning(f"[CopyTrade] order {result.raw_status}: {signal.side} {signal.asset[:10]} size={result.size} err={result.err_msg}")

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
                f"[CopyTrade] trade patched order row: order_id={order_id[:10]} "
                f"size_matched={order.size_matched:.4f}->{new_size_matched:.4f} status={order.status}->{new_status}"
            )
            order.size_matched = new_size_matched
            order.status = new_status
        return order

    async def _place_order(self, config: CopyTradingConfig, asset_id: str, side: str, size: float, price: float, tick_size: str=None, neg_risk: bool=None) -> PlaceOrderResult:
        """
        纯下单函数，返回 PlaceOrderResult（状态登记和 pending_delta 由调用方处理）。
        SELL 余额不足时内部重试一次（使用实际余额）。
        """
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
                logger.info(f"[CopyTrade] order LIVE: {side} {size} @ {order_price} asset={self._asset_label(asset_id)} order_id={order_id[:10]}")
                return PlaceOrderResult(pending_delta=size, position_delta=0, size=size, price=order_price, raw_status="LIVE", order_id=order_id, err_msg=None)
            elif status == "matched":
                taking = float(result.get("takingAmount") or 0)
                making = float(result.get("makingAmount") or 0)
                filled_size = taking if side == "BUY" else making
                remaining = size - filled_size
                logger.info(f"[CopyTrade] order MATCHED: {side} {size} @ {order_price} filled={filled_size} remaining={remaining} asset={self._asset_label(asset_id)} order_id={order_id[:10]}")
                return PlaceOrderResult(pending_delta=remaining, position_delta=filled_size, size=size, price=order_price, raw_status="MATCHED" if remaining == 0 else "LIVE", order_id=order_id, err_msg=None)
            elif status == "delayed":
                logger.info(f"[CopyTrade] order DELAYED: {side} {size} @ {order_price} asset={self._asset_label(asset_id)} order_id={order_id[:10]}")
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
                        logger.info(f"[CopyTrade] order RETRY LIVE: {side} {retry_size} @ {order_price} asset={self._asset_label(asset_id)} order_id={retry_order_id[:10]}")
                        return PlaceOrderResult(pending_delta=retry_size, position_delta=0, size=retry_size, price=order_price, raw_status="LIVE", order_id=retry_order_id, err_msg=None)
                    elif retry_status == "matched":
                        taking = float(retry_result.get("takingAmount") or 0)
                        making = float(retry_result.get("makingAmount") or 0)
                        retry_filled = taking if side == "BUY" else making
                        retry_remaining = retry_size - retry_filled
                        logger.info(f"[CopyTrade] order RETRY MATCHED: {side} {retry_size} @ {order_price} filled={retry_filled} remaining={retry_remaining} asset={self._asset_label(asset_id)} order_id={retry_order_id[:10]}")
                        return PlaceOrderResult(pending_delta=retry_remaining, position_delta=retry_filled, size=retry_size, price=order_price, raw_status="MATCHED" if retry_remaining == 0 else "LIVE", order_id=retry_order_id, err_msg=None)
                    elif retry_status == "delayed":
                        logger.info(f"[CopyTrade] order RETRY DELAYED: {side} {retry_size} @ {order_price} asset={self._asset_label(asset_id)} order_id={retry_order_id[:10]}")
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
                        order_price, tick_size, neg_risk, config.gtd_expiration_sec,
                    )
                    retry_status = retry_result.get("status")
                    retry_order_id = retry_result.get("orderID")
                    if retry_status == "live":
                        logger.info(f"[CopyTrade] order RETRY LIVE: SELL {retry_size} @ {order_price} asset={self._asset_label(asset_id)} order_id={retry_order_id[:10]}")
                        return PlaceOrderResult(pending_delta=retry_size, position_delta=0, size=retry_size, price=order_price, raw_status="LIVE", order_id=retry_order_id, err_msg=None)
                    elif retry_status == "matched":
                        retry_filled = float(retry_result.get("makingAmount") or 0)
                        retry_remaining = retry_size - retry_filled
                        logger.info(f"[CopyTrade] order RETRY MATCHED: SELL {retry_size} @ {order_price} filled={retry_filled} remaining={retry_remaining} asset={self._asset_label(asset_id)} order_id={retry_order_id[:10]}")
                        return PlaceOrderResult(pending_delta=retry_remaining, position_delta=retry_filled, size=retry_size, price=order_price, raw_status="MATCHED" if retry_remaining == 0 else "LIVE", order_id=retry_order_id, err_msg=None)
                    elif retry_status == "delayed":
                        logger.info(f"[CopyTrade] order RETRY DELAYED: SELL {retry_size} @ {order_price} asset={self._asset_label(asset_id)} order_id={retry_order_id[:10]}")
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
        share_ratio: float,
        owner_user_id: int = 0,
        buy_spread_thr: float = DEFAULT_TAKER_SPREAD_THRESHOLD,
        sell_spread_thr: float = DEFAULT_TAKER_SPREAD_THRESHOLD,
        buy_exceed_thr: bool = DEFAULT_EXCEED_THR,
        sell_exceed_thr: bool = DEFAULT_EXCEED_THR,
        buy_follow_taker: bool = True,
        sell_follow_taker: bool = True,
        buy_price_min: float = DEFAULT_BUY_PRICE_MIN,
        buy_price_max: float = DEFAULT_BUY_PRICE_MAX,
        sell_price_min: float = DEFAULT_SELL_PRICE_MIN,
        sell_price_max: float = DEFAULT_SELL_PRICE_MAX,
        buy_price_filter_min: float = DEFAULT_BUY_PRICE_FILTER_MIN,
        buy_price_filter_max: float = DEFAULT_BUY_PRICE_FILTER_MAX,
    ) -> int:
        """创建跟单配置"""
        leader_proxy_wallet = leader_proxy_wallet.lower()
        follower_proxy_wallet = follower_proxy_wallet.lower()
        # 检查同一 follower 是否已配置相同的 leader（避免重复）
        existing = self._get_config_by_fl(follower_proxy_wallet, leader_proxy_wallet)
        if existing:
            raise ValueError(f"Follower {follower_proxy_wallet} already follows leader {leader_proxy_wallet}")

        # 注册 leader 到链监听器 + Predexon
        get_copy_trading_chain_monitor().add_leader(leader_proxy_wallet)
        get_copy_trading_predexon().add_leader(leader_proxy_wallet)
        # 注册 follower 的 PositionsConverted 监听
        get_copy_trading_chain_monitor().add_follower(follower_proxy_wallet)

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
            config_id = create_copy_trading_config(leader_proxy_wallet, follower_proxy_wallet,
                                                   share_ratio, owner_user_id,
                                                   buy_spread_thr, sell_spread_thr,
                                                   buy_exceed_thr, sell_exceed_thr,
                                                   buy_follow_taker, sell_follow_taker,
                                                   buy_price_min, buy_price_max, sell_price_min, sell_price_max,
                                                   buy_price_filter_min, buy_price_filter_max)
        except pymysql.err.IntegrityError as e:
            raise RuntimeError(f"Follower {follower_proxy_wallet} already follows leader {leader_proxy_wallet}") from e

        # 直接用参数构造对象
        new_config = CopyTradingConfig(
            id=config_id,
            leader_proxy_wallet=leader_proxy_wallet,
            follower_proxy_wallet=follower_proxy_wallet,
            share_ratio=share_ratio,
            enabled=False,
            owner_user_id=owner_user_id,
            buy_spread_thr=buy_spread_thr,
            sell_spread_thr=sell_spread_thr,
            buy_exceed_thr=buy_exceed_thr,
            sell_exceed_thr=sell_exceed_thr,
            buy_price_min=buy_price_min,
            buy_price_max=buy_price_max,
            sell_price_min=sell_price_min,
            sell_price_max=sell_price_max,
            buy_price_filter_min=buy_price_filter_min,
            buy_price_filter_max=buy_price_filter_max,
        )

        # 增量更新缓存
        self._leader_addr_to_configs.setdefault(leader_proxy_wallet, set()).add(new_config)
        self._follower_addr_to_configs.setdefault(follower_proxy_wallet, set()).add(new_config)
        self._followers.add(follower_proxy_wallet)
        self._config_id_to_config[config_id] = new_config
        self._add_fl_key(new_config)
        self._follower_positions.setdefault(follower_proxy_wallet, {})

        # 同步 follower 仓位和 pending
        await self._sync_pending_orders_from_poly(follower_proxy_wallet)
        await self._sync_follower_positions_from_poly(follower_proxy_wallet)
        self._record_config_baseline_snapshots(new_config)

        logger.info(f"[CopyTrade] Created config: follower={follower_proxy_wallet[:10]} follows leader={leader_proxy_wallet[:10]}")
        return config_id

    async def _save_pending_sell_with_question(self, f_addr: str, asset_id: str, pending: float):
        """异步获取 market label 并写入 pending 到 DB"""
        try:
            label = await self._get_asset_label(asset_id)
            await asyncio.to_thread(upsert_follower_pending_sell, f_addr, asset_id, pending, label)
            logger.debug(f"[CopyTrade] Saved pending_sell to DB: {f_addr[:10]} {label} pending={pending}")
        except Exception as e:
            logger.error(f"[CopyTrade] Failed to save pending_sell to DB: {e}")

    async def _save_pending_buy_with_question(self, f_addr: str, asset_id: str, pending: float):
        """异步获取 market label 并写入 BUY pending 到 DB"""
        try:
            label = await self._get_asset_label(asset_id)
            await asyncio.to_thread(upsert_follower_pending_buy, f_addr, asset_id, pending, label)
            logger.debug(f"[CopyTrade] Saved pending_buy to DB: {f_addr[:10]} {label} pending={pending}")
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
                    logger.debug(f"[CopyTrade] Order {order_id[:10]} already processed by _place_order, skip WS PLACEMENT")
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
            logger.info(f"[CopyTrade] order PLACEMENT: {side:>4} {original_size:>7.2f} @ {price:<5} asset={self._asset_label(asset_id)} order_id={order_id[:10]} (new_pos={cur_pos:>7.2f}, new_pending={new_pending:>7.2f})")

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
                    logger.debug(f"[CopyTrade] Order {order_id[:10]} already matched, skip cancellation")
                    return
                if order_id in self._processed_canceled_order_ids:
                    logger.debug(f"[CopyTrade] Order {order_id[:10]} cancellation already processed, skip")
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

                else:
                    cur_pos = self._follower_positions.setdefault(f_addr, {}).get(asset_id, 0)
                    cur_pending = self._pending_sell_orders.setdefault(f_addr, {}).get(asset_id, 0)
                    new_pending = max(0, cur_pending - released)
                    if new_pending <= 0.01:
                        self._pending_sell_orders[f_addr].pop(asset_id, None)
                    else:
                        self._pending_sell_orders[f_addr][asset_id] = new_pending
                    asyncio.create_task(self._save_pending_sell_with_question(f_addr, asset_id, new_pending))
            cancel_reason = "leader_exit" if order_id in self._leader_exit_canceled_ids else "expired_or_manual"
            if order_id in self._leader_exit_canceled_ids:
                self._leader_exit_canceled_ids.discard(order_id)
            logger.info(f"[CopyTrade] order CANCELED : {side:>4} {released:>7.2f} @ {price:<5} asset={self._asset_label(asset_id)} order_id={order_id[:10]} reason={cancel_reason} (position={cur_pos:>7.2f}, pending={new_pending:>7.2f})")

            # 更新订单状态为 cancelled
            asyncio.create_task(asyncio.to_thread(update_copy_trading_order,
                order_id=order_id,
                size_matched=size_matched,
                status=status,
                err_msg=cancel_reason,
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
            config_for_snapshot = self._config_id_to_config.get(config_id)
            
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
                    logger.debug(f"[CopyTrade] Trade {order_id[:10]} already processed by _place_order, skip position update (skipped={skip_amount:.4f})")
                    return
                logger.debug(f"[CopyTrade] Trade {order_id[:10]} partially skipped (skipped={skip_amount:.4f}, remaining={matched_amount:.4f})")

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

            if config_for_snapshot:
                self._record_position_snapshot(
                    config_for_snapshot,
                    asset_id,
                    "follower_trade_confirmed",
                    side=side,
                    event_size=matched_amount,
                    event_price=price,
                    order_id=order_id,
                    leader_tx_hash=order.leader_tx_hash if order else None,
                    raw_context={"config_id": config_id},
                )

        logger.info(f"[CopyTrade] Trade CONFIRMED: {side:>4} {matched_amount:>7.2f} @ {price:<5} asset={self._asset_label(asset_id)} order_id={order_id[:10]} (new_pos={new_size:>7.2f}, new_pending={new_pending:>7.2f})")


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
        # 持久化
        success = update_copy_trading_config(config_id, **kwargs)
        if success:
            # 更新缓存
            config = self._config_id_to_config.get(config_id)
            if config:
                if "share_ratio" in kwargs:
                    config.share_ratio = kwargs["share_ratio"]
                if "enabled" in kwargs:
                    config.enabled = kwargs["enabled"]
                if "gtd_expiration_sec" in kwargs:
                    config.gtd_expiration_sec = int(kwargs["gtd_expiration_sec"])
                if "buy_spread_thr" in kwargs:
                    config.buy_spread_thr = float(kwargs["buy_spread_thr"])
                if "sell_spread_thr" in kwargs:
                    config.sell_spread_thr = float(kwargs["sell_spread_thr"])
                if "buy_exceed_thr" in kwargs:
                    config.buy_exceed_thr = bool(kwargs["buy_exceed_thr"])
                if "sell_exceed_thr" in kwargs:
                    config.sell_exceed_thr = bool(kwargs["sell_exceed_thr"])
                if "buy_follow_taker" in kwargs:
                    config.buy_follow_taker = bool(kwargs["buy_follow_taker"])
                if "sell_follow_taker" in kwargs:
                    config.sell_follow_taker = bool(kwargs["sell_follow_taker"])
                if "buy_price_min" in kwargs:
                    config.buy_price_min = float(kwargs["buy_price_min"])
                if "buy_price_max" in kwargs:
                    config.buy_price_max = float(kwargs["buy_price_max"])
                if "sell_price_min" in kwargs:
                    config.sell_price_min = float(kwargs["sell_price_min"])
                if "sell_price_max" in kwargs:
                    config.sell_price_max = float(kwargs["sell_price_max"])
                if "buy_price_filter_min" in kwargs:
                    config.buy_price_filter_min = float(kwargs["buy_price_filter_min"])
                if "buy_price_filter_max" in kwargs:
                    config.buy_price_filter_max = float(kwargs["buy_price_filter_max"])
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
                self._leader_addr_to_configs[l_addr].discard(cfg)
                if not self._leader_addr_to_configs[l_addr]:
                    del self._leader_addr_to_configs[l_addr]
                    get_copy_trading_chain_monitor().remove_leader(l_addr)
                    get_copy_trading_predexon().remove_leader(l_addr)

            # 检查 f_addr 是否还有其他 config（没有则清理 follower 资源）
            if not self._follower_addr_to_configs.get(f_addr):
                self._followers.discard(f_addr)
                get_copy_trading_chain_monitor().remove_follower(f_addr)
                delete_follower_positions(f_addr)
                self._follower_positions.pop(f_addr, None)

                from .ws import get_copy_trading_ws, remove_copy_trading_ws
                ws = get_copy_trading_ws(f_addr)
                if ws:
                    asyncio.create_task(ws.stop())
                    remove_copy_trading_ws(f_addr)

        return success

    async def process_convert(self, event: dict):
        """处理链上 Convert 事件"""
        logger.debug(f"[Convert] Full event: {event}")
        event_type = event.get("event_type")
        user = event.get("user", "").lower()
        amount = event.get("amount_decimal", "N/A")
        transaction_hash = event.get("transaction_hash", "")
        index_set = event.get("index_set", 0)

        if event_type == "PositionsConverted" and user:
            if user in self._leader_addr_to_configs:
                # 通过 activity API 查询具体被 convert 的市场
                market_info = await self._get_converted_market_info(user, transaction_hash, index_set)
                logger.info(f"[Convert] market_info result: {market_info}")
                market_id = market_info.get("market_id", "") if market_info else ""
                question = market_info.get("question", "") if market_info else ""
                group_item_title = market_info.get("group_item_title", "") if market_info else ""
                link = market_info.get("link", "") if market_info else ""
                leader_name = self._leader_service.get_leader_name(user)
                logger.info(f"[Convert] Detected: {leader_name} converted {amount} on {question[:30]}")
                return

            # 检查是否为 FOLLOWER
            if user in self._followers:
                logger.info(f"[Convert] Follower convert detected for {user[:10]}")
                try:
                    synced = await self._sync_follower_positions_from_poly(user, delay=35)
                    logger.info(f"[Convert] Follower position synced, {synced} positions updated")
                except Exception as e:
                    logger.error(f"[Convert] Follower position sync failed: {e}")

    async def _get_converted_market_info(self, user: str, transaction_hash: str, index_set: int) -> Optional[dict]:
        """通过 user activity API 查询 CONVERSION 记录，再从 event markets 中匹配 conditionId 得到 groupItemTitle

        Args:
            user: 用户地址
            transaction_hash: 链上 PositionsConverted 事件的交易 hash

        Returns:
            {"market_id": conditionId, "question": title, "group_item_title": ..., "link": ...} 或 None
        """
        for attempt in range(20):  # 最多等 1 分钟
            try:
                resp = await asyncio.to_thread(
                    requests.get,
                    "https://data-api.polymarket.com/activity",
                    params={"user": user, "type": "CONVERSION"},
                    timeout=10,
                )
                if resp.status_code != 200:
                    logger.warning(
                        f"[Convert] Activity API error: {resp.status_code} {resp.text[:100]}, attempt {attempt + 1}/20"
                    )
                    await asyncio.sleep(3)
                    continue

                activities = resp.json()
                # 匹配 transaction hash
                activity = None
                for a in activities:
                    if a.get("transactionHash", "").lower() == transaction_hash.lower():
                        activity = a
                        break

                if not activity:
                    logger.warning(
                        f"[Convert] No CONVERSION record found for tx {transaction_hash[:10]}, "
                        f"attempt {attempt + 1}/20"
                    )
                    await asyncio.sleep(3)
                    continue

                logger.info(f"[Convert] Activity matched: {activity}")

                event_slug = activity.get("eventSlug", "")
                condition_id = activity.get("conditionId", "")  # 实际是 negRiskMarketID（event 级别）
                title = activity.get("title", "")
                link = f"https://polymarket.com/event/{event_slug}" if event_slug else ""

                # 用 eventSlug 查到 event，按 market_id 升序排列后用 index_set 定位具体市场
                if event_slug:
                    ev_resp = await asyncio.to_thread(
                        requests.get,
                        "https://gamma-api.polymarket.com/events",
                        params={"slug": event_slug},
                        timeout=10,
                    )
                    if ev_resp.status_code == 200:
                        events = ev_resp.json()
                        if events:
                            ev = events[0]
                            logger.info(
                                f"[Convert] Event: title={ev.get('title')}, "
                                f"markets={len(ev.get('markets', []))}, negRiskMarketID={(ev.get('negRiskMarketID') or '')[:10]}"
                            )
                            # 按 market_id 升序排列
                            markets = sorted(ev.get("markets", []), key=lambda m: m["id"])
                            idx = int(math.log2(index_set))
                            if idx < len(markets):
                                market = markets[idx]
                                group_item_title = market.get("groupItemTitle", "")
                                logger.debug(
                                    f"[Convert] Market resolved: idx={idx}, groupItemTitle={group_item_title!r}, "
                                    f"market_id={(market.get('id') or '')[:10]}, index_set={index_set}"
                                )
                                return {
                                    "market_id": condition_id,
                                    "question": title,
                                    "group_item_title": group_item_title,
                                    "link": link,
                                }
                            else:
                                logger.warning(
                                    f"[Convert] index_set={index_set} (idx={idx}) out of range "
                                    f"for event {event_slug} with {len(markets)} markets"
                                )

                return {"market_id": condition_id, "question": title, "group_item_title": "", "link": link}

            except Exception as e:
                logger.warning(f"[Convert] Failed to fetch activity (attempt {attempt + 1}/20): {e}")
                await asyncio.sleep(3)

        logger.error(f"[Convert] Activity API: tx {transaction_hash[:10]} not found after 20 attempts (1 min)")
        return None
    
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

    def get_position_history(
        self,
        config_id: int,
        asset_id: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        normalized: bool = False,
        limit: int = 2000,
    ) -> List[dict]:
        """获取指定 config+asset 的 leader/follower 仓位历史曲线点。"""
        return get_position_history_from_db(config_id, asset_id, start=start, end=end, normalized=normalized, limit=limit)


def get_copy_trading_service() -> CopyTradingService:
    """获取或创建跟单服务实例"""
    global copy_trading_service
    if copy_trading_service is None:
        copy_trading_service = CopyTradingService()
    return copy_trading_service

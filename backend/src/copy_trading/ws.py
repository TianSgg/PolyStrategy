"""User Channel WebSocket 客户端 - 监听订单确认事件"""
import asyncio
import json
import logging
import websockets
from typing import Dict, Optional, Set

from py_clob_client_v2.clob_types import ApiCreds
from .service import get_copy_trading_service

logger = logging.getLogger(__name__)

USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"


class CopyTradingWS:
    """User Channel WebSocket 客户端（每个 follower account 一个实例）"""

    def __init__(self, follower_addr: str, creds: ApiCreds):
        self.follower_addr = follower_addr.lower()
        self.creds = creds
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._ws = None
        self._processed_trades: Set[str] = set()

    async def start(self):
        """启动 WS 连接"""
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info(f"[CopyTradingWS] {self.follower_addr[:10]} WebSocket starting")

    async def stop(self):
        """停止 WS 连接"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info(f"[CopyTradingWS] {self.follower_addr[:10]} WebSocket stopped")

    async def _run(self):
        """连接循环（指数退避重连：1s → 2s → 4s → ... → 60s）"""
        delay = 1
        while self._running:
            try:
                async with websockets.connect(USER_WS_URL) as ws:
                    self._ws = ws
                    delay = 1  # 连接成功重置退避
                    # 发送认证订阅
                    auth_msg = {
                        "auth": {
                            "apiKey": self.creds.api_key,
                            "secret": self.creds.api_secret,
                            "passphrase": self.creds.api_passphrase,
                        },
                        "type": "user"
                    }
                    await ws.send(json.dumps(auth_msg))
                    logger.info(f"[CopyTradingWS] {self.follower_addr[:10]} subscribed to user channel")

                    async for msg in ws:
                        if not self._running:
                            break
                        await self._handle_message(msg)

            except asyncio.CancelledError:
                self._ws = None
                break
            except Exception as e:
                self._ws = None
                logger.error(f"[CopyTradingWS] {self.follower_addr[:10]} error: {e}")

            if self._running:
                logger.info(f"[CopyTradingWS] {self.follower_addr[:10]} Reconnecting in {delay}s (exp backoff)")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def _handle_message(self, msg: str):
        """处理 WS 消息"""
        try:
            data = json.loads(msg)
            event_type = data.get("event_type")
            status = data.get("status")

            if event_type == "trade" and status == "CONFIRMED":
                tx_hash = data.get("transaction_hash") or data.get("transactionHash", "")
                trader_side = data.get("trader_side", "")
                addr = data.get("maker_address", "")

                # 找到自己（follower）的成交记录
                fills = []

                if trader_side == "MAKER":
                    # 我的挂单被 taker 成交了，从 maker_orders 匹配
                    for order in data.get("maker_orders", []):
                        if order.get("maker_address", "").lower() == self.follower_addr:
                            size = float(order.get("matched_amount") or 0)
                            side = order.get("side", "").upper()
                            asset_id = order.get("asset_id", "")
                            price = float(order.get("price", "0.0"))
                            order_id = order.get("order_id") or ""
                            fills.append((order_id, asset_id, side, size, price))
                else:
                    # TAKER：maker_address 是我的地址
                    if addr.lower() == self.follower_addr:
                        size = float(data.get("size") or 0)
                        side = data.get("side", "").upper()
                        asset_id = data.get("asset_id", "")
                        price = float(data.get("price", "0.0"))
                        order_id = data.get("taker_order_id") or ""
                        fills.append((order_id, asset_id, side, size, price))

                service = get_copy_trading_service()
                for order_id, asset_id, side, size, price in fills:
                    if size <= 0:
                        continue
                    trade_key = f"{tx_hash}:{order_id}:{asset_id}:{side}:{size}"
                    if trade_key in self._processed_trades:
                        logger.debug(
                            f"[CopyTradingWS] Trade {trade_key[:40]}... already processed, skipping"
                        )
                        continue
                    self._processed_trades.add(trade_key)
                    logger.debug(f"[CopyTradingWS] Trade CONFIRMED: {self.follower_addr[:10]}, {side} {size} @ {price} {service._asset_label(asset_id)} order_id={order_id[:10]}")
                    await service.handle_trade_confirmed(self.follower_addr, asset_id, size, side, price, order_id)

            elif event_type == "order":
                # 处理订单事件（PLACEMENT / CANCELLATION）
                maker = data.get("maker_address", "")
                if maker.lower() != self.follower_addr:
                    return
                type = data.get("type", "")
                status = data.get("status", "")
                asset_id = data.get("asset_id", "")
                original_size = float(data.get("original_size") or 0)
                size_matched = float(data.get("size_matched") or 0)
                side = data.get("side", "").upper()

                order_id = data.get("id")
                price = float(data.get("price") or 0)
                service = get_copy_trading_service()
                logger.debug(f"[CopyTradingWS] Order {type}: {status} {side} original={original_size} matched={size_matched} @ price={price} {service._asset_label(asset_id)} order_id={order_id[:10]}")
                await service.handle_order_event(self.follower_addr, asset_id, original_size, size_matched, type, status, side, order_id, price)

        except json.JSONDecodeError:
            logger.warning(f"[CopyTradingWS] Invalid JSON: {msg[:100]}")
        except Exception as e:
            logger.error(f"[CopyTradingWS] Handle error: {e}")

    async def check_latency(self) -> int:
        """返回 ws_user 延迟（毫秒）"""
        if not self._running or not self._ws:
            raise Exception("WS not connected")
        try:
            pong_waiter = await asyncio.wait_for(self._ws.ping(), timeout=3.0)
            latency_sec = await pong_waiter
            return int(latency_sec * 1000)
        except asyncio.TimeoutError:
            raise Exception("WS ping timeout")


# 全局 WS 实例: {proxy_wallet: CopyTradingWS}
_ws_instances: Dict[str, CopyTradingWS] = {}


def get_copy_trading_ws(proxy_wallet: str) -> Optional[CopyTradingWS]:
    """获取 WS 实例"""
    return _ws_instances.get(proxy_wallet.lower())


def get_all_copy_trading_ws() -> Dict[str, 'CopyTradingWS']:
    """获取所有 WS 实例"""
    return _ws_instances


def add_copy_trading_ws(ws: CopyTradingWS):
    """注册 WS 实例"""
    _ws_instances[ws.follower_addr] = ws


def remove_copy_trading_ws(proxy_wallet: str):
    """移除 WS 实例"""
    _ws_instances.pop(proxy_wallet.lower(), None)


async def stop_all_copy_trading_ws():
    """停止所有 WS 实例"""
    for ws in list(_ws_instances.values()):
        await ws.stop()
    _ws_instances.clear()

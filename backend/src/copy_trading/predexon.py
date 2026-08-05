"""Predexon Trades WebSocket 连接管理 - 跟单模块（pending mempool 信号）"""
import asyncio
import json
import logging
import os
from typing import Optional

import websockets

logger = logging.getLogger(__name__)

PREDEXON_WS_URL = "wss://wss.predexon.com/"
PREDEXON_API_KEY = os.getenv("PREDEXON_API_KEY", "")


class CopyTradingPredexon:
    """Predexon Trades WebSocket 连接管理（跟单专用，pending mempool 信号）"""

    def __init__(self):
        self.service = None
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.connected = False
        self._running = False
        self._subscription_id: Optional[str] = None
        self._leader_addresses: set[str] = set()

    async def start(self):
        """启动 Predexon 连接（长运行，由外部 create_task 包裹）"""
        if not PREDEXON_API_KEY:
            logger.warning("[Predexon] PREDEXON_API_KEY not set, skipping")
            return
        logger.info("[Predexon] Starting copy trading Predexon connection...")
        await self._connect_loop()

    def stop(self):
        """标记停止（实际中断由外部 task.cancel() 完成）"""
        logger.info("[Predexon] Stopping copy trading Predexon connection...")
        self._running = False

    def add_leader(self, address: str):
        """动态添加 leader 地址，通过 update 更新已有订阅"""
        addr = address.lower()
        if addr in self._leader_addresses:
            return
        self._leader_addresses.add(addr)
        if self.connected and self._subscription_id:
            task = asyncio.create_task(self._update_subscription())
            task.add_done_callback(self._on_subscription_update_done)

    def remove_leader(self, address: str):
        """动态移除 leader 地址，通过 update 更新已有订阅"""
        addr = address.lower()
        self._leader_addresses.discard(addr)
        if self.connected and self._subscription_id:
            task = asyncio.create_task(self._update_subscription())
            task.add_done_callback(self._on_subscription_update_done)

    async def check_latency(self) -> int:
        """返回 Predexon WS 延迟（毫秒）"""
        if not self.connected or not self.ws:
            raise Exception("Predexon WS not connected")
        pong_waiter = await asyncio.wait_for(self.ws.ping(), timeout=3.0)
        latency_sec = await pong_waiter
        return int(latency_sec * 1000)

    def _on_subscription_update_done(self, task: asyncio.Task):
        if task.exception():
            logger.error(f"[Predexon] Failed to update subscription: {task.exception()}")

    async def _connect_loop(self):
        """连接循环（指数退避重连：1s → 2s → 4s → ... → 60s）"""
        self._running = True
        delay = 1
        while self._running:
            try:
                ws_url = f"{PREDEXON_WS_URL}?api_key={PREDEXON_API_KEY}"
                async with websockets.connect(ws_url, ping_interval=10, open_timeout=15) as ws:
                    self.ws = ws
                    self.connected = True
                    delay = 1
                    logger.info("[Predexon] Connected to Predexon Trades WS")

                    await self._subscribe()

                    async for message in ws:
                        if not self._running:
                            break
                        await self._handle_message(message)

            except asyncio.CancelledError:
                logger.info("[Predexon] Cancelled, exiting connect loop")
                break
            except Exception as e:
                logger.error(f"[Predexon] Connection error: {e}")
                self.connected = False
                self.ws = None
                self._subscription_id = None

            if self._running:
                logger.info(f"[Predexon] Reconnecting in {delay}s (exp backoff)...")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

        self._running = False
        self.connected = False
        self.ws = None
        logger.info("[Predexon] Connection loop ended")

    async def _subscribe(self):
        """首次订阅"""
        subscription = {
            "action": "subscribe",
            "platform": "polymarket",
            "version": 1,
            "type": "orders",
            "filters": {
                "users": list(self._leader_addresses),
                "status": "pending",
            },
        }
        await self.ws.send(json.dumps(subscription))
        logger.info(f"[Predexon] Subscription sent for {len(self._leader_addresses)} leaders")

    async def _update_subscription(self):
        """通过 update action 替换已有订阅的 filters（同一个 subscription_id）"""
        if not self._subscription_id:
            return

        update_msg = {
            "action": "update",
            "subscription_id": self._subscription_id,
            "filters": {
                "users": list(self._leader_addresses),
                "status": "pending",
            },
        }
        await self.ws.send(json.dumps(update_msg))
        logger.info(f"[Predexon] Updated subscription {self._subscription_id} with {len(self._leader_addresses)} leaders")

    async def _handle_message(self, message: str):
        """处理 Predexon 消息"""
        try:
            data = json.loads(message)

            msg_type = data.get("type")

            # 解包 event 信封
            if msg_type == "event":
                event = data["data"]

                event_type = event.get("event_type")
                if event_type != "order_filled":
                    return

                # 转换为 process_signal 期望的 payload 格式
                user = event.get("user", "").lower()
                side = event.get("side", "")
                shares = event.get("shares_normalized", 0)
                price = event.get("price", 0)
                token_id = event.get("token_id", "")
                tx_hash = event.get("tx_hash", "")

                role = event.get("role")    # "taker" / "maker"

                payload = {
                    "proxyWallet": user,
                    "transactionHash": tx_hash,
                    "side": side.upper(),
                    "size": float(shares),
                    "price": float(price),
                    "asset": token_id,
                    "source": "predexon",
                    "role": role,
                }

                if not self.service:
                    from .service import get_copy_trading_service
                    self.service = get_copy_trading_service()
                await self.service.process_signal(payload)
                return

            if msg_type == "connected":
                return
            if msg_type == "ack":
                self._subscription_id = data.get("subscription_id")
                logger.info(f"[Predexon] Subscribed: subscription_id={self._subscription_id}")
                return

        except json.JSONDecodeError:
            logger.warning(f"[Predexon] Invalid JSON: {message[:100]}")
        except Exception as e:
            logger.error(f"[Predexon] Error handling message: {e}")


# 全局实例
_predexon_instance: Optional[CopyTradingPredexon] = None


def get_copy_trading_predexon() -> CopyTradingPredexon:
    """获取或创建 Predexon 实例"""
    global _predexon_instance
    if _predexon_instance is None:
        _predexon_instance = CopyTradingPredexon()
    return _predexon_instance

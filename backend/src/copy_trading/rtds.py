"""RTDS WebSocket 连接管理 - 跟单模块"""
import asyncio
import json
import logging
import websockets
from typing import Optional

from .service import get_copy_trading_service

logger = logging.getLogger(__name__)

RTDS_WS_URL = "wss://ws-live-data.polymarket.com"


class CopyTradingRTDS:
    """RTDS WebSocket 连接管理（跟单专用）"""

    def __init__(self):
        self.service = get_copy_trading_service()
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.connected = False
        self._run_task: Optional[asyncio.Task] = None
        self._closing = False

    async def start(self):
        """启动 RTDS 连接"""
        logger.info("[RTDS] Starting copy trading RTDS connection...")
        self._run_task = asyncio.create_task(self._connect_loop())

    async def stop(self):
        """停止 RTDS 连接"""
        logger.info("[RTDS] Stopping copy trading RTDS connection...")
        self._closing = True
        if self._run_task:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
            self._run_task = None

    async def _connect_loop(self):
        """连接循环（指数退避重连：1s → 2s → 4s → ... → 60s）"""
        delay = 1
        while not self._closing:
            try:
                async with websockets.connect(RTDS_WS_URL, ping_interval=10) as ws:
                    self.ws = ws
                    self.connected = True
                    delay = 1  # 连接成功重置退避
                    logger.info("[RTDS] Connected to Polymarket RTDS")

                    await self._subscribe()

                    async for message in ws:
                        if self._closing:
                            break
                        await self._handle_message(message)

            except asyncio.CancelledError:
                logger.info("[RTDS] Cancelled, exiting connect loop")
                break
            except Exception as e:
                logger.error(f"[RTDS] Connection error: {e}")
                self.connected = False
                self.ws = None

            if not self._closing:
                logger.info(f"[RTDS] Reconnecting in {delay}s (exp backoff)...")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

        self.connected = False
        self.ws = None
        logger.info("[RTDS] Connection loop ended")

    async def _subscribe(self):
        """发送订阅消息"""
        subscription = {
            "action": "subscribe",
            "subscriptions": [
                {
                    "topic": "activity",
                    "type": "orders_matched"
                }
            ]
        }
        await self.ws.send(json.dumps(subscription))
        # logger.info(f"[RTDS] sent sub:{json.dumps(subscription, indent=2)}")

    async def _handle_message(self, message: str):
        """处理 RTDS 消息"""
        try:
            data = json.loads(message)
            payload = data.get("payload", {})

            if payload is not None:
                payload["source"] = "rtds"
                await self.service.process_signal(payload)
        except json.JSONDecodeError:
            logger.warning(f"[RTDS] Invalid JSON: {message[:100]}")
        except Exception as e:
            logger.error(f"[RTDS] Error handling message: {e}")

    async def check_latency(self) -> int:
        """返回 poly_rtds 延迟（毫秒）"""
        if not self.connected or not self.ws:
            raise Exception("RTDS not connected")
        try:
            pong_waiter = await asyncio.wait_for(self.ws.ping(), timeout=3.0)
            latency_sec = await pong_waiter
            return int(latency_sec * 1000)
        except asyncio.TimeoutError:
            raise Exception("RTDS ping timeout")


# 全局 RTDS 实例
copy_trading_rtds: Optional[CopyTradingRTDS] = None


def get_copy_trading_rtds() -> CopyTradingRTDS:
    """获取或创建 RTDS 实例"""
    global copy_trading_rtds
    if copy_trading_rtds is None:
        copy_trading_rtds = CopyTradingRTDS()
    return copy_trading_rtds

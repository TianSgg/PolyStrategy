"""统一通知服务，支持多渠道（Telegram 为主）"""
import asyncio
import html
import logging
import os
from typing import Optional

from dotenv import load_dotenv
load_dotenv()  # 加载 .env，确保 os.getenv 能读取到 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{bot_token}/sendMessage"


class TelegramNotifier:
    """Telegram 通知器"""

    def __init__(self, bot_token: Optional[str] = None, chat_id: Optional[str] = None):
        self._bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self._chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self._enabled = bool(self._bot_token and self._chat_id)
        if not self._enabled:
            logger.warning("[Telegram] 未配置 bot_token 或 chat_id，通知功能已禁用")

    async def send(self, text: str) -> bool:
        """发送 Telegram 消息（非阻塞），返回是否成功"""
        if not self._enabled:
            return False

        url = TELEGRAM_API_URL.format(bot_token=self._bot_token)
        payload = {"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"}

        def _sync_send():
            try:
                resp = requests.post(url, json=payload, timeout=10)
                if resp.status_code == 200:
                    logger.debug(f"[Telegram] 消息发送成功: {text[:50]}...")
                    return True
                else:
                    logger.error(f"[Telegram] 发送失败: {resp.status_code} {resp.text}")
                    return False
            except Exception as e:
                logger.error(f"[Telegram] 发送异常: {e}")
                return False

        return await asyncio.to_thread(_sync_send)


# 全局服务实例
_notification_service: Optional['NotificationService'] = None


class NotificationService:
    """统一通知服务，支持多渠道"""

    def __init__(self):
        self._channels = {
            "telegram": TelegramNotifier(),
        }

    async def send_telegram(self, text: str) -> bool:
        """发送 Telegram 消息（非阻塞）"""
        return await self._channels["telegram"].send(text)

    async def send(self, channel: str, text: str) -> bool:
        """通用发送接口（非阻塞）"""
        ch = self._channels.get(channel)
        if not ch:
            logger.warning(f"[Notify] Unknown channel: {channel}")
            return False
        return await ch.send(text)

    # ==================== 结构化通知接口 ====================

    def _format_account(self, address: str, name: str = "") -> str:
        """格式化账户显示：有名字显示名字（截断20字符），无名字显示截断地址"""
        if name:
            return f"<code>{html.escape(name[:20])}</code>"
        return f"<code>{address[:16]}...</code>"

    async def notify_order_matched(self, side: str, follower: str, size: float, price: float,
                                  asset: str, follower_name: str = "", question: str = ""):
        """BUY/SELL 成交通知（非阻塞）"""
        follower_display = self._format_account(follower, follower_name)
        if question:
            market_display = f"<code>{html.escape(question)}</code>"
        else:
            market_display = f"<code>{asset[:20]}...</code>"
        text = (
            f"🟢 <b>{side} 成交</b>\n"
            f"Follower: {follower_display}\n"
            f"数量: <code>{size}</code> @ <code>{price}</code>\n"
            f"市场: {market_display}"
        )
        return await self.send_telegram(text)

    async def notify_order_pending(self, side: str, follower: str, size: float, price: float,
                                  asset: str, follower_name: str = "", question: str = ""):
        """BUY/SELL 挂单通知（非阻塞）"""
        follower_display = self._format_account(follower, follower_name)
        if question:
            market_display = f"<code>{html.escape(question)}</code>"
        else:
            market_display = f"<code>{asset[:20]}...</code>"
        text = (
            f"🟡 <b>{side} 挂单</b>\n"
            f"Follower: {follower_display}\n"
            f"数量: <code>{size}</code> @ <code>{price}</code>\n"
            f"市场: {market_display}"
        )
        return await self.send_telegram(text)

    async def notify_order_failed(self, side: str, follower: str, size: float, price: float,
                                 asset: str, follower_name: str = "", reason: str = "",
                                 status: str = "", question: str = ""):
        """BUY/SELL 失败通知（非阻塞）"""
        follower_display = self._format_account(follower, follower_name)
        if question:
            market_display = f"<code>{html.escape(question)}</code>"
        else:
            market_display = f"<code>{asset[:20]}...</code>"
        text = (
            f"🔴 <b>{side} 失败</b>\n"
            f"Follower: {follower_display}\n"
            f"数量: <code>{size}</code> @ <code>{price}</code>\n"
            f"市场: {market_display}"
        )
        if status:
            text += f"\nStatus: <code>{status}</code>"
        if reason:
            text += f"\n原因: <code>{reason}</code>"
        return await self.send_telegram(text)

    async def notify_order_skipped(self, side: str, follower: str, size: float, price: float,
                                  asset: str, follower_name: str = "", reason: str = "", question: str = ""):
        """BUY/SELL 跳过通知（非阻塞）"""
        follower_display = self._format_account(follower, follower_name)
        if question:
            market_display = f"<code>{html.escape(question)}</code>"
        else:
            market_display = f"<code>{asset[:20]}...</code>"
        text = (
            f"⚠️ <b>{side} 跳过</b>\n"
            f"Follower: {follower_display}\n"
            f"数量: <code>{size}</code> @ <code>{price}</code>\n"
            f"市场: {market_display}\n"
            f"原因: <code>{reason}</code>"
        )
        return await self.send_telegram(text)

    async def notify_convert_detected(
        self, leader: str, amount: str, market_id: str,
        leader_name: str = "", question: str = "",
        group_item_title: str = "", link: str = ""
    ):
        """Leader Convert 事件检测通知（非阻塞）"""
        question_escaped = html.escape(question[:80])
        if len(question) > 80:
            question_escaped += "..."
        leader_display = self._format_account(leader, leader_name)
        text = (
            f"⚠️ <b>Convert 检测</b>\n"
            f"Leader: {leader_display}\n"
            f"Amount: <code>{amount}</code>\n"
            f"问题: <code>{question_escaped}</code>\n"
            f"Market: <code>{market_id[:40]}...</code>"
        )
        if group_item_title:
            text += f"\n选项: <code>{html.escape(group_item_title)}</code>"
        if link:
            text += f"\n🔗 <a href='{link}'>去处理 Convert</a>"
        return await self.send_telegram(text)


    async def notify_leader_exit(
        self, follower: str, asset_id: str,
        follower_name: str = "", canceled_count: int = 0, question: str = ""
    ):
        """Leader 清仓退出，撤单通知"""
        follower_display = self._format_account(follower, follower_name)
        question_escaped = html.escape(question[:80]) if question else asset_id[:10]
        text = (
            f"🚪 <b>Leader 退出</b>\n"
            f"Follower: {follower_display}\n"
            f"市场: <code>{question_escaped}</code>\n"
            f"已撤销 {canceled_count} 笔挂单"
        )
        return await self.send_telegram(text)


def get_notification_service() -> NotificationService:
    """获取通知服务单例"""
    global _notification_service
    if _notification_service is None:
        _notification_service = NotificationService()
    return _notification_service

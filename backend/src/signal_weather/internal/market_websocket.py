"""Polymarket market-channel WebSocket implementation.

The implementation currently lives beside the HTTP client for compatibility
with the existing reconnect/subscription code. This module is the stable
internal import boundary for WebSocket consumers.
"""

from .polymarket_client import MonitorProtocol, SharedMarketWebSocket

__all__ = ["MonitorProtocol", "SharedMarketWebSocket"]

"""跟单模块数据类型定义"""
import time
from dataclasses import dataclass
from typing import Optional


@dataclass(eq=False)
class CopyTradingConfig:
    """跟单配置"""
    id: int
    leader_proxy_wallet: str
    follower_proxy_wallet: str
    enabled: bool = True
    owner_user_id: int = 0
    gtd_expiration_sec: int = 1800
    buy_size: float = 100.0
    size_mode: str = "fixed"
    size_ratio: float = 1.0
    size_min: float = 0.0

    def __hash__(self):
        return hash(self.id)


@dataclass
class ActivitySignal:
    """RTDS Activity 信号"""
    proxy_wallet: str
    transaction_hash: str
    side: str
    signal_time: float
    size: float
    price: float
    asset: str
    source: str
    role: Optional[str] = None

    @classmethod
    def from_payload(cls, payload: dict) -> Optional['ActivitySignal']:
        try:
            return cls(
                proxy_wallet=payload["proxyWallet"],
                transaction_hash=payload["transactionHash"],
                side=payload["side"].upper(),
                signal_time=time.time(),
                size=float(payload["size"]),
                price=float(payload["price"]),
                asset=payload["asset"],
                source=payload["source"],
                role=payload.get("role"),
            )
        except (KeyError, ValueError, TypeError):
            return None


@dataclass
class PlaceOrderResult:
    """_place_order 的返回类型，分离接口层和业务层"""
    pending_delta: float
    position_delta: float
    size: float           # 实际下单的份额（用于 record_copy_trading_order）
    price: float          # 实际下单价格（用于 WS 回查 config/price）
    raw_status: Optional[str]  # "live" | "matched" | "delayed" | None
    order_id: Optional[str]   # 订单 ID（用于通知）
    err_msg: Optional[str]    # 错误信息，None 表示成功


@dataclass
class CopyTradingOrder:
    """订单表记录"""
    id: str
    config_id: int
    leader: str
    follower: str
    leader_tx_hash: str
    asset_id: str
    side: str
    leader_size: float
    leader_price: float
    follow_size: float
    follow_price: float
    size_matched: float
    status: str
    created_at: Optional[str]
    updated_at: Optional[str]

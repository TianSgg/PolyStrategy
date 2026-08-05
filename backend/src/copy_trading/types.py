"""跟单模块数据类型定义"""
import time
from dataclasses import dataclass
from typing import Optional

INF = 1e10          # 大数表示无穷（同时作为 MySQL 存储值）
DEFAULT_TAKER_SPREAD_THRESHOLD = 0.05
DEFAULT_EXCEED_THR = True
DEFAULT_BUY_PRICE_MIN = 0.001
DEFAULT_BUY_PRICE_MAX = 0.999
DEFAULT_SELL_PRICE_MIN = 0.001
DEFAULT_SELL_PRICE_MAX = 0.999
DEFAULT_BUY_PRICE_FILTER_MIN = 0.001
DEFAULT_BUY_PRICE_FILTER_MAX = 0.998


@dataclass(eq=False)
class CopyTradingConfig:
    """跟单配置"""
    id: int
    leader_proxy_wallet: str
    follower_proxy_wallet: str
    share_ratio: float
    enabled: bool = True
    threshold: float = INF   # 总额度，INF=不限制
    allowance: float = INF  # 当前可用额度，0=耗尽，INF=无穷
    owner_user_id: int = 0
    gtd_expiration_sec: int = 1800  # GTD 订单过期时间（秒），默认 30 分钟
    buy_spread_thr: float = DEFAULT_TAKER_SPREAD_THRESHOLD
    sell_spread_thr: float = DEFAULT_TAKER_SPREAD_THRESHOLD
    buy_exceed_thr: bool = DEFAULT_EXCEED_THR
    sell_exceed_thr: bool = DEFAULT_EXCEED_THR
    buy_follow_taker: bool = True
    sell_follow_taker: bool = True
    auto_merge_enabled: bool = False
    auto_merge_threshold: float = 100.0
    buy_only: bool = False
    buy_price_min: float = DEFAULT_BUY_PRICE_MIN
    buy_price_max: float = DEFAULT_BUY_PRICE_MAX
    sell_price_min: float = DEFAULT_SELL_PRICE_MIN
    sell_price_max: float = DEFAULT_SELL_PRICE_MAX
    buy_price_filter_min: float = DEFAULT_BUY_PRICE_FILTER_MIN
    buy_price_filter_max: float = DEFAULT_BUY_PRICE_FILTER_MAX

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
    leader_role: Optional[str]
    follower_role: Optional[str]
    created_at: Optional[str]
    updated_at: Optional[str]

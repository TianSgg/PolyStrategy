"""下单/撤单 — 策略进程共用的交易执行接口。"""
import time
import logging
from typing import Optional

from py_clob_client_v2.clob_types import PartialCreateOrderOptions, OrderType, OrderPayload
from py_clob_client_v2.clob_types import OrderArgsV2 as OrderArgs
from py_clob_client_v2.order_builder.constants import BUY, SELL

logger = logging.getLogger(__name__)

DEFAULT_GTD_EXPIRATION_SEC = 30 * 60


def place_limit_order(
    proxy_wallet: str,
    token_id: str,
    side: str,
    size: float,
    price: float,
    tick_size: str = None,
    neg_risk: bool = None,
    gtd_expiration_sec: int = None,
) -> Optional[dict]:
    from account_service.service import get_account_service
    service = get_account_service()
    client = service.get_or_create_clob_client(proxy_wallet)
    if not client:
        raise RuntimeError(f"No client for {service.get_acc_name(proxy_wallet)}")

    if side == "BUY":
        exp_sec = gtd_expiration_sec or DEFAULT_GTD_EXPIRATION_SEC
        expiration = int(time.time()) + exp_sec
        return client.create_and_post_order(
            OrderArgs(token_id=token_id, side=BUY, size=size, price=price, expiration=expiration),
            PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk),
            order_type=OrderType.GTD,
        )
    else:
        return client.create_and_post_order(
            OrderArgs(token_id=token_id, side=SELL, size=size, price=price),
            PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk),
            order_type=OrderType.GTC,
        )


def cancel_order(proxy_wallet: str, order_id: str) -> dict:
    from account_service.service import get_account_service
    service = get_account_service()
    client = service.get_or_create_clob_client(proxy_wallet)
    if not client:
        raise RuntimeError(f"No client for {service.get_acc_name(proxy_wallet)}")
    return client.cancel_order(OrderPayload(orderID=order_id))

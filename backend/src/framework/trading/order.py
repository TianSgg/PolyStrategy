"""下单/撤单 — 策略进程共用的交易执行接口。"""
import time
import logging
from typing import Optional

from py_clob_client_v2.clob_types import PartialCreateOrderOptions, OrderType, OrderPayload
from py_clob_client_v2.clob_types import OrderArgsV2 as OrderArgs
from py_clob_client_v2.order_builder.constants import BUY, SELL

from framework.trading.provider import get_client, repair_client_signature

logger = logging.getLogger(__name__)

DEFAULT_GTD_EXPIRATION_SEC = 30 * 60

_SIGNATURE_ERROR_KEYWORDS = ("signature does not match", "invalid poly_1271", "invalid signature")


def _is_signature_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(kw in msg for kw in _SIGNATURE_ERROR_KEYWORDS)


def _do_place(client, token_id, side, size, price, tick_size, neg_risk, gtd_expiration_sec):
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
    client = get_client(proxy_wallet)
    try:
        return _do_place(client, token_id, side, size, price, tick_size, neg_risk, gtd_expiration_sec)
    except Exception as first_err:
        if not _is_signature_error(first_err):
            raise
        logger.warning("Signature error for %s, attempting auto-repair: %s", proxy_wallet[:8], first_err)
        new_type = repair_client_signature(proxy_wallet)
        if new_type is None:
            raise
        client = get_client(proxy_wallet)
        return _do_place(client, token_id, side, size, price, tick_size, neg_risk, gtd_expiration_sec)


def cancel_order(proxy_wallet: str, order_id: str) -> dict:
    client = get_client(proxy_wallet)
    return client.cancel_order(OrderPayload(orderID=order_id))

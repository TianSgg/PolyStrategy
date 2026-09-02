"""快速下单测试脚本。

用法:
    cd backend/tests
    python test_order.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv
_project_root = Path(__file__).resolve().parent.parent.parent
load_dotenv(_project_root / ".env", override=True)

from py_clob_client_v2 import ClobClient
from py_clob_client_v2.clob_types import (
    ApiCreds, PartialCreateOrderOptions, OrderType, OrderPayload,
)
from py_clob_client_v2.clob_types import OrderArgsV2 as OrderArgs
from py_clob_client_v2.order_builder.constants import BUY, SELL

# ═══════════════════════════════════════════════════════════
# 配置区 — 修改这里
# ═══════════════════════════════════════════════════════════
PRIVATE_KEY = ""
TOKEN_ID = ""
SIZE = 5.0
PRICE = 0.99
# ═══════════════════════════════════════════════════════════


def create_client(private_key: str) -> ClobClient:
    print("\n[1/4] 创建临时 client 获取 API credentials...")
    temp_client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=private_key,
    )

    print("[2/4] 获取/创建 API key...")
    try:
        creds = temp_client.create_or_derive_api_creds()
    except Exception as e:
        print(f"  ✗ 获取 API creds 失败: {e}")
        sys.exit(1)
    print(f"  ✓ API key: {creds.api_key[:16]}...")

    print("[3/4] 获取 proxy wallet 地址...")
    try:
        from eth_account import Account
        account = Account.from_key(private_key)
        funder_address = account.address
    except Exception:
        funder_address = ""
    print(f"  ✓ Funder: {funder_address}")

    print("[4/4] 创建正式 ClobClient...")
    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=private_key,
        creds=creds,
        signature_type=0,
        funder=funder_address,
        retry_on_error=True,
    )
    return client


def check_connectivity(client: ClobClient):
    print("\n═══ 连接测试 ═══")
    try:
        server_time = client.get_server_time()
        print(f"  ✓ 服务器时间: {server_time}")
    except Exception as e:
        print(f"  ✗ get_server_time 失败: {e}")
        return False
    return True


def check_market_info(client: ClobClient, token_id: str):
    print(f"\n═══ 市场信息 (token: {token_id[:16]}...) ═══")
    try:
        book = client.get_order_book(token_id)
        if book:
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            best_bid = bids[0] if bids else None
            best_ask = asks[0] if asks else None
            print(f"  Best Bid: {best_bid}")
            print(f"  Best Ask: {best_ask}")
            print(f"  Bid levels: {len(bids)}, Ask levels: {len(asks)}")
        else:
            print("  ✗ 订单簿为空")
    except Exception as e:
        print(f"  ✗ get_order_book 失败: {e}")

    try:
        tick_size = client.get_tick_size(token_id)
        print(f"  Tick size: {tick_size}")
    except Exception as e:
        print(f"  ✗ get_tick_size 失败: {e}")
        tick_size = "0.01"

    try:
        neg_risk = client.get_neg_risk(token_id)
        print(f"  Neg risk: {neg_risk}")
    except Exception as e:
        print(f"  ✗ get_neg_risk 失败: {e}")
        neg_risk = False

    return tick_size, neg_risk


def check_place_order(client: ClobClient, token_id: str, tick_size: str, neg_risk: bool):
    expiration = int(time.time()) + 1800

    print(f"\n═══ 下单测试 ═══")
    print(f"  Side: BUY")
    print(f"  Price: {PRICE}")
    print(f"  Size: {SIZE}")
    print(f"  Tick size: {tick_size}")
    print(f"  Neg risk: {neg_risk}")
    print(f"  Expiration: {expiration} (30min GTD)")
    print()

    confirm = input("  确认下单? (y/N): ").strip().lower()
    if confirm != "y":
        print("  取消")
        return None

    print("  下单中...")
    start_ns = time.monotonic_ns()

    try:
        result = client.create_and_post_order(
            OrderArgs(
                token_id=token_id,
                side=BUY,
                size=SIZE,
                price=PRICE,
                expiration=expiration,
            ),
            PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk),
            order_type=OrderType.GTD,
        )
    except Exception as e:
        elapsed_ms = (time.monotonic_ns() - start_ns) / 1_000_000
        print(f"  ✗ 下单异常 ({elapsed_ms:.1f}ms): {type(e).__name__}: {e}")
        return None

    elapsed_ms = (time.monotonic_ns() - start_ns) / 1_000_000
    print(f"  响应耗时: {elapsed_ms:.1f}ms")
    print(f"  返回结果: {result}")

    if result:
        status = result.get("status", "unknown")
        order_id = result.get("orderID", "")
        print(f"\n  Status: {status}")
        print(f"  Order ID: {order_id}")

        if status == "matched":
            print(f"  ✓ 立即成交! takingAmount={result.get('takingAmount')}, makingAmount={result.get('makingAmount')}")
        elif status == "live":
            print(f"  ✓ 挂单成功，等待成交")
            cancel = input("\n  是否立即撤单? (y/N): ").strip().lower()
            if cancel == "y":
                try:
                    cancel_result = client.cancel_order(OrderPayload(orderID=order_id))
                    print(f"  撤单结果: {cancel_result}")
                except Exception as e:
                    print(f"  ✗ 撤单失败: {e}")
        else:
            print(f"  ✗ 下单失败! status={status}")
            print(f"  完整响应: {result}")
    else:
        print("  ✗ 返回为空 (None)")

    return result


def main():
    print("=" * 60)
    print("PolyStrategy 下单链路测试")
    print("=" * 60)

    if not PRIVATE_KEY:
        print("\n✗ 请先在文件顶部填写 PRIVATE_KEY")
        sys.exit(1)
    if not TOKEN_ID:
        print("\n✗ 请先在文件顶部填写 TOKEN_ID")
        sys.exit(1)

    print(f"\n  Token ID: {TOKEN_ID[:16]}...")
    print(f"  Size: {SIZE}")
    print(f"  Price: {PRICE}")

    client = create_client(PRIVATE_KEY)

    if not check_connectivity(client):
        sys.exit(1)

    tick_size, neg_risk = check_market_info(client, TOKEN_ID)

    check_place_order(client, TOKEN_ID, tick_size, neg_risk)

    print("\n═══ 测试完成 ═══")


if __name__ == "__main__":
    main()

UPDATE copy_trading_orders
SET status = 'CANCELED'
WHERE status = 'CANCELLED';

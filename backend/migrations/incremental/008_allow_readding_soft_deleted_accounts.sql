-- Allow a wallet address to be re-added after its account has been soft-deleted.
-- Active accounts remain globally unique; deleted rows retain their history.
ALTER TABLE accounts
  ADD COLUMN active_wallet_address VARCHAR(128)
    GENERATED ALWAYS AS (
      CASE WHEN deleted_at IS NULL THEN LOWER(wallet_address) ELSE NULL END
    ) STORED;

ALTER TABLE accounts
  DROP INDEX idx_wallet_address,
  ADD UNIQUE KEY uq_accounts_active_wallet_address (active_wallet_address);

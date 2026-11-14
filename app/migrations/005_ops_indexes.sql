-- Admin operations query indexes. Idempotent; does not rebuild or alter order rows.
CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at);
CREATE INDEX IF NOT EXISTS idx_orders_paid_at ON orders(paid_at);
CREATE INDEX IF NOT EXISTS idx_orders_lookup_email_created ON orders(lookup_email, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_recharge_email_created ON orders(recharge_email, created_at);
CREATE INDEX IF NOT EXISTS idx_fulfillments_status_updated ON fulfillments(status, updated_at);

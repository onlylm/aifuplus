ALTER TABLE orders ADD COLUMN lookup_email TEXT NOT NULL DEFAULT '';
CREATE INDEX idx_orders_lookup ON orders(order_no, lookup_email);

CREATE TABLE site_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT ''
);

ALTER TABLE events ADD COLUMN detail TEXT NOT NULL DEFAULT '';

ALTER TABLE upstream_config ADD COLUMN last_test_at INTEGER NOT NULL DEFAULT 0;
ALTER TABLE upstream_config ADD COLUMN last_test_ok INTEGER NOT NULL DEFAULT 0;
ALTER TABLE upstream_config ADD COLUMN last_test_msg TEXT NOT NULL DEFAULT '';
ALTER TABLE upstream_config ADD COLUMN last_test_detail TEXT NOT NULL DEFAULT '';
ALTER TABLE upstream_config ADD COLUMN last_sync_at INTEGER NOT NULL DEFAULT 0;
ALTER TABLE upstream_config ADD COLUMN auto_card_open INTEGER NOT NULL DEFAULT 0;

CREATE TABLE upstream_plans (
  plan_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  regions TEXT NOT NULL DEFAULT '',
  usable INTEGER NOT NULL DEFAULT 1,
  note TEXT NOT NULL DEFAULT '',
  synced_at INTEGER NOT NULL DEFAULT 0
);

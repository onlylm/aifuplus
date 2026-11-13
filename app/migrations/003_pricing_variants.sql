-- GPT direct-recharge pricing: one row per (plan, region). Money is stored in integer minor units:
--   *_usd_cents = US cents, *_cny = CNY fen, rates = rate x 1,000,000, percentages = basis points.
CREATE TABLE plan_variants (
  id INTEGER PRIMARY KEY,
  product_id INTEGER NOT NULL REFERENCES products(id),
  plan_id TEXT NOT NULL,
  plan_label TEXT NOT NULL,
  region_code TEXT NOT NULL,
  region_label TEXT NOT NULL,
  region_currency TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 0,
  upstream_available INTEGER NOT NULL DEFAULT 1,
  cost_source TEXT NOT NULL DEFAULT 'manual' CHECK (cost_source IN ('upstream','manual')),
  upstream_cost_usd_cents INTEGER,            -- last VALID upstream reference (NULL = never had one)
  upstream_latest_null INTEGER NOT NULL DEFAULT 0,   -- 1 when the most recent sync returned null for this region
  pending_upstream_cost_usd_cents INTEGER,    -- new reading held back because it moved too much
  manual_package_cost_cny INTEGER,
  effective_package_cost_cny INTEGER,
  other_cost_cny INTEGER NOT NULL DEFAULT 0,
  total_cost_cny INTEGER,
  pricing_mode TEXT NOT NULL DEFAULT 'percentage' CHECK (pricing_mode IN ('percentage','fixed')),
  markup_basis_points INTEGER NOT NULL DEFAULT 2000,
  fixed_profit_cny INTEGER NOT NULL DEFAULT 0,
  rounding TEXT NOT NULL DEFAULT 'cent' CHECK (rounding IN ('cent','yuan_up','tail_90','tail_99')),
  sale_price_cny INTEGER,                     -- the published price (NULL = no price yet, cannot be sold)
  fx_rate_snapshot INTEGER,
  anomaly TEXT NOT NULL DEFAULT '',           -- '' or a plain-language reason awaiting confirmation
  anomaly_detail TEXT NOT NULL DEFAULT '',
  upstream_synced_at INTEGER,
  price_calculated_at INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE (plan_id, region_code)
);
CREATE INDEX idx_variants_product ON plan_variants(product_id, enabled);

CREATE TABLE pricing_settings (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  fx_mode TEXT NOT NULL DEFAULT 'auto' CHECK (fx_mode IN ('auto','manual')),
  fx_auto_micro INTEGER,                      -- last SUCCESSFUL automatic USD/CNY (applied)
  fx_auto_at INTEGER,
  fx_auto_source TEXT NOT NULL DEFAULT '',
  fx_pending_micro INTEGER,                   -- automatic rate held back because it moved too much
  fx_pending_detail TEXT NOT NULL DEFAULT '',
  fx_manual_micro INTEGER,
  fx_safety_bp INTEGER NOT NULL DEFAULT 0,
  anomaly_bp INTEGER NOT NULL DEFAULT 1000,
  default_markup_bp INTEGER NOT NULL DEFAULT 2000,
  fx_last_try_at INTEGER NOT NULL DEFAULT 0,
  fx_last_error TEXT NOT NULL DEFAULT '',
  upstream_last_ok_at INTEGER,
  upstream_last_try_at INTEGER NOT NULL DEFAULT 0,
  upstream_last_error TEXT NOT NULL DEFAULT '',
  next_sync_at INTEGER NOT NULL DEFAULT 0,
  sync_lock_until INTEGER NOT NULL DEFAULT 0
);
INSERT INTO pricing_settings (id) VALUES (1);

CREATE TABLE pricing_audit (
  id INTEGER PRIMARY KEY,
  created_at INTEGER NOT NULL,
  actor TEXT NOT NULL,
  kind TEXT NOT NULL,
  plan_id TEXT NOT NULL DEFAULT '',
  region_code TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_audit_time ON pricing_audit(id);

-- price / cost snapshot stored on every order (history is never recomputed)
ALTER TABLE orders ADD COLUMN variant_id INTEGER;
ALTER TABLE orders ADD COLUMN snap_plan_label TEXT NOT NULL DEFAULT '';
ALTER TABLE orders ADD COLUMN snap_region_label TEXT NOT NULL DEFAULT '';
ALTER TABLE orders ADD COLUMN snap_package_cost_cny INTEGER;
ALTER TABLE orders ADD COLUMN snap_other_cost_cny INTEGER;
ALTER TABLE orders ADD COLUMN snap_total_cost_cny INTEGER;
ALTER TABLE orders ADD COLUMN snap_fx_rate INTEGER;
ALTER TABLE orders ADD COLUMN snap_pricing_mode TEXT NOT NULL DEFAULT '';
ALTER TABLE orders ADD COLUMN snap_cost_source TEXT NOT NULL DEFAULT '';
ALTER TABLE orders ADD COLUMN snap_expected_profit_cny INTEGER;

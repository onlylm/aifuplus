-- Direct retail price mode: admin sets final CNY sale price (integer fen) without markup formulas.
-- Rebuild plan_variants so pricing_mode CHECK accepts manual_retail; preserve all existing rows.

CREATE TABLE plan_variants_004 (
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
  upstream_cost_usd_cents INTEGER,
  upstream_latest_null INTEGER NOT NULL DEFAULT 0,
  pending_upstream_cost_usd_cents INTEGER,
  manual_package_cost_cny INTEGER,
  effective_package_cost_cny INTEGER,
  other_cost_cny INTEGER NOT NULL DEFAULT 0,
  total_cost_cny INTEGER,
  pricing_mode TEXT NOT NULL DEFAULT 'percentage' CHECK (pricing_mode IN ('percentage','fixed','manual_retail')),
  markup_basis_points INTEGER NOT NULL DEFAULT 2000,
  fixed_profit_cny INTEGER NOT NULL DEFAULT 0,
  manual_sale_price_cny INTEGER,
  rounding TEXT NOT NULL DEFAULT 'cent' CHECK (rounding IN ('cent','yuan_up','tail_90','tail_99')),
  sale_price_cny INTEGER,
  fx_rate_snapshot INTEGER,
  anomaly TEXT NOT NULL DEFAULT '',
  anomaly_detail TEXT NOT NULL DEFAULT '',
  upstream_synced_at INTEGER,
  price_calculated_at INTEGER,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE (plan_id, region_code)
);

INSERT INTO plan_variants_004 (
  id, product_id, plan_id, plan_label, region_code, region_label, region_currency, enabled, upstream_available,
  cost_source, upstream_cost_usd_cents, upstream_latest_null, pending_upstream_cost_usd_cents,
  manual_package_cost_cny, effective_package_cost_cny, other_cost_cny, total_cost_cny,
  pricing_mode, markup_basis_points, fixed_profit_cny, manual_sale_price_cny, rounding, sale_price_cny,
  fx_rate_snapshot, anomaly, anomaly_detail, upstream_synced_at, price_calculated_at, created_at, updated_at
)
SELECT
  id, product_id, plan_id, plan_label, region_code, region_label, region_currency, enabled, upstream_available,
  cost_source, upstream_cost_usd_cents, upstream_latest_null, pending_upstream_cost_usd_cents,
  manual_package_cost_cny, effective_package_cost_cny, other_cost_cny, total_cost_cny,
  pricing_mode, markup_basis_points, fixed_profit_cny, NULL, rounding, sale_price_cny,
  fx_rate_snapshot, anomaly, anomaly_detail, upstream_synced_at, price_calculated_at, created_at, updated_at
FROM plan_variants;

DROP TABLE plan_variants;
ALTER TABLE plan_variants_004 RENAME TO plan_variants;
CREATE INDEX idx_variants_product ON plan_variants(product_id, enabled);

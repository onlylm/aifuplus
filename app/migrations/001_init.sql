CREATE TABLE users (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);

CREATE TABLE alipay_config (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  enabled INTEGER NOT NULL DEFAULT 0,
  mode TEXT NOT NULL DEFAULT 'sandbox' CHECK (mode IN ('sandbox','production')),
  app_id TEXT NOT NULL DEFAULT '',
  merchant_pid TEXT NOT NULL DEFAULT '',
  private_key_enc TEXT NOT NULL DEFAULT '',
  alipay_public_key TEXT NOT NULL DEFAULT '',
  public_base_url TEXT NOT NULL DEFAULT '',
  order_timeout_minutes INTEGER NOT NULL DEFAULT 15,
  updated_at INTEGER NOT NULL DEFAULT 0
);
INSERT INTO alipay_config (id) VALUES (1);

CREATE TABLE upstream_config (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  enabled INTEGER NOT NULL DEFAULT 0,
  base_url TEXT NOT NULL DEFAULT 'https://pro20x.com/api/v3/automate',
  api_key_enc TEXT NOT NULL DEFAULT '',
  card_platform_id TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL DEFAULT 0
);
INSERT INTO upstream_config (id) VALUES (1);

CREATE TABLE products (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  type TEXT NOT NULL CHECK (type IN ('card','api_recharge')),
  price_cents INTEGER NOT NULL CHECK (price_cents > 0),
  enabled INTEGER NOT NULL DEFAULT 0,
  max_per_order INTEGER NOT NULL DEFAULT 1 CHECK (max_per_order >= 1),
  plan_id TEXT NOT NULL DEFAULT '',
  regions TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE orders (
  id INTEGER PRIMARY KEY,
  order_no TEXT NOT NULL UNIQUE,
  user_id INTEGER REFERENCES users(id),
  guest_token_hash TEXT,
  product_id INTEGER NOT NULL REFERENCES products(id),
  product_name TEXT NOT NULL,
  product_type TEXT NOT NULL,
  unit_price_cents INTEGER NOT NULL,
  quantity INTEGER NOT NULL,
  amount_cents INTEGER NOT NULL,
  payment_status TEXT NOT NULL DEFAULT 'unpaid' CHECK (payment_status IN ('unpaid','paid','closed')),
  contact TEXT NOT NULL DEFAULT '',
  qr_code TEXT NOT NULL DEFAULT '',
  precreate_attempted INTEGER NOT NULL DEFAULT 0,
  recharge_input_enc TEXT NOT NULL DEFAULT '',
  recharge_plan_id TEXT NOT NULL DEFAULT '',
  recharge_region TEXT NOT NULL DEFAULT '',
  recharge_email TEXT NOT NULL DEFAULT '',
  attention TEXT NOT NULL DEFAULT '',
  late_payment INTEGER NOT NULL DEFAULT 0,
  last_query_at INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  pay_deadline INTEGER NOT NULL,
  paid_at INTEGER,
  closed_at INTEGER
);
CREATE INDEX idx_orders_user ON orders(user_id);
CREATE INDEX idx_orders_pay ON orders(payment_status, expires_at);

CREATE TABLE card_keys (
  id INTEGER PRIMARY KEY,
  product_id INTEGER NOT NULL REFERENCES products(id),
  code_enc TEXT NOT NULL,
  code_hash TEXT NOT NULL,
  mask TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'available' CHECK (status IN ('available','reserved','sold','void')),
  order_id INTEGER REFERENCES orders(id),
  created_at INTEGER NOT NULL,
  reserved_at INTEGER,
  sold_at INTEGER,
  UNIQUE (product_id, code_hash)
);
CREATE INDEX idx_cards_stock ON card_keys(product_id, status, id);
CREATE INDEX idx_cards_order ON card_keys(order_id);

CREATE TABLE payments (
  id INTEGER PRIMARY KEY,
  order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id),
  out_trade_no TEXT NOT NULL UNIQUE,
  trade_no TEXT NOT NULL UNIQUE,
  amount_cents INTEGER NOT NULL,
  source TEXT NOT NULL,
  late INTEGER NOT NULL DEFAULT 0,
  raw TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);

CREATE TABLE fulfillments (
  id INTEGER PRIMARY KEY,
  order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id),
  type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'none'
    CHECK (status IN ('none','pending','processing','delivered','failed','blocked','manual')),
  reason_code TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at INTEGER NOT NULL DEFAULT 0,
  locked_until INTEGER NOT NULL DEFAULT 0,
  deadline INTEGER NOT NULL DEFAULT 0,
  upstream_attempt INTEGER NOT NULL DEFAULT 1,
  upstream_client_order_id TEXT NOT NULL DEFAULT '',
  upstream_task_id TEXT NOT NULL DEFAULT '',
  upstream_status TEXT NOT NULL DEFAULT '',
  upstream_error_code TEXT NOT NULL DEFAULT '',
  delivered_at INTEGER,
  updated_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_fulfil_status ON fulfillments(status, next_attempt_at);

CREATE TABLE events (
  id INTEGER PRIMARY KEY,
  order_id INTEGER REFERENCES orders(id),
  kind TEXT NOT NULL,
  message TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX idx_events_order ON events(order_id, id);

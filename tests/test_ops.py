"""Composer: admin operations stats, timezone bounds, privacy, indexes."""
import json

import pytest

from app import ops, payments
from app.migrate import migrate
from app.worker import sweep_once

from .conftest import AUTH
from .test_pricing import PLUS, adopt, open_region, product_for, ready
from .test_recharge import buy_recharge, pay
from .test_ui_flow import admin_client


def _pay_only(env, no):
    env.alipay.pay(no)
    assert env.post_notify(env.alipay.notify_form(no)).data == b"success"


def test_lookup_email_case_folds_to_same_buyer(env):
    pid = env.api_product()
    a, _ = buy_recharge(env, pid=pid, auth=AUTH)
    # second order, different casing / spaces
    c = env.app.test_client()
    r = c.post("/product/%d/buy" % pid, data={"quantity": "1", "region": "PH", "auth_json": AUTH,
                                              "lookup_email": "  Buyer@Example.COM ", "_csrf": env.csrf(c)})
    assert r.status_code == 302
    s = ops.buyer_summary(env.core, "BUYER@example.com")
    assert s["orders"] == 2
    assert s["email"] == "buyer@example.com"


def test_lookup_and_recharge_emails_are_separate(env):
    pid = env.api_product()
    auth2 = json.dumps({"accessToken": "AT", "account_id": "acc-2", "user": {"email": "chatgpt-a@x.com"}})
    no1, _ = buy_recharge(env, pid=pid, auth=AUTH)  # recharge buyer@example.com, lookup buyer@example.com
    c = env.app.test_client()
    r = c.post("/product/%d/buy" % pid, data={"quantity": "1", "region": "PH", "auth_json": auth2,
                                              "lookup_email": "payer@other.com", "_csrf": env.csrf(c)})
    assert r.status_code == 302
    buyer = ops.buyer_summary(env.core, "payer@other.com")
    assert buyer["orders"] == 1
    acct = ops.recharge_account_summary(env.core, "chatgpt-a@x.com")
    assert acct["recharge_orders"] == 1
    # must not mix the other identity
    assert ops.buyer_summary(env.core, "chatgpt-a@x.com")["orders"] == 0
    assert ops.recharge_account_summary(env.core, "payer@other.com")["recharge_orders"] == 0


def test_buyer_counts_paid_success_and_amount(env):
    pid = env.api_product()
    no1, _ = buy_recharge(env, pid=pid)
    no2, _ = buy_recharge(env, pid=pid)
    pay(env, no1)
    env.upstream.set_status("%s-1" % no1, "succeeded")
    env.t[0] += 10
    sweep_once(env.core)
    s = ops.buyer_summary(env.core, "buyer@example.com")
    assert s["orders"] == 2
    assert s["paid_orders"] == 1
    assert s["success_orders"] == 1
    assert s["paid_amount_cents"] == env.order(no1)["amount_cents"]
    assert s["paid_amount_cents"] != env.order(no1)["amount_cents"] + env.order(no2)["amount_cents"]


def test_unpaid_orders_excluded_from_revenue(env):
    pid = env.api_product()
    no, _ = buy_recharge(env, pid=pid)
    ov = ops.overview(env.core, "today")
    assert ov["revenue_cents"] == 0
    assert ov["paid_orders"] == 0
    assert ov["new_orders"] >= 1
    pay(env, no)
    ov2 = ops.overview(env.core, "today")
    assert ov2["revenue_cents"] == env.order(no)["amount_cents"]
    assert ov2["paid_orders"] == 1


def test_profit_uses_order_snapshot_not_current_price(env):
    ready(env)
    pid = product_for(env, PLUS)["id"]
    c = env.app.test_client()
    r = c.post("/product/%d/buy" % pid, data={"quantity": "1", "region": "PH", "auth_json": AUTH,
                                              "lookup_email": "buyer@example.com", "_csrf": env.csrf(c)})
    no = env.order_no_from(r.headers["Location"])
    snap_profit = env.order(no)["snap_expected_profit_cny"]
    assert snap_profit is not None
    from app import pricing_service
    vid = env.core.conn.execute(
        "SELECT id FROM plan_variants WHERE plan_id=? AND region_code='PH'", (PLUS,)).fetchone()["id"]
    pricing_service.update_variant(env.core, "t", vid, {"pricing_mode": "manual_retail", "manual_sale_price": "999.00"})
    _pay_only(env, no)
    ov = ops.overview(env.core, "today")
    assert ov["profit_cents"] == snap_profit
    assert ov["profit_cents"] != 99900 - env.order(no)["snap_total_cost_cny"]


def test_missing_snapshot_not_treated_as_zero(env):
    pid = env.api_product()  # legacy flat product: no snap_* 
    no, _ = buy_recharge(env, pid=pid)
    assert env.order(no)["snap_total_cost_cny"] is None
    _pay_only(env, no)
    ov = ops.overview(env.core, "today")
    assert ov["revenue_cents"] == env.order(no)["amount_cents"]
    assert ov["cost_cents"] is None
    assert ov["profit_cents"] is None
    assert ov["cost_orders_with_snapshot"] == 0


def test_today_bounds_use_asia_manila_not_os_tz(env):
    # 2026-11-13 16:00 UTC = 2026-11-14 00:00 Asia/Manila
    env.t[0] = 1_763_049_600  # approx; compute exactly
    from datetime import datetime
    from zoneinfo import ZoneInfo
    manila = ZoneInfo("Asia/Manila")
    start_manila = datetime(2026, 11, 14, 0, 0, 0, tzinfo=manila)
    env.t[0] = int(start_manila.timestamp()) + 3600  # 01:00 Manila on Nov 14
    start, end = ops.period_bounds("today", env.t[0])
    assert start == int(start_manila.timestamp())
    assert end == int(datetime(2026, 11, 15, 0, 0, 0, tzinfo=manila).timestamp())
    # just before midnight Manila still "today"
    env.t[0] = end - 1
    s2, e2 = ops.period_bounds("today", env.t[0])
    assert (s2, e2) == (start, end)
    # next second is next day
    env.t[0] = end
    s3, e3 = ops.period_bounds("today", env.t[0])
    assert s3 == end


def test_created_vs_paid_windows_differ(env):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    manila = ZoneInfo("Asia/Manila")
    day1 = datetime(2026, 11, 10, 12, 0, 0, tzinfo=manila)
    day2 = datetime(2026, 11, 11, 12, 0, 0, tzinfo=manila)
    env.t[0] = int(day1.timestamp())
    pid = env.api_product()
    no, _ = buy_recharge(env, pid=pid)
    # pay on next calendar day (Manila)
    env.t[0] = int(day2.timestamp())
    _pay_only(env, no)
    env.t[0] = int(day2.timestamp()) + 10
    created_day = ops.overview(env.core, date_from="2026-11-10", date_to="2026-11-10")
    paid_day = ops.overview(env.core, date_from="2026-11-11", date_to="2026-11-11")
    assert created_day["new_orders"] == 1 and created_day["revenue_cents"] == 0
    assert paid_day["new_orders"] == 0 and paid_day["revenue_cents"] == env.order(no)["amount_cents"]
    assert paid_day["paid_orders"] == 1


def test_filters_plan_region_status_email_and_pagination(env):
    ready(env)
    pid = product_for(env, PLUS)["id"]
    c = env.app.test_client()
    nos = []
    for i in range(3):
        r = c.post("/product/%d/buy" % pid, data={"quantity": "1", "region": "PH", "auth_json": AUTH,
                                                  "lookup_email": "buyer@example.com", "_csrf": env.csrf(c)})
        nos.append(env.order_no_from(r.headers["Location"]))
        env.t[0] += 1
    # US region order
    r = c.post("/product/%d/buy" % pid, data={"quantity": "1", "region": "US", "auth_json": AUTH,
                                              "lookup_email": "other@example.com", "_csrf": env.csrf(c)})
    us_no = env.order_no_from(r.headers["Location"])
    page1 = ops.search_orders(env.core, region="PH", page=1, page_size=2)
    assert page1["total"] == 3 and len(page1["rows"]) == 2
    page2 = ops.search_orders(env.core, region="PH", page=2, page_size=2)
    assert len(page2["rows"]) == 1
    ids = {r["id"] for r in page1["rows"]} | {r["id"] for r in page2["rows"]}
    assert len(ids) == 3
    us = ops.search_orders(env.core, region="US")
    assert us["total"] == 1 and us["rows"][0]["order_no"] == us_no
    by_email = ops.search_orders(env.core, lookup_email="OTHER@example.com")
    assert by_email["total"] == 1
    unpaid = ops.search_orders(env.core, status="unpaid", plan_id=PLUS)
    assert unpaid["total"] >= 4


def test_duplicate_payment_notify_does_not_duplicate_timeline_or_revenue(env):
    no, _ = buy_recharge(env)
    env.alipay.pay(no)
    form = env.alipay.notify_form(no)
    assert env.post_notify(form).data == b"success"
    assert env.post_notify(form).data == b"success"
    assert env.post_notify(form).data == b"success"
    tl = ops.order_timeline(env.core, env.order(no)["id"])
    paid_nodes = [n for n in tl["nodes"] if n["step"] == "paid"]
    assert len(paid_nodes) == 1
    ov = ops.overview(env.core, "today")
    assert ov["paid_orders"] == 1
    assert ov["revenue_cents"] == env.order(no)["amount_cents"]


def test_non_admin_cannot_access_ops_pages(env):
    env.make_user("user@example.com", admin=False)
    c = env.app.test_client()
    env.login(c, "user@example.com")
    for path in ("/admin/", "/admin/orders", "/admin/orders/1"):
        r = c.get(path)
        assert r.status_code == 403
    # anonymous redirected to login
    c2 = env.app.test_client()
    assert c2.get("/admin/orders").status_code in (302, 401)


def test_public_pages_still_mask_emails(env):
    no, client = buy_recharge(env)
    # follow pay location which includes token
    # buy_recharge returns client after buy; get pay page via order token from Location is already done
    o = env.order(no)
    # public lookup
    r = client.post("/lookup", data={"order_no": no, "email": "buyer@example.com", "_csrf": env.csrf(client)},
                    follow_redirects=True)
    body = r.data.decode()
    assert "buyer@example.com" not in body or "b•••••@example.com" in body or "•••••" in body
    # status JSON
    # need guest token — recreate via API
    c = env.app.test_client()
    resp = c.post("/api/checkout/order",
                  data=json.dumps({"product_id": env.api_product(), "region": "PH", "session": AUTH,
                                   "contact_email": "buyer@example.com", "confirmed": True}),
                  content_type="application/json", headers={"X-CSRF-Token": env.csrf(c)})
    data = resp.get_json()
    st = c.get("/api/orders/%s/status?t=%s" % (data["order_no"], data["token"])).get_json()
    assert "buyer@example.com" not in json.dumps(st)
    assert "•••••" in st.get("account_masked", "")


def test_ops_indexes_migration_and_data_intact(env):
    pid = env.api_product()
    no, _ = buy_recharge(env, pid=pid)
    before = dict(env.order(no))
    assert migrate(env.db_path, verbose=False) == []  # already applied by fixture
    names = ops.index_names(env.core.conn)
    for need in ("idx_orders_created_at", "idx_orders_paid_at", "idx_orders_lookup_email_created",
                 "idx_orders_recharge_email_created", "idx_fulfillments_status_updated"):
        assert need in names, need
    after = dict(env.order(no))
    assert after["order_no"] == before["order_no"] and after["amount_cents"] == before["amount_cents"]
    migs = [r[0] for r in env.core.conn.execute("SELECT name FROM schema_migrations ORDER BY name")]
    assert "005_ops_indexes.sql" in migs


def test_admin_order_detail_shows_full_email_and_summaries(env):
    no, _ = buy_recharge(env)
    oid = env.order(no)["id"]
    c, tok = admin_client(env)
    page = c.get("/admin/orders/%d" % oid).data.decode()
    assert "buyer@example.com" in page
    assert "联系邮箱摘要" in page and "充值账号摘要" in page and "订单时间线" in page
    assert "Asia/Manila" in page

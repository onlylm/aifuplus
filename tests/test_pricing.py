import json

import pytest

from app import orders, pricing, pricing_service, worker
from app.fx import FxError

from .conftest import AUTH
from .test_ui_flow import admin_client, visible

PLUS, PRO5, PRO20, MAX = "plus-monthly", "pro5x-monthly", "pro20x-direct-monthly", "promax-monthly"


def sync(env):
    return pricing_service.run_cycle(env.core, force=True)


def var(env, plan, region):
    return dict(env.core.conn.execute("SELECT * FROM plan_variants WHERE plan_id=? AND region_code=?", (plan, region)).fetchone())


def adopt(env, plan, region, **extra):
    """Admin picks the upstream reference cost for a region (and optionally sets markup etc.)."""
    v = var(env, plan, region)
    fields = {"cost_source": "upstream"}
    fields.update(extra)
    pricing_service.update_variant(env.core, "tester", v["id"], fields)
    return var(env, plan, region)


def open_region(env, plan, region):
    pricing_service.toggle_variant(env.core, "tester", var(env, plan, region)["id"], True)


def product_for(env, plan):
    return env.core.conn.execute("SELECT * FROM products WHERE plan_id=?", (plan,)).fetchone()


def ready(env):
    """Synced, Plus/PH priced from the upstream cost (16 USD x 7.2 x 1.2 = 138.24) and on sale, Plus/US on sale with a manual cost."""
    sync(env)
    adopt(env, PLUS, "PH")
    open_region(env, PLUS, "PH")
    adopt(env, PLUS, "US", cost_source="manual", manual_package_cost="100")
    open_region(env, PLUS, "US")


# ---------------------------------------------------------------- 1-4 sync
def test_only_the_four_gpt_plans_are_synced(env):
    sync(env)
    plans = {r[0] for r in env.core.conn.execute("SELECT DISTINCT plan_id FROM plan_variants")}
    assert plans == {PLUS, PRO5, PRO20, MAX}
    prods = {r[0] for r in env.core.conn.execute("SELECT plan_id FROM products WHERE type='api_recharge'")}
    assert prods == {PLUS, PRO5, PRO20, MAX}
    for banned in ("go-monthly", "credits", "team-subscription", "subscription-upgrade", "grok-xpass-monthly"):
        assert banned not in plans


def test_regions_come_from_plan_regions(env):
    sync(env)
    got = lambda plan: {r[0] for r in env.core.conn.execute("SELECT region_code FROM plan_variants WHERE plan_id=?", (plan,))}
    assert got(PLUS) == {"PH", "US", "JP"} and got(PRO5) == {"EG", "JP", "CL"} and got(PRO20) == {"PH", "SG"} and got(MAX) == {"PH", "US"}
    assert var(env, PRO5, "CL")["region_label"] == "智利" and var(env, PLUS, "JP")["region_label"] == "日本"


def test_regions_given_as_objects_are_supported(env):
    cfg = env.upstream.config_payload
    cfg["planRegions"]["plus-monthly"] = [{"code": "PH", "label": "菲律宾"}, {"code": "US", "label": "美区"}, {"regionCode": "jp"}, {"code": "ZZ", "label": "某地"}, {"x": 1}, 7]
    sync(env)
    plus = {r["region_code"]: r for r in env.core.conn.execute("SELECT * FROM plan_variants WHERE plan_id=?", (PLUS,))}
    assert set(plus) == {"PH", "US", "JP", "ZZ"}
    assert plus["PH"]["upstream_cost_usd_cents"] == 1600 and plus["US"]["upstream_cost_usd_cents"] == 2035 and plus["JP"]["upstream_cost_usd_cents"] is None
    assert plus["US"]["region_label"] == "美国" and plus["ZZ"]["region_label"] == "某地"        # our own Chinese name wins; unknown codes use the upstream label


def test_upstream_usd_cost_is_stored_in_cents(env):
    sync(env)
    assert var(env, PLUS, "PH")["upstream_cost_usd_cents"] == 1600
    assert var(env, PLUS, "US")["upstream_cost_usd_cents"] == 2035
    assert var(env, PRO5, "EG")["upstream_cost_usd_cents"] == 9000 and var(env, PRO20, "PH")["upstream_cost_usd_cents"] == 15000
    assert var(env, PLUS, "PH")["upstream_synced_at"] == env.t[0]


def test_regional_display_price_is_never_used_as_cost(env):
    sync(env)
    for r in env.core.conn.execute("SELECT * FROM plan_variants"):
        assert r["upstream_cost_usd_cents"] in (None, 1600, 2035, 9000, 15000)      # 99999/88888/77777 decoys never appear
    assert var(env, PLUS, "PH")["sale_price_cny"] is None                          # nothing priced until the admin adopts a cost


# ---------------------------------------------------------------- 4-6 null / manual
def test_null_cost_is_not_zero(env):
    sync(env)
    for plan, region in ((PLUS, "JP"), (PRO5, "JP"), (PRO20, "SG"), (MAX, "PH"), (MAX, "US")):
        v = var(env, plan, region)
        assert v["upstream_cost_usd_cents"] is None and v["upstream_latest_null"] == 1
        assert v["sale_price_cny"] is None and v["effective_package_cost_cny"] is None
    assert env.core.conn.execute("SELECT COUNT(*) FROM plan_variants WHERE upstream_cost_usd_cents=0").fetchone()[0] == 0


def test_null_keeps_the_last_valid_cost(env):
    ready(env)
    before = var(env, PLUS, "PH")
    env.upstream.config_payload["wallet"]["prices"]["plus-monthly"]["PH"] = None
    env.t[0] += 3600
    sync(env)
    after = var(env, PLUS, "PH")
    assert after["upstream_cost_usd_cents"] == 1600 and after["upstream_latest_null"] == 1
    assert after["sale_price_cny"] == before["sale_price_cny"] and after["enabled"] == 1
    page = admin_client(env)[0].get("/admin/products").data.decode()
    assert "沿用旧值" in visible(page)


def test_variant_without_any_cost_cannot_be_put_on_sale(env):
    sync(env)
    with pytest.raises(pricing_service.PricingError) as e:
        open_region(env, MAX, "PH")
    assert "还没有可用成本" in str(e.value)
    v = var(env, MAX, "PH")
    assert v["enabled"] == 0
    page = admin_client(env)[0].get("/admin/products").data.decode()
    assert "上游未提供成本，需要人工填写" in visible(page)


def test_manual_cost_overrides_upstream_reference(env):
    sync(env)
    v = adopt(env, PLUS, "PH")                                       # adopt upstream: 16 x 7.2 = 115.20
    assert v["effective_package_cost_cny"] == 11520
    v = adopt(env, PLUS, "PH", cost_source="manual", manual_package_cost="99.99")
    assert v["effective_package_cost_cny"] == 9999 and v["total_cost_cny"] == 9999
    env.upstream.config_payload["wallet"]["prices"]["plus-monthly"]["PH"] = 17.0
    sync(env)
    assert var(env, PLUS, "PH")["effective_package_cost_cny"] == 9999     # upstream moves, manual still wins


def test_upstream_cost_is_not_adopted_automatically(env):
    sync(env)
    v = var(env, PLUS, "PH")
    assert v["cost_source"] == "manual" and v["sale_price_cny"] is None and v["upstream_cost_usd_cents"] == 1600
    assert "采用上游参考成本" in admin_client(env)[0].get("/admin/products").data.decode()


# ---------------------------------------------------------------- 7-11 formulas
def test_usd_to_cny_conversion():
    assert pricing.usd_cents_to_cny_fen(1600, 7_200_000) == 11520
    assert pricing.usd_cents_to_cny_fen(1600, 6_671_900) == 10675          # 10675.04 -> half-up
    assert pricing.usd_cents_to_cny_fen(1, 7_245_000) == 7 and pricing.usd_cents_to_cny_fen(1, 7_500_000) == 8
    assert pricing.with_safety(7_200_000, 100) == 7_272_000


def test_percentage_markup_is_on_cost_not_margin():
    assert pricing.compute_price(11520, "percentage", 2000, 0, "cent") == 13824          # cost x 1.2
    assert pricing.compute_price(10000, "percentage", 0, 0, "cent") == 10000
    assert pricing.compute_price(10001, "percentage", 1250, 0, "cent") == 11251           # half-up on fen


def test_fixed_profit():
    assert pricing.compute_price(11520, "fixed", 9999, 5000, "cent") == 16520             # markup ignored in fixed mode


def test_rounding_rules():
    assert pricing.apply_rounding(13824, "cent") == 13824
    assert pricing.apply_rounding(13824, "yuan_up") == 13900 and pricing.apply_rounding(13800, "yuan_up") == 13800
    assert pricing.apply_rounding(13824, "tail_90") == 13890 and pricing.apply_rounding(13895, "tail_90") == 13990
    assert pricing.apply_rounding(13824, "tail_99") == 13899 and pricing.apply_rounding(13899, "tail_99") == 13899
    assert pricing.apply_rounding(13900, "tail_90") == 13990 and pricing.apply_rounding(13890, "tail_90") == 13890
    assert pricing.compute_price(11520, "percentage", 2000, 0, "tail_99") == 13899


def test_other_cost_is_part_of_total_cost(env):
    sync(env)
    v = adopt(env, PLUS, "PH", other_cost="5")
    assert v["total_cost_cny"] == 11520 + 500 and v["sale_price_cny"] == 14424            # (115.20 + 5) x 1.2
    v = adopt(env, PLUS, "PH", pricing_mode="fixed", fixed_profit="10", rounding="yuan_up")
    assert v["sale_price_cny"] == 13100                                                     # 120.20 + 10.00 = 130.20 -> up to 131


# ---------------------------------------------------------------- 12-15 on/off
def test_admin_can_close_one_region_only(env):
    ready(env)
    c, tok = admin_client(env)
    vid = var(env, PLUS, "US")["id"]
    r = c.post("/admin/variants/%d/toggle" % vid, data={"_csrf": tok, "enabled": "0"}, follow_redirects=True).data.decode()
    assert "关闭后，新用户将无法选择该地区，历史订单不受影响。" in r
    assert var(env, PLUS, "US")["enabled"] == 0 and var(env, PLUS, "PH")["enabled"] == 1


def test_closed_region_is_not_shown_on_storefront(env):
    ready(env)
    pid = product_for(env, PLUS)["id"]
    c = env.app.test_client()
    page = c.get("/product/%d" % pid).data.decode()
    assert "菲律宾" in page and "美国" in page
    pricing_service.toggle_variant(env.core, "t", var(env, PLUS, "US")["id"], False)
    page = c.get("/product/%d" % pid).data.decode()
    assert "菲律宾" in page and "美国" not in page and "日本" not in page
    home = c.get("/").data.decode()
    assert "菲律宾" in home and "美国" not in home
    assert "<h3>ChatGPT Pro 5x</h3>" not in home and "<h3>ChatGPT Pro Max</h3>" not in home     # nothing on sale for those plans


def test_forged_request_cannot_buy_a_closed_region(env):
    ready(env)
    pricing_service.toggle_variant(env.core, "t", var(env, PLUS, "US")["id"], False)
    pid = product_for(env, PLUS)["id"]
    c = env.app.test_client()
    tok = env.csrf(c)
    for url in ("confirm", "buy"):
        r = c.post("/product/%d/%s" % (pid, url), data={"quantity": "1", "region": "US", "auth_json": AUTH, "lookup_email": "b@example.com",
                                                        "amount": "0.01", "price": "0.01", "_csrf": tok})
        assert r.status_code == 200 and "暂时不能购买" in r.data.decode()
    for region in ("JP", "XX", ""):                                                 # never opened / unknown / empty
        r = c.post("/product/%d/buy" % pid, data={"quantity": "1", "region": region, "auth_json": AUTH, "lookup_email": "b@example.com", "_csrf": tok})
        assert r.status_code == 200
    assert env.core.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    with pytest.raises(orders.OrderError):                                           # also blocked below the web layer
        orders.create_order(env.core, pid, 1, region="US", recharge_auth_text=AUTH, lookup_email="b@example.com")


def test_plan_with_all_regions_closed_is_hidden(env):
    ready(env)
    pid = product_for(env, PLUS)["id"]
    c = env.app.test_client()
    assert "<h3>ChatGPT Plus</h3>" in c.get("/").data.decode()
    for region in ("PH", "US"):
        pricing_service.toggle_variant(env.core, "t", var(env, PLUS, region)["id"], False)
    assert "<h3>ChatGPT Plus</h3>" not in c.get("/").data.decode()
    assert c.get("/product/%d" % pid).status_code == 404


def test_bulk_enable_disable_and_planwide_settings(env):
    sync(env)
    c, tok = admin_client(env)
    ids = [var(env, PRO5, r)["id"] for r in ("EG", "CL")]
    url = "/admin/plans/%s/bulk" % PRO5
    r = c.post(url, data={"_csrf": tok, "action": "enable", "variant_ids": ids}, follow_redirects=True).data.decode()
    assert "还没有成本，已跳过" in r and var(env, PRO5, "EG")["enabled"] == 0                 # no cost yet: nothing can be enabled
    for region in ("EG", "CL"):
        adopt(env, PRO5, region)
    c.post(url, data={"_csrf": tok, "action": "enable", "variant_ids": ids})
    assert var(env, PRO5, "EG")["enabled"] == 1 and var(env, PRO5, "CL")["enabled"] == 1 and var(env, PRO5, "JP")["enabled"] == 0
    c.post(url, data={"_csrf": tok, "action": "markup", "value": "30", "rounding": "yuan_up"})
    assert var(env, PRO5, "EG")["markup_basis_points"] == 3000 and var(env, PRO5, "JP")["markup_basis_points"] == 3000
    assert var(env, PRO5, "EG")["sale_price_cny"] == pricing.compute_price(64800, "percentage", 3000, 0, "yuan_up")
    c.post(url, data={"_csrf": tok, "action": "profit", "value": "20", "variant_ids": ids})
    assert var(env, PRO5, "EG")["pricing_mode"] == "fixed" and var(env, PRO5, "JP")["pricing_mode"] == "percentage"
    c.post(url, data={"_csrf": tok, "action": "other_cost", "value": "8"})
    assert var(env, PRO5, "EG")["other_cost_cny"] == 800 and var(env, PRO5, "EG")["total_cost_cny"] == 64800 + 800
    c.post(url, data={"_csrf": tok, "action": "disable", "variant_ids": ids})
    assert var(env, PRO5, "EG")["enabled"] == 0 and var(env, PRO5, "CL")["enabled"] == 0


# ---------------------------------------------------------------- 16-17 guards
def test_cost_jump_waits_for_confirmation(env):
    ready(env)
    old_price = var(env, PLUS, "PH")["sale_price_cny"]
    env.upstream.config_payload["wallet"]["prices"]["plus-monthly"]["PH"] = 20.0          # +25% > 10%
    env.t[0] += 1800
    sync(env)
    v = var(env, PLUS, "PH")
    assert v["sale_price_cny"] == old_price and v["upstream_cost_usd_cents"] == 1600 and v["pending_upstream_cost_usd_cents"] == 2000
    assert "成本异常" in v["anomaly"] and "25.0%" in v["anomaly_detail"] and "$16.00" in v["anomaly_detail"] and "$20.00" in v["anomaly_detail"]
    assert v["enabled"] == 1                                                                 # keeps selling at the old price
    c, tok = admin_client(env)
    page = c.get("/admin/products").data.decode()
    assert "成本异常，等待确认" in page and "确认异常价格" in page
    c.post("/admin/variants/%d/confirm" % v["id"], data={"_csrf": tok})
    v = var(env, PLUS, "PH")
    assert v["upstream_cost_usd_cents"] == 2000 and v["anomaly"] == "" and v["sale_price_cny"] == pricing.compute_price(14400, "percentage", 2000, 0, "cent")


def test_small_cost_change_is_applied_automatically(env):
    ready(env)
    env.upstream.config_payload["wallet"]["prices"]["plus-monthly"]["PH"] = 16.5            # +3.1%
    sync(env)
    v = var(env, PLUS, "PH")
    assert v["upstream_cost_usd_cents"] == 1650 and v["anomaly"] == "" and v["sale_price_cny"] == pricing.compute_price(11880, "percentage", 2000, 0, "cent")


def test_fx_jump_waits_for_confirmation_and_old_rate_stays(env):
    ready(env)
    price = var(env, PLUS, "PH")["sale_price_cny"]
    env.fx_rate[0] = 8_200_000                                                               # +13.9% > 10%
    pricing_service.sync_fx(env.core)
    s = pricing_service.get_settings(env.core.conn)
    assert s["fx_auto_micro"] == 7_200_000 and s["fx_pending_micro"] == 8_200_000 and "等待确认" in s["fx_pending_detail"]
    assert var(env, PLUS, "PH")["sale_price_cny"] == price
    c, tok = admin_client(env)
    assert "汇率异常，等待确认" in c.get("/admin/products").data.decode()
    c.post("/admin/pricing/confirm-fx", data={"_csrf": tok})
    assert pricing_service.get_settings(env.core.conn)["fx_auto_micro"] == 8_200_000
    assert var(env, PLUS, "PH")["sale_price_cny"] == pricing.compute_price(pricing.usd_cents_to_cny_fen(1600, 8_200_000), "percentage", 2000, 0, "cent")


def test_fx_failure_keeps_old_rate_and_never_zero(env):
    pricing_service.sync_fx(env.core)
    s = pricing_service.get_settings(env.core.conn)
    assert s["fx_auto_micro"] == 7_200_000 and s["fx_auto_source"] == "测试汇率源"
    env.fx_fail[0] = True
    ok, msg = pricing_service.sync_fx(env.core)
    s = pricing_service.get_settings(env.core.conn)
    assert ok is False and s["fx_auto_micro"] == 7_200_000 and "继续使用上一次的汇率" in s["fx_last_error"]
    env.fx_rate[0] = 0
    env.fx_fail[0] = False
    ok, _ = pricing_service.sync_fx(env.core)
    assert ok is False and pricing_service.get_settings(env.core.conn)["fx_auto_micro"] == 7_200_000


def test_fx_provider_rejects_nonsense_rates():
    from app import fx
    with pytest.raises(FxError):
        fx.fetch_rate([("bad", lambda: 0), ("worse", lambda: 99_000_000)])
    assert fx.fetch_rate([("bad", lambda: 0), ("ok", lambda: 6_800_000)]) == (6_800_000, "ok")


def test_manual_fx_override_and_safety_factor(env):
    ready(env)
    c, tok = admin_client(env)
    c.post("/admin/pricing/settings", data={"_csrf": tok, "fx_mode": "auto", "safety": "1", "anomaly": "10", "default_markup": "20"})
    fxv = pricing_service.fx_view(env.core)
    assert fxv["effective"] == 7_272_000 and fxv["manual_on"] is False
    c.post("/admin/pricing/settings", data={"_csrf": tok, "fx_mode": "manual", "fx_manual": "7.5", "safety": "1", "anomaly": "10", "default_markup": "20"})
    fxv = pricing_service.fx_view(env.core)
    assert fxv["effective"] == 7_500_000 and fxv["manual_on"] is True and fxv["auto"] == 7_200_000
    assert var(env, PLUS, "PH")["sale_price_cny"] == pricing.compute_price(12000, "percentage", 2000, 0, "cent")
    page = c.get("/admin/products").data.decode()
    assert "人工覆盖中" in page
    r = c.post("/admin/pricing/settings", data={"_csrf": tok, "fx_mode": "manual", "fx_manual": "abc"}, follow_redirects=True).data.decode()
    assert "请填写合理的人工汇率" in r


def test_upstream_failures_keep_old_costs(env):
    ready(env)
    price = var(env, PLUS, "PH")["sale_price_cny"]
    env.upstream.config_error = 503
    env.t[0] += 1800
    ok, msg, _ = pricing_service.sync_costs(env.core)
    assert ok is False and "已保留上一次的成本" in msg
    assert var(env, PLUS, "PH")["sale_price_cny"] == price and var(env, PLUS, "PH")["upstream_cost_usd_cents"] == 1600
    env.upstream.config_error = 0
    env.upstream.config_payload = {"success": True, "weird": 1}
    ok, msg, _ = pricing_service.sync_costs(env.core)
    assert ok is False and var(env, PLUS, "PH")["upstream_cost_usd_cents"] == 1600


def test_staleness_warning_levels(env):
    sync(env)
    assert pricing_service.staleness(env.core)["upstream"]["level"] == "ok"
    env.t[0] += 2 * 3600 + 60
    assert pricing_service.staleness(env.core)["upstream"]["level"] == "warn" and pricing_service.staleness(env.core)["fx"]["level"] == "warn"
    c, _ = admin_client(env)
    assert 'class="bar warn"' in c.get("/admin/products").data.decode()
    env.t[0] += 4 * 3600
    assert pricing_service.staleness(env.core)["upstream"]["level"] == "err"
    assert 'class="bar err"' in c.get("/admin/").data.decode()


# ---------------------------------------------------------------- 18-19 snapshot
def buy_variant(env, region="PH", client=None):
    pid = product_for(env, PLUS)["id"]
    c = client or env.app.test_client()
    r = c.post("/product/%d/buy" % pid, data={"quantity": "1", "region": region, "auth_json": AUTH, "lookup_email": "buyer@example.com",
                                              "_csrf": env.csrf(c)})
    assert r.status_code == 302, r.data.decode()
    return env.order_no_from(r.headers["Location"]), r.headers["Location"], c


def test_order_stores_price_and_cost_snapshot(env):
    ready(env)
    adopt(env, PLUS, "PH", other_cost="5")
    no, _, _ = buy_variant(env, "PH")
    o = env.order(no)
    v = var(env, PLUS, "PH")
    assert o["amount_cents"] == o["unit_price_cents"] == v["sale_price_cny"] == 14424
    assert (o["snap_plan_label"], o["snap_region_label"], o["recharge_plan_id"], o["recharge_region"]) == ("ChatGPT Plus", "菲律宾", PLUS, "PH")
    assert (o["snap_package_cost_cny"], o["snap_other_cost_cny"], o["snap_total_cost_cny"]) == (11520, 500, 12020)
    assert (o["snap_fx_rate"], o["snap_pricing_mode"], o["snap_cost_source"], o["snap_expected_profit_cny"]) == (7_200_000, "percentage", "upstream", 14424 - 12020)
    assert o["variant_id"] == v["id"]
    c, _ = admin_client(env)
    detail = visible(c.get("/admin/orders/%d" % o["id"]).data.decode())
    assert "下单时的价格与成本" in detail and "预计利润" in detail and "¥144.24" in detail and "¥120.20" in detail


def test_repricing_never_touches_existing_orders(env):
    ready(env)
    no, loc, c = buy_variant(env, "PH")
    before = dict(env.order(no))
    assert before["amount_cents"] == 13824
    env.fx_rate[0] = 7_500_000                                       # +4.2%: applied
    pricing_service.sync_fx(env.core)
    adopt(env, PLUS, "PH", markup_percent="50", other_cost="20")
    new_price = var(env, PLUS, "PH")["sale_price_cny"]
    assert new_price != 13824
    after = dict(env.order(no))
    for k in ("amount_cents", "unit_price_cents", "snap_package_cost_cny", "snap_other_cost_cny", "snap_total_cost_cny", "snap_fx_rate",
              "snap_pricing_mode", "snap_expected_profit_cny", "variant_id"):
        assert after[k] == before[k], k
    assert "138.24" in c.get(loc).data.decode()                      # the buyer still pays what they were shown
    no2, _, _ = buy_variant(env, "PH")
    assert env.order(no2)["amount_cents"] == new_price
    pricing_service.toggle_variant(env.core, "t", var(env, PLUS, "PH")["id"], False)     # closing a region keeps the old order intact
    assert env.order(no)["amount_cents"] == 13824 and env.order(no)["payment_status"] == "unpaid"


def test_price_is_decided_by_the_server_only(env):
    ready(env)
    pid = product_for(env, PLUS)["id"]
    c = env.app.test_client()
    tok = env.csrf(c)
    r = c.post("/product/%d/buy" % pid, data={"quantity": "3", "region": "PH", "auth_json": AUTH, "lookup_email": "b@example.com",
                                              "amount": "0.01", "amount_cents": "1", "price": "0.01", "unit_price_cents": "1", "_csrf": tok})
    assert r.status_code == 200                                      # quantity 3 is rejected for a recharge product
    r = c.post("/product/%d/buy" % pid, data={"quantity": "1", "region": "ph", "auth_json": AUTH, "lookup_email": "b@example.com",
                                              "amount": "0.01", "amount_cents": "1", "price": "0.01", "_csrf": tok})
    assert r.status_code == 302 and env.order(env.order_no_from(r.headers["Location"]))["amount_cents"] == 13824


# ---------------------------------------------------------------- 20 guest flow end to end
def test_guest_buys_a_variant_without_login_and_gets_recharged(env):
    ready(env)
    pid = product_for(env, PLUS)["id"]
    c = env.app.test_client()
    page = c.get("/product/%d" % pid).data.decode()
    assert "充值凭证" in page and "authSessionJson" not in visible(page) and "plus-monthly" not in visible(page)
    assert "¥138.24" in page and "¥120.00" in page                          # PH from upstream cost, US from the manual cost
    r = c.post("/product/%d/confirm" % pid, data={"quantity": "1", "region": "PH", "auth_json": AUTH, "lookup_email": "buyer@example.com", "_csrf": env.csrf(c)})
    body = r.data.decode()
    assert r.status_code == 200 and "确认并去付款" in body and "138.24" in body and "菲律宾" in body
    assert env.core.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    no, loc, _ = buy_variant(env, "PH", c)
    assert "/pay/" in loc and [r for r in env.upstream.requests if r[0] == "POST"] == []    # no recharge is submitted before payment
    env.alipay.pay(no)
    env.post_notify(env.alipay.notify_form(no))
    sent = env.upstream.tasks["%s-1" % no]["body"]
    assert sent["planId"] == PLUS and sent["checkoutCountry"] == "PH" and sent["paymentSource"] == "auto_card"
    assert env.alipay.trades[no]["amount"] == "138.24"
    with c.session_transaction() as s:
        assert "uid" not in s


def test_legacy_flat_price_product_still_works(env):
    pid = env.api_product()                                                      # a pre-existing product with no variants
    order_no, _ = orders.create_order(env.core, pid, 1, region="PH", recharge_auth_text=AUTH, lookup_email="a@example.com")
    assert env.order(order_no)["amount_cents"] == 5000 and env.order(order_no)["variant_id"] is None


# ---------------------------------------------------------------- scheduling / audit / admin UI
def test_scheduler_runs_once_per_half_hour_and_never_overlaps(env):
    assert pricing_service.run_cycle(env.core) is not None                          # first run happens immediately
    assert pricing_service.run_cycle(env.core) is None                              # not due yet
    env.t[0] += 29 * 60
    assert pricing_service.run_cycle(env.core) is None
    env.t[0] += 2 * 60
    assert pricing_service.run_cycle(env.core) is not None                          # 30 min later
    assert pricing_service.run_cycle(env.core, force=True) is not None              # manual "sync now" ignores the schedule
    env.core.conn.execute("UPDATE pricing_settings SET sync_lock_until=?", (env.t[0] + 500,))
    assert pricing_service.run_cycle(env.core, force=True) is None                  # another process holds the lock
    calls = []
    orig = pricing_service.run_cycle
    pricing_service.run_cycle = lambda core, force=False, actor="system": calls.append(force)
    try:
        worker.pricing_tick(env.core, force=True)
        worker.pricing_tick(env.core)
    finally:
        pricing_service.run_cycle = orig
    assert calls == [True, False]


def test_web_processes_do_not_schedule_price_sync():
    import inspect
    from app import factory
    assert "pricing" not in inspect.getsource(worker.start_thread) and "pricing" not in inspect.getsource(factory)
    assert "pricing_tick" in inspect.getsource(worker)


def test_every_sync_and_price_change_is_audited(env):
    ready(env)
    kinds = {r[0] for r in env.core.conn.execute("SELECT DISTINCT kind FROM pricing_audit")}
    assert {"sync_ok", "fx_sync", "cost_sync", "price_change", "toggle", "variant_update", "cost_null", "product"} <= kinds
    env.fx_fail[0] = True
    pricing_service.sync_fx(env.core)
    env.upstream.config_error = 500
    pricing_service.sync_costs(env.core)
    kinds = {r[0] for r in env.core.conn.execute("SELECT DISTINCT kind FROM pricing_audit")}
    assert {"fx_failed", "sync_failed"} <= kinds
    row = env.core.conn.execute("SELECT * FROM pricing_audit WHERE kind='price_change' ORDER BY id LIMIT 1").fetchone()
    assert "→" in row["message"] and row["actor"] in ("tester", "system")


def test_admin_pricing_page_is_plain_language(env):
    ready(env)
    c, tok = admin_client(env)
    page = c.get("/admin/products").data.decode()
    v = visible(page)                                                              # collapsed 技术详情 blocks are stripped out
    for need in ("ChatGPT Plus", "ChatGPT Pro 5x", "ChatGPT Pro 20x", "ChatGPT Pro Max", "已上架地区", "最低售价", "最近成本同步", "当前定价策略",
                 "自动参考汇率", "实际计价汇率", "立即同步", "重新计算售价"):
        assert need in v, need
    for need in ("展开地区管理", "上游参考成本", "套餐人民币成本", "其他成本", "总成本", "加价方式", "成本来源", "同步状态", "预计利润",
                 "批量上架", "批量关闭", "统一百分比加价", "统一固定利润", "批量设置其他成本", "上架销售", "关闭销售",
                 "关闭后，新用户将无法选择该地区，历史订单不受影响。"):
        assert need in page, need
    for bad in ("plus-monthly", "pro5x-monthly", "planId", "paymentSource", "wallet", "regionalDisplayPrices", "planRegions", "JSON"):
        assert bad not in v, bad
    assert "plus-monthly" in page                                                 # present only inside the collapsed 技术详情


def test_migration_003_is_incremental_and_idempotent(env):
    from app.migrate import migrate
    assert migrate(env.db_path, verbose=False) == []
    names = [r[0] for r in env.core.conn.execute("SELECT name FROM schema_migrations ORDER BY name")]
    assert names == ["001_init.sql", "002_site_and_lookup.sql", "003_pricing_variants.sql"]

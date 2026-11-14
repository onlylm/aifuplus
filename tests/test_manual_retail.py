"""Composer: direct retail price mode + multi-region batch set."""
import json

import pytest

from app import orders, pricing, pricing_service
from app.migrate import migrate

from .conftest import AUTH
from .test_pricing import PLUS, PRO5, adopt, open_region, product_for, ready, sync, var
from .test_ui_flow import admin_client


def test_single_region_manual_retail_exact_fen(env):
    sync(env)
    adopt(env, PLUS, "PH")
    v = var(env, PLUS, "PH")
    pricing_service.update_variant(env.core, "admin", v["id"], {
        "pricing_mode": "manual_retail", "manual_sale_price": "128.88"})
    v = var(env, PLUS, "PH")
    assert v["pricing_mode"] == "manual_retail"
    assert v["manual_sale_price_cny"] == 12888
    assert v["sale_price_cny"] == 12888  # no secondary rounding


def test_fx_and_cost_sync_do_not_overwrite_manual_retail(env):
    sync(env)
    adopt(env, PLUS, "PH")
    pricing_service.update_variant(env.core, "admin", var(env, PLUS, "PH")["id"], {
        "pricing_mode": "manual_retail", "manual_sale_price": "199.00"})
    before = var(env, PLUS, "PH")
    assert before["sale_price_cny"] == 19900
    old_cost = before["total_cost_cny"]

    env.fx_rate[0] = 7_500_000  # ~4.2% — below default anomaly threshold, applied automatically
    pricing_service.sync_fx(env.core)
    env.upstream.config_payload["wallet"]["prices"]["plus-monthly"]["PH"] = 18.0
    pricing_service.sync_costs(env.core)

    after = var(env, PLUS, "PH")
    assert after["sale_price_cny"] == 19900
    assert after["manual_sale_price_cny"] == 19900
    assert after["pricing_mode"] == "manual_retail"
    assert after["total_cost_cny"] != old_cost  # cost updated
    assert after["total_cost_cny"] == after["effective_package_cost_cny"] + after["other_cost_cny"]
    assert after["sale_price_cny"] - after["total_cost_cny"] == after["sale_price_cny"] - after["total_cost_cny"]


def test_cost_change_updates_profit_preview(env):
    sync(env)
    adopt(env, PLUS, "PH")
    pricing_service.update_variant(env.core, "admin", var(env, PLUS, "PH")["id"], {
        "pricing_mode": "manual_retail", "manual_sale_price": "150.00"})
    open_region(env, PLUS, "PH")
    cards = pricing_service.plan_cards(env.core)
    plus = next(c for c in cards if c["plan_id"] == PLUS)
    ph = next(v for v in plus["variants"] if v["region_code"] == "PH")
    assert ph["profit"] == 15000 - ph["total_cost_cny"]
    env.fx_rate[0] = 7_500_000
    pricing_service.sync_fx(env.core)
    cards = pricing_service.plan_cards(env.core)
    ph2 = next(v for v in next(c for c in cards if c["plan_id"] == PLUS)["variants"] if v["region_code"] == "PH")
    assert ph2["sale_price_cny"] == 15000
    assert ph2["profit"] == 15000 - ph2["total_cost_cny"]
    assert ph2["profit"] != ph["profit"]


def test_switch_back_to_percentage_recomputes(env):
    sync(env)
    adopt(env, PLUS, "PH")
    pricing_service.update_variant(env.core, "admin", var(env, PLUS, "PH")["id"], {
        "pricing_mode": "manual_retail", "manual_sale_price": "200.00"})
    assert var(env, PLUS, "PH")["sale_price_cny"] == 20000
    pricing_service.update_variant(env.core, "admin", var(env, PLUS, "PH")["id"], {
        "pricing_mode": "percentage", "markup_percent": "20", "rounding": "cent"})
    v = var(env, PLUS, "PH")
    assert v["pricing_mode"] == "percentage"
    assert v["sale_price_cny"] == pricing.compute_price(v["total_cost_cny"], "percentage", 2000, 0, "cent")


def test_bulk_retail_selected_regions_only(env):
    sync(env)
    adopt(env, PRO5, "EG")
    adopt(env, PRO5, "CL")
    c, tok = admin_client(env)
    eg, cl = var(env, PRO5, "EG")["id"], var(env, PRO5, "CL")["id"]
    r = c.post("/admin/plans/%s/bulk" % PRO5, data={
        "_csrf": tok, "action": "retail", "value": "88.50", "variant_ids": [eg, cl]}, follow_redirects=True)
    assert r.status_code == 200
    assert var(env, PRO5, "EG")["sale_price_cny"] == 8850
    assert var(env, PRO5, "CL")["sale_price_cny"] == 8850
    assert var(env, PRO5, "JP")["pricing_mode"] == "percentage"  # untouched
    assert var(env, PRO5, "EG")["enabled"] == 0  # not auto-listed
    assert var(env, PRO5, "CL")["enabled"] == 0


def test_select_all_regions_only_affects_current_plan(env):
    sync(env)
    adopt(env, PLUS, "PH")
    adopt(env, PLUS, "US", cost_source="manual", manual_package_cost="100")
    adopt(env, PRO5, "EG")
    ids = [var(env, PLUS, r)["id"] for r in ("PH", "US", "JP")]
    c, tok = admin_client(env)
    c.post("/admin/plans/%s/bulk" % PLUS, data={
        "_csrf": tok, "action": "retail", "value": "66.00", "variant_ids": ids})
    assert var(env, PLUS, "PH")["sale_price_cny"] == 6600
    assert var(env, PLUS, "US")["sale_price_cny"] == 6600
    assert var(env, PLUS, "JP")["sale_price_cny"] == 6600
    assert var(env, PRO5, "EG")["pricing_mode"] == "percentage"
    assert var(env, PRO5, "EG")["sale_price_cny"] != 6600


def test_bulk_invalid_rolls_back_entire_transaction(env):
    sync(env)
    adopt(env, PLUS, "PH")
    adopt(env, PLUS, "US", cost_source="manual", manual_package_cost="100")
    before_ph, before_us = var(env, PLUS, "PH"), var(env, PLUS, "US")
    c, tok = admin_client(env)
    r = c.post("/admin/plans/%s/bulk" % PLUS, data={
        "_csrf": tok, "action": "retail", "value": "not-a-price",
        "variant_ids": [before_ph["id"], before_us["id"]]}, follow_redirects=True)
    assert "操作失败" in r.data.decode() or "金额" in r.data.decode()
    assert var(env, PLUS, "PH")["sale_price_cny"] == before_ph["sale_price_cny"]
    assert var(env, PLUS, "US")["sale_price_cny"] == before_us["sale_price_cny"]
    assert var(env, PLUS, "PH")["pricing_mode"] == before_ph["pricing_mode"]


def test_bulk_retail_does_not_auto_enable(env):
    sync(env)
    adopt(env, PLUS, "PH")
    assert var(env, PLUS, "PH")["enabled"] == 0
    pricing_service.bulk(env.core, "admin", PLUS, "retail", [var(env, PLUS, "PH")["id"]], {"value": "77.00"})
    v = var(env, PLUS, "PH")
    assert v["sale_price_cny"] == 7700 and v["enabled"] == 0


def test_tampered_frontend_retail_price_ignored(env):
    sync(env)
    adopt(env, PLUS, "PH")
    pricing_service.update_variant(env.core, "admin", var(env, PLUS, "PH")["id"], {
        "pricing_mode": "manual_retail", "manual_sale_price": "128.00"})
    open_region(env, PLUS, "PH")
    pid = product_for(env, PLUS)["id"]
    c = env.app.test_client()
    r = c.post("/product/%d/buy" % pid, data={
        "quantity": "1", "region": "PH", "auth_json": AUTH, "lookup_email": "b@example.com",
        "amount": "0.01", "price": "0.01", "sale_price": "0.01", "_csrf": env.csrf(c)})
    assert r.status_code == 302
    no = env.order_no_from(r.headers["Location"])
    assert env.order(no)["amount_cents"] == 12800


def test_existing_order_immune_to_later_bulk_retail(env):
    ready(env)
    pid = product_for(env, PLUS)["id"]
    no, _ = orders.create_order(env.core, pid, 1, region="PH", recharge_auth_text=AUTH, lookup_email="a@example.com")
    before = dict(env.order(no))
    pricing_service.bulk(env.core, "admin", PLUS, "retail",
                         [var(env, PLUS, "PH")["id"], var(env, PLUS, "US")["id"]], {"value": "50.00"})
    after = dict(env.order(no))
    assert after["amount_cents"] == before["amount_cents"]
    assert after["snap_pricing_mode"] == before["snap_pricing_mode"]
    assert after["unit_price_cents"] == before["unit_price_cents"]


def test_loss_warning_but_save_allowed(env):
    sync(env)
    adopt(env, PLUS, "PH")  # cost ~115.20
    pricing_service.update_variant(env.core, "admin", var(env, PLUS, "PH")["id"], {
        "pricing_mode": "manual_retail", "manual_sale_price": "10.00"})
    v = var(env, PLUS, "PH")
    assert v["sale_price_cny"] == 1000
    cards = pricing_service.plan_cards(env.core)
    ph = next(x for x in next(c for c in cards if c["plan_id"] == PLUS)["variants"] if x["region_code"] == "PH")
    assert ph["loss"] is True and ph["profit"] < 0
    # Existing policy: can enable when sale_price is set (even if lossy).
    pricing_service.toggle_variant(env.core, "admin", v["id"], True)
    assert var(env, PLUS, "PH")["enabled"] == 1


def test_migration_keeps_existing_modes_and_prices(env):
    sync(env)
    adopt(env, PLUS, "PH")
    open_region(env, PLUS, "PH")
    before = var(env, PLUS, "PH")
    assert before["pricing_mode"] == "percentage" and before["sale_price_cny"] == 13824
    assert migrate(env.db_path, verbose=False) == []  # already applied
    cols = {r[1] for r in env.core.conn.execute("PRAGMA table_info(plan_variants)")}
    assert "manual_sale_price_cny" in cols
    after = var(env, PLUS, "PH")
    assert after["pricing_mode"] == "percentage"
    assert after["sale_price_cny"] == 13824
    assert after["manual_sale_price_cny"] is None


def test_audit_records_retail_change(env):
    sync(env)
    adopt(env, PLUS, "PH")
    pricing_service.update_variant(env.core, "admin", var(env, PLUS, "PH")["id"], {
        "pricing_mode": "manual_retail", "manual_sale_price": "130.00"})
    row = env.core.conn.execute(
        "SELECT * FROM pricing_audit WHERE plan_id=? AND region_code=? AND kind='variant_update' ORDER BY id DESC LIMIT 1",
        (PLUS, "PH")).fetchone()
    assert "直接设置零售价" in row["message"] or "130.00" in row["message"]
    detail = json.loads(row["detail"])
    assert detail["new_mode"] == "manual_retail"
    assert detail["new_sale_price_cny"] == 13000

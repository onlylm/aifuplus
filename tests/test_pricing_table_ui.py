"""The admin pricing table: tabs, one row per region, drawer editing, in-place saving, friendly guards."""
import re
from pathlib import Path

from .test_pricing import MAX, PLUS, PRO5, adopt, ready, sync, var
from .test_ui_flow import admin_client, visible

AJAX = {"X-Requested-With": "fetch", "Accept": "application/json"}


def page(c, query=""):
    return c.get("/admin/products" + query).get_data(as_text=True)


def update(c, tok, vid, ajax=True, **fields):
    data = {"_csrf": tok}
    data.update(fields)
    return c.post("/admin/variants/%d/update" % vid, data=data, headers=AJAX if ajax else {})


def test_tabs_table_and_no_region_cards(env):
    ready(env)
    c, _ = admin_client(env)
    html = page(c)
    tabs = re.findall(r'<button type="button" role="tab"[^>]*>(.*?)<span', html)
    assert [t.strip() for t in tabs] == ["Plus", "Pro 5x", "Pro 20x", "Pro Max"]
    assert 'class="vc' not in html and 'class="vgrid"' not in html                   # no big per-region cards any more
    for col in ("地区", "套餐成本", "其他成本", "总成本", "零售价", "预计利润", "销售状态", "操作"):
        assert "<th>%s</th>" % col in html or ">%s</th>" % col in html, col
    plus_panel = html.split('id="plan-plus-monthly"')[1].split('role="tabpanel"')[0]
    assert plus_panel.count("<tr id=\"row-") == 3                                       # PH, US, JP: one row per region
    assert html.count("data-edit=") == sum(1 for _ in env.core.conn.execute("SELECT 1 FROM plan_variants"))
    assert "已上架地区" in visible(html) and "最低售价" in visible(html)


def test_active_tab_and_open_drawer_come_from_the_url(env):
    ready(env)
    c, _ = admin_client(env)
    assert re.search(r'class="pp-tab on"[^>]*data-plan="pro5x-monthly"', page(c, "?plan=pro5x-monthly"))
    vid = var(env, PLUS, "PH")["id"]
    html = page(c, "?plan=plus-monthly&edit=%d" % vid)
    assert "window.PP_OPEN=%d" % vid in html
    assert re.search(r'class="pp-tab on"[^>]*data-plan="plus-monthly"', html)


def test_drawer_has_three_clear_pricing_methods_and_only_one_set_of_fields_shows(env):
    ready(env)
    c, _ = admin_client(env)
    html = page(c)
    dr = html.split('id="dr-%d"' % var(env, PLUS, "PH")["id"])[1].split('<div class="pp-dr"')[0]
    assert re.findall(r'name="pricing_mode" value="(\w+)"', dr) == ["manual_retail", "percentage", "fixed"]
    for label in ("直接设置零售价", "按成本百分比加价", "固定利润", "只有选中的方式会生效"):
        assert label in dr, label
    for pane in ("manual_retail", "percentage", "fixed"):                                 # one pane per method; the script shows only the chosen one
        assert 'data-pane="%s"' % pane in dr
    js = (Path(__file__).resolve().parent.parent / "app" / "static" / "admin-pricing.js").read_text(encoding="utf-8")
    assert "i.disabled = !on" in js                                                        # fields of hidden methods are not submitted
    assert "'Escape'" in js and "X-Requested-With" in js and "history.replaceState" in js
    assert "if (!count) return" in js and "backdrop.addEventListener('click', closeDrawer)" in js


def test_no_cost_explains_which_methods_are_available(env):
    sync(env)                                                                              # nothing adopted: every region has no package cost
    c, _ = admin_client(env)
    html = page(c)
    v = visible(html)
    assert "个地区还没有套餐成本" in v and "可以</b>使用「直接设置零售价」" in html and "不能</b>使用「按成本百分比加价」和「固定利润」" in html
    assert "暂无成本，无法计算" in v and "请先填写套餐成本" in html


def test_direct_retail_price_saves_without_any_cost_and_shows_profit_note(env):
    sync(env)
    c, tok = admin_client(env)
    vid = var(env, MAX, "PH")["id"]
    r = update(c, tok, vid, pricing_mode="manual_retail", manual_sale_price="128", cost_source="manual", manual_package_cost="", other_cost="0")
    d = r.get_json()
    assert r.status_code == 200 and d["ok"] is True and d["plan_id"] == MAX
    row = next(x for x in d["variants"] if x["id"] == vid)
    assert row["price"] == "¥128.00" and row["profit"] == "暂无成本，无法计算" and row["mode"] == "manual_retail" and row["package_cost"] == "未填写"
    v = var(env, MAX, "PH")
    assert v["sale_price_cny"] == 12800 and v["pricing_mode"] == "manual_retail" and v["effective_package_cost_cny"] is None


def test_typed_retail_price_is_never_silently_ignored(env):
    ready(env)
    c, tok = admin_client(env)
    v = var(env, PLUS, "PH")
    before = (v["sale_price_cny"], v["pricing_mode"], v["manual_sale_price_cny"])
    r = update(c, tok, v["id"], pricing_mode="percentage", markup_percent="30", manual_sale_price="66")
    d = r.get_json()
    assert r.status_code == 400 and d["ok"] is False and "直接设置零售价" in d["message"] and "不会生效" in d["message"]
    after = var(env, PLUS, "PH")
    assert (after["sale_price_cny"], after["pricing_mode"], after["manual_sale_price_cny"]) == before
    r = update(c, tok, v["id"], ajax=False, pricing_mode="fixed", fixed_profit="9", manual_sale_price="66")
    assert r.status_code == 302 and "edit=%d" % v["id"] in r.headers["Location"]           # fallback keeps the plan and the row


def test_percentage_and_fixed_need_a_package_cost(env):
    sync(env)
    c, tok = admin_client(env)
    vid = var(env, MAX, "PH")["id"]
    for mode, extra in (("percentage", {"markup_percent": "25"}), ("fixed", {"fixed_profit": "20"})):
        r = update(c, tok, vid, pricing_mode=mode, cost_source="manual", manual_package_cost="", **extra)
        d = r.get_json()
        assert r.status_code == 400 and "请先填写套餐成本" in d["message"] and "直接设置零售价" in d["message"], mode
        assert var(env, MAX, "PH")["pricing_mode"] == "percentage" and var(env, MAX, "PH")["sale_price_cny"] is None
    r = update(c, tok, vid, pricing_mode="percentage", markup_percent="25", cost_source="manual", manual_package_cost="100")
    assert r.status_code == 200 and var(env, MAX, "PH")["sale_price_cny"] == 12500        # cost typed in the same save -> allowed
    r = update(c, tok, var(env, PLUS, "JP")["id"], pricing_mode="percentage", cost_source="upstream")    # JP: upstream gave no cost
    assert r.status_code == 400


def test_save_response_keeps_everything_needed_to_stay_in_place(env):
    ready(env)
    c, tok = admin_client(env)
    vid = var(env, PLUS, "PH")["id"]
    d = update(c, tok, vid, pricing_mode="percentage", markup_percent="50", cost_source="upstream", other_cost="0", rounding="yuan_up").get_json()
    assert d["ok"] and d["message"] == "已保存。" and d["plan_id"] == PLUS
    assert {x["id"] for x in d["variants"]} == {r["id"] for r in env.core.conn.execute("SELECT id FROM plan_variants WHERE plan_id=?", (PLUS,))}
    assert d["summary"]["live"] == 2 and d["summary"]["min_price"].startswith("¥")
    row = next(x for x in d["variants"] if x["id"] == vid)
    assert row["fields"]["markup"] == "50" and row["fields"]["rounding"] == "yuan_up" and row["fields"]["mode"] == "percentage"
    assert row["price"] == "¥173.00" and row["profit"] == "¥57.80" and row["total_cost"] == "¥115.20"


def test_row_toggle_and_bulk_actions_reply_in_place(env):
    ready(env)
    c, tok = admin_client(env)
    jp = var(env, PLUS, "JP")["id"]
    r = c.post("/admin/variants/%d/toggle" % jp, data={"_csrf": tok, "enabled": "1"}, headers=AJAX)
    assert r.status_code == 400 and "还没有" in r.get_json()["message"]                     # no price yet: cannot list
    ph, us = var(env, PLUS, "PH")["id"], var(env, PLUS, "US")["id"]
    r = c.post("/admin/variants/%d/toggle" % us, data={"_csrf": tok, "enabled": "0"}, headers=AJAX).get_json()
    assert r["ok"] and next(x for x in r["variants"] if x["id"] == us)["status"] == "未销售" and r["summary"]["live"] == 1
    url = "/admin/plans/%s/bulk" % PLUS
    r = c.post(url, data={"_csrf": tok, "action": "retail", "value_retail": "99", "variant_ids": [ph, us, jp]}, headers=AJAX).get_json()
    assert r["ok"] and "未自动上架" in r["message"]
    assert [x["price"] for x in r["variants"]] == ["¥99.00"] * 3 and var(env, PLUS, "JP")["enabled"] == 0
    assert next(x for x in r["variants"] if x["id"] == jp)["profit"] == "暂无成本，无法计算"
    r = c.post(url, data={"_csrf": tok, "action": "other_cost", "value_other_cost": "7", "variant_ids": [ph, us]}, headers=AJAX).get_json()
    assert r["ok"] and var(env, PLUS, "PH")["other_cost_cny"] == 700 and var(env, PLUS, "JP")["other_cost_cny"] == 0
    r = c.post(url, data={"_csrf": tok, "action": "enable", "variant_ids": [us]}, headers=AJAX).get_json()
    assert r["ok"] and var(env, PLUS, "US")["enabled"] == 1
    r = c.post(url, data={"_csrf": tok, "action": "disable", "variant_ids": [us, ph]}, headers=AJAX).get_json()
    assert r["ok"] and var(env, PLUS, "US")["enabled"] == 0 and var(env, PLUS, "PH")["enabled"] == 0
    r = c.post(url, data={"_csrf": tok, "action": "retail", "value_retail": "", "variant_ids": [ph]}, headers=AJAX)
    assert r.status_code == 400 and "请填写零售价" in r.get_json()["message"]
    r = c.post(url, data={"_csrf": tok, "action": "retail", "value_retail": "50"}, headers=AJAX)
    assert r.status_code == 400 and "勾选" in r.get_json()["message"]                       # nothing selected


def test_bulk_markup_skips_regions_without_cost_and_says_so(env):
    sync(env)
    adopt(env, PRO5, "EG")
    c, tok = admin_client(env)
    r = c.post("/admin/plans/%s/bulk" % PRO5, data={"_csrf": tok, "action": "markup", "value": "30"}, headers=AJAX).get_json()
    skipped_part = r["message"].split("另有")[1]
    assert r["ok"] and "日本" in skipped_part and "智利" in skipped_part and "埃及" not in skipped_part and "已跳过" in skipped_part
    assert var(env, PRO5, "EG")["markup_basis_points"] == 3000 and var(env, PRO5, "JP")["markup_basis_points"] == 2000
    only_missing = c.post("/admin/plans/%s/bulk" % MAX, data={"_csrf": tok, "action": "profit", "value": "10"}, headers=AJAX)
    assert only_missing.status_code == 400 and "请先填写套餐成本" in only_missing.get_json()["message"]


def test_anomaly_confirm_works_in_place(env):
    ready(env)
    env.upstream.config_payload["wallet"]["prices"]["plus-monthly"]["PH"] = 20.0
    env.t[0] += 1800
    sync(env)
    c, tok = admin_client(env)
    vid = var(env, PLUS, "PH")["id"]
    html = page(c)
    assert "成本异常，等待确认" in visible(html) and "确认异常价格" in html
    r = c.post("/admin/variants/%d/confirm" % vid, data={"_csrf": tok}, headers=AJAX).get_json()
    assert r["ok"] and next(x for x in r["variants"] if x["id"] == vid)["anomaly"] == ""


def test_ajax_still_needs_csrf_and_admin(env):
    ready(env)
    c, tok = admin_client(env)
    vid = var(env, PLUS, "PH")["id"]
    assert c.post("/admin/variants/%d/update" % vid, data={"pricing_mode": "manual_retail", "manual_sale_price": "1"}, headers=AJAX).status_code == 400
    anon = env.app.test_client()
    assert anon.post("/admin/variants/%d/update" % vid, data={"_csrf": "x"}, headers=AJAX).status_code in (302, 400, 403)
    assert var(env, PLUS, "PH")["sale_price_cny"] != 100


def test_page_hides_technical_words_and_keeps_old_sections(env):
    ready(env)
    c, _ = admin_client(env)
    html = page(c)
    v = visible(html)
    for bad in ("manual_retail", "percentage", "plus-monthly", "planId", "JSON", "wallet", "paymentSource"):
        assert bad not in v, bad
    for keep in ("汇率与成本同步", "立即同步", "重新计算售价", "卡密商品", "价格异常"):
        assert keep in v, keep
    assert "最近的价格记录" not in html and "→" not in v                         # the audit list is gone from the daily page

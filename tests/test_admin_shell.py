"""Admin information architecture: sidebar, system-settings tabs, order filters and live (read-only) status."""
import json
import re
from pathlib import Path

import pytest

from app import ops, orders, worker

from .conftest import AUTH
from .test_pricing import MAX, PLUS, PRO5, adopt, open_region, product_for, ready, sync, var
from .test_ui_flow import admin_client, visible

PRO20 = "pro20x-direct-monthly"
STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def auth_for(email):
    return json.dumps({"accessToken": "TOK-" + email, "sessionToken": "s", "account_id": "acc-1", "user": {"email": email}})


def ready_all(env):
    sync(env)
    for plan, region in ((PLUS, "PH"), (PLUS, "US"), (PRO5, "EG"), (PRO20, "PH"), (MAX, "PH")):
        adopt(env, plan, region, cost_source="manual", manual_package_cost="100")
        open_region(env, plan, region)


def mk(env, plan, region, lookup, recharge, state="unpaid", created=None, contact=""):
    """Create a real order through the normal path, then put it in the wanted state for list/filter tests."""
    no, _tok = orders.create_order(env.core, product_for(env, plan)["id"], 1, region=region, recharge_auth_text=auth_for(recharge),
                                   lookup_email=lookup, contact=contact)
    c = env.core.conn
    oid = env.order(no)["id"]
    if created:
        c.execute("UPDATE orders SET created_at=? WHERE id=?", (created, oid))
    if state == "closed":
        c.execute("UPDATE orders SET payment_status='closed' WHERE id=?", (oid,))
    elif state != "unpaid":
        c.execute("UPDATE orders SET payment_status='paid', paid_at=? WHERE id=?", (created or env.t[0], oid))
        c.execute("UPDATE fulfillments SET status=?, reason=? WHERE order_id=?",
                  ({"processing": "pending", "attention": "blocked", "done": "delivered"}[state], "积分不足" if state == "attention" else "", oid))
    return no, oid


def nos(html):
    return re.findall(r'class="mono" href="/admin/orders/\d+">([A-Z0-9]+)</a>', html)


def get(c, **params):
    return c.get("/admin/orders", query_string=params).get_data(as_text=True)


# ------------------------------------------------------------------ sidebar
def test_sidebar_has_exactly_four_primary_entries_and_a_footer(env):
    c, _ = admin_client(env)
    html = c.get("/admin/").get_data(as_text=True)
    nav = re.search(r'<nav class="adm-nav">(.*?)</nav>', html, flags=re.S).group(1)
    labels = re.findall(r"<span>([^<]+)</span></a>", nav)
    assert labels == ["首页", "订单管理", "商品与定价", "系统设置"]
    assert nav.count("<a ") == 4 and "上游充值" not in nav and "支付宝收款" not in nav and "网站内容" not in nav
    foot = re.search(r'<div class="adm-side-foot">(.*?)</div>\s*</aside>', html, flags=re.S).group(1)
    assert "查看前台" in foot and "退出登录" in foot and 'action="/logout"' in foot
    assert '<header class="top"' not in html                                   # the old top bar is gone


@pytest.mark.parametrize("url,label", [("/admin/", "首页"), ("/admin/orders", "订单管理"), ("/admin/products", "商品与定价"),
                                       ("/admin/alipay", "系统设置"), ("/admin/upstream", "系统设置"), ("/admin/site", "系统设置")])
def test_current_page_is_highlighted_in_the_sidebar(env, url, label):
    c, _ = admin_client(env)
    nav = re.search(r'<nav class="adm-nav">(.*?)</nav>', c.get(url).get_data(as_text=True), flags=re.S).group(1)
    on = re.findall(r'<a [^>]*class="on"[^>]*>.*?<span>([^<]+)</span>', nav, flags=re.S)
    assert on == [label]
    assert nav.count('aria-current=page') == 1


def test_order_detail_keeps_orders_highlighted(env):
    ready_all(env)
    _no, oid = mk(env, PLUS, "PH", "a@example.com", "r@example.com")
    c, _ = admin_client(env)
    nav = re.search(r'<nav class="adm-nav">(.*?)</nav>', c.get("/admin/orders/%d" % oid).get_data(as_text=True), flags=re.S).group(1)
    assert re.findall(r'<a [^>]*class="on"[^>]*>.*?<span>([^<]+)</span>', nav, flags=re.S) == ["订单管理"]


def test_mobile_menu_markup_and_behaviour_contract(env):
    c, _ = admin_client(env)
    html = c.get("/admin/").get_data(as_text=True)
    for need in ('id="adm-menu"', 'aria-controls="adm-side"', 'id="adm-scrim"', 'id="adm-close"', "admin-shell.js"):
        assert need in html, need
    js = (STATIC / "admin-shell.js").read_text(encoding="utf-8")
    assert "'Escape'" in js and "scrim.addEventListener('click'" in js and "x.addEventListener('click'" in js and "aria-expanded" in js
    assert "e.target.closest('.adm-nav a')" in js and "setOpen(false)" in js
    css = (STATIC / "admin.css").read_text(encoding="utf-8")
    m = re.search(r"@media\(max-width:900px\)\{(.*?)\n\}", css, flags=re.S).group(1)
    assert ".adm-side{position:fixed" in m and "translateX(-102%)" in m and ".adm-bar{display:flex" in m
    assert re.search(r"\.adm-shell\{display:grid;grid-template-columns:216px", css) and ".adm-main{overflow-y:auto" in css     # fixed left, scrolling right
    assert "max-width:none" in css.split(".adm-content{")[1].split("}")[0]                                                       # wide tables get the room


# ------------------------------------------------------------------ system settings
def test_three_config_entries_live_under_system_settings_tabs(env):
    c, tok = admin_client(env)
    assert c.get("/admin/settings").status_code == 302 and c.get("/admin/settings").headers["Location"].endswith("/admin/alipay")
    for url, current in (("/admin/alipay", "支付与收款"), ("/admin/upstream", "上游充值"), ("/admin/site", "网站内容")):
        html = c.get(url).get_data(as_text=True)
        tabs = re.search(r'<div class="set-tabs".*?</div>', html, flags=re.S).group(0)
        assert re.findall(r'<a role="tab"[^>]*>([^<]+)</a>', tabs) == ["支付与收款", "上游充值", "网站内容"]
        assert re.findall(r'<a role="tab"[^>]*class="on"[^>]*>([^<]+)</a>', tabs) == [current]            # clear selected state, per URL (refresh keeps it)
        assert "<h1>系统设置</h1>" in html
    assert c.post("/admin/alipay", data={"_csrf": tok, "mode": "sandbox", "order_timeout_minutes": "15"}, follow_redirects=True).status_code == 200


def test_settings_forms_are_not_duplicated(env):
    c, _ = admin_client(env)
    for url, action_ok in (("/admin/alipay", 1), ("/admin/upstream", 1), ("/admin/site", 1)):
        html = c.get(url).get_data(as_text=True)
        main = re.findall(r'<form method="post">', html)
        assert len(main) == action_ok, url                                   # one main settings form per tab, nothing copied between tabs
    assert c.get("/admin/settings", follow_redirects=True).get_data(as_text=True).count('name="private_key"') == 1


def test_save_stays_on_its_own_tab(env):
    c, tok = admin_client(env)
    r = c.post("/admin/site", data={"_csrf": tok, "site_name": "测试店"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["Location"].endswith("/admin/site")
    r = c.post("/admin/upstream", data={"_csrf": tok, "enabled": "", "base_url": "https://pro20x.com/api/v3/automate"}, follow_redirects=False)
    assert r.headers["Location"].endswith("/admin/upstream")


# ------------------------------------------------------------------ pricing page
def test_pricing_page_has_no_price_log_but_keeps_sync_facts(env):
    ready(env)
    c, _ = admin_client(env)
    before = env.core.conn.execute("SELECT COUNT(*) FROM pricing_audit").fetchone()[0]
    html = c.get("/admin/products").get_data(as_text=True)
    v = visible(html)
    assert "最近的价格记录" not in html and "上游成本同步成功" not in html and "同步和调价都会留下记录" not in html
    assert "→" not in v and "¥-" not in v
    for keep in ("上游成本最近同步", "汇率最近同步", "同步正常", "价格异常"):
        assert keep in v, keep
    assert before > 0 and env.core.conn.execute("SELECT COUNT(*) FROM pricing_audit").fetchone()[0] == before      # the audit table is untouched
    assert re.findall(r'<button type="button" role="tab"[^>]*>(.*?)<span', html) == ["Plus", "Pro 5x", "Pro 20x", "Pro Max"]
    assert 'class="pp-table"' in html and 'class="vc' not in html and "pp-drawer" in html


# ------------------------------------------------------------------ order filters
def test_orders_can_be_filtered_by_each_of_the_four_plans(env):
    ready_all(env)
    mk(env, PLUS, "PH", "a@x.co", "pa@x.co")
    mk(env, PRO5, "EG", "b@x.co", "pb@x.co")
    mk(env, PRO20, "PH", "c@x.co", "pc@x.co")
    mk(env, MAX, "PH", "d@x.co", "pd@x.co")
    c, _ = admin_client(env)
    assert len(nos(get(c))) == 4
    for plan, who in ((PLUS, "pa@x.co"), (PRO5, "pb@x.co"), (PRO20, "pc@x.co"), (MAX, "pd@x.co")):
        html = get(c, plan=plan)
        assert len(nos(html)) == 1 and who in html and "共 <b>1</b> 笔订单" in html, plan
    bar = re.search(r'<select class="t" name="plan">(.*?)</select>', get(c), flags=re.S).group(1)
    assert re.findall(r"<option[^>]*>([^<]+)</option>", bar) == ["全部套餐", "Plus", "Pro 5x", "Pro 20x", "Pro Max"]


def test_region_options_are_chinese_and_filter_correctly(env):
    ready_all(env)
    mk(env, PLUS, "PH", "a@x.co", "pa@x.co")
    mk(env, PLUS, "US", "b@x.co", "pb@x.co")
    c, _ = admin_client(env)
    html = get(c)
    bar = re.search(r'<select class="t" name="region">(.*?)</select>', html, flags=re.S).group(1)
    opts = re.findall(r"<option[^>]*>([^<]+)</option>", bar)
    assert opts[0] == "全部地区" and "菲律宾" in opts and "美国" in opts and "PH" not in opts and "US" not in opts
    assert len(nos(get(c, region="PH"))) == 1 and "pa@x.co" in get(c, region="PH")
    assert "pb@x.co" in get(c, region="US") and "pa@x.co" not in get(c, region="US")


def test_status_time_customer_and_plan_filters_combine(env):
    ready_all(env)
    now = env.t[0]
    today0, _ = ops.period_bounds("today", now)
    yday0, _ = ops.period_bounds("yesterday", now)
    day = 86400
    a1, _ = mk(env, PLUS, "PH", "first@x.co", "r1@x.co", "done", created=today0 + 3600)                 # today, done, first order of first@
    a2, _ = mk(env, PLUS, "PH", "first@x.co", "r1@x.co", "attention", created=today0 + 7200)            # today, needs attention, repeat
    a3, _ = mk(env, PRO5, "EG", "other@x.co", "r2@x.co", "processing", created=yday0 + 3600)            # yesterday
    a4, _ = mk(env, PLUS, "US", "old@x.co", "r3@x.co", "unpaid", created=today0 - 5 * day)              # 5 days ago
    a5, _ = mk(env, MAX, "PH", "ancient@x.co", "r4@x.co", "closed", created=today0 - 20 * day)          # 20 days ago
    c, _ = admin_client(env)
    got = lambda **p: set(nos(get(c, **p)))
    assert got() == {a1, a2, a3, a4, a5}
    assert got(period="today") == {a1, a2} and got(period="yesterday") == {a3} and got(period="7d") == {a1, a2, a3, a4} and got(period="30d") == {a1, a2, a3, a4, a5}
    assert got(view="done") == {a1} and got(view="attention") == {a2} and got(view="processing") == {a3} and got(view="unpaid") == {a4} and got(view="closed") == {a5}
    assert got(customer="repeat") == {a2} and got(customer="first") == {a1, a3, a4, a5}
    assert got(plan=PLUS, period="7d") == {a1, a2, a4}
    assert got(plan=PLUS, region="PH", period="today", view="attention", customer="repeat") == {a2}
    assert got(plan=PLUS, region="PH", period="today", view="attention", customer="first") == set()
    fmt = lambda t: ops.format_ts(t)[:10]
    assert got(period="custom", **{"from": fmt(today0 - 6 * day), "to": fmt(today0 - 4 * day)}) == {a4}
    assert got(period="custom", **{"from": fmt(today0 - 25 * day), "to": fmt(today0)}) == {a1, a2, a3, a4, a5}


def test_custom_dates_need_both_ends_and_bad_ranges_explain_themselves(env):
    ready_all(env)
    mk(env, PLUS, "PH", "a@x.co", "pa@x.co")
    c, _ = admin_client(env)
    html = get(c, period="custom", **{"from": "2027-01-01"})
    assert "需要同时选择开始日期和结束日期" in html and len(nos(html)) == 1
    html = get(c, period="custom", **{"from": "2027-01-10", "to": "2027-01-01"})
    assert "结束日期不能早于开始日期" in html and len(nos(html)) == 1
    assert get(c, period="custom", **{"from": "garbage", "to": "2027-01-01"}).count("日期格式不对") == 1


def test_search_box_matches_order_number_contact_email_and_full_recharge_email(env):
    ready_all(env)
    n1, _ = mk(env, PLUS, "PH", "Buyer.One@x.co", "acct.one@x.co", contact="wechat-1")
    n2, _ = mk(env, PLUS, "US", "buyer.two@x.co", "acct.two@x.co")
    c, _ = admin_client(env)
    assert nos(get(c, q=n1[-8:])) == [n1]                                  # part of the order number
    assert nos(get(c, q="buyer.one@x.co")) == [n1]                         # contact e-mail (case-insensitive)
    assert nos(get(c, q="ACCT.TWO@x.co")) == [n2]                          # full recharge e-mail
    assert nos(get(c, q="acct.two")) == []                                 # partial e-mail is not a full-email match
    assert 'value="buyer.one@x.co"' in get(c, q="buyer.one@x.co")           # the box keeps what you typed


def test_filters_survive_refresh_and_pagination(env):
    ready_all(env)
    for i in range(35):
        mk(env, PLUS, "PH", "buyer%d@x.co" % i, "acct%d@x.co" % i)
    mk(env, PRO5, "EG", "z@x.co", "zz@x.co")
    c, _ = admin_client(env)
    html = get(c, plan=PLUS, region="PH", view="unpaid", customer="first")
    assert "共 <b>35</b> 笔订单" in html and len(nos(html)) == 30 and "第 1 / 2 页" in html
    nxt = re.search(r'<a class="btn sm" href="([^"]+)">下一页</a>', html).group(1).replace("&amp;", "&")
    for k, v in (("plan", PLUS), ("region", "PH"), ("view", "unpaid"), ("customer", "first"), ("page", "2")):
        assert "%s=%s" % (k, v) in nxt, (k, nxt)
    page2 = c.get(nxt).get_data(as_text=True)
    assert len(nos(page2)) == 5 and "第 2 / 2 页" in page2 and "上一页</a>" in page2
    prev = re.search(r'<a class="btn sm" href="([^"]+)">上一页</a>', page2).group(1).replace("&amp;", "&")
    assert "plan=%s" % PLUS in prev and "view=unpaid" in prev
    for name, val in (("plan", PLUS), ("region", "PH"), ("view", "unpaid"), ("customer", "first")):                     # selections come back selected
        assert re.search(r'<option value="%s" selected>' % re.escape(val), page2), name


def test_clear_button_total_and_empty_states(env):
    c, _ = admin_client(env)
    assert "还没有订单" in get(c) and "清除筛选" not in get(c)
    ready_all(env)
    mk(env, PLUS, "PH", "a@x.co", "pa@x.co")
    html = get(c, view="done")
    assert "没有符合当前筛选条件的订单" in html and "系统里一共有 1 笔订单" in html and "还没有订单" not in html
    assert html.count("清除筛选") >= 2 and 'href="/admin/orders"' in html
    assert "清除筛选" not in get(c) and "共 <b>1</b> 笔订单" in get(c)


def test_full_emails_show_for_admins_only(env):
    ready_all(env)
    no, oid = mk(env, PLUS, "PH", "contact.full@x.co", "recharge.full@x.co")
    c, _ = admin_client(env)
    page = get(c)
    assert "contact.full@x.co" in page and "recharge.full@x.co" in page
    anon = env.app.test_client()
    other = env.app.test_client()
    tok = env.csrf(other)
    r = other.post("/orders", data={"order_no": no, "email": "contact.full@x.co", "_csrf": tok}, follow_redirects=True).get_data(as_text=True)
    assert "recharge.full@x.co" not in r and "contact.full@x.co" not in r
    assert "recharge.full@x.co" not in anon.get("/orders").get_data(as_text=True)


# ------------------------------------------------------------------ access control for the new endpoints
def test_filters_and_live_endpoints_are_admin_only(env):
    ready_all(env)
    _no, oid = mk(env, PLUS, "PH", "a@x.co", "pa@x.co")
    env.make_user("buyer@x.co")
    anon, user = env.app.test_client(), env.app.test_client()
    env.login(user, "buyer@x.co")
    urls = ["/admin/orders?plan=%s&view=done" % PLUS, "/admin/api/orders/live?ids=%d" % oid, "/admin/api/orders/%d/live" % oid]
    for u in urls:
        assert anon.get(u).status_code == 302 and "/login" in anon.get(u).headers["Location"], u
        assert user.get(u).status_code == 403, u
    c, _ = admin_client(env)
    assert all(c.get(u).status_code == 200 for u in urls)
    assert c.get("/admin/api/orders/99999/live").status_code == 404


# ------------------------------------------------------------------ live status
def paid_recharge_order(env):
    """A real order paid through the real notification path (stays 'pending' until the worker runs)."""
    ready_all(env)
    c = env.app.test_client()
    pid = product_for(env, PLUS)["id"]
    r = c.post("/product/%d/buy" % pid, data={"quantity": "1", "region": "PH", "auth_json": AUTH, "lookup_email": "buyer@example.com", "_csrf": env.csrf(c)})
    no = env.order_no_from(r.headers["Location"])
    return no, env.order(no)["id"], r.headers["Location"]


def test_live_json_shows_only_real_stages_in_order(env):
    no, oid, _loc = paid_recharge_order(env)
    c, _ = admin_client(env)
    live = lambda: c.get("/admin/api/orders/%d/live" % oid).get_json()
    d = live()
    assert d["stage"]["text"] == "等待付款" and d["payment"]["text"] == "待付款" and d["steps"][0] == {"text": "等待付款", "state": "current"}
    env.alipay.pay(no)
    env.post_notify(env.alipay.notify_form(no))
    d = live()
    assert d["stage"]["text"] == "等待 Worker" and d["payment"]["text"] == "已付款" and "等待 Worker 领取" in d["worker_text"]
    assert [s["state"] for s in d["steps"]] == ["done", "done", "current", "pending", "pending", "pending"]
    assert [s["text"] for s in d["steps"]] == ["等待付款", "已付款", "等待 Worker", "已提交上游", "上游处理中", "充值完成"]
    worker.sweep_once(env.core)
    d = live()
    assert d["stage"]["text"] == "已提交上游" and d["worker_text"] == "Worker 已提交上游" and d["upstream_text"] == "上游排队中"
    env.upstream.set_status("%s-1" % no, "running")
    env.t[0] += 10
    worker.sweep_once(env.core)
    d = live()
    assert d["stage"]["text"] == "上游处理中" and d["upstream_text"] == "上游充值中"
    env.upstream.set_status("%s-1" % no, "succeeded")
    env.t[0] += 10
    worker.sweep_once(env.core)
    d = live()
    assert d["stage"] == {"key": "done", "text": "充值完成", "tone": "ok"} and all(s["state"] == "done" for s in d["steps"]) and d["fulfillment"]["text"] == "已完成"
    assert d["completed_at_text"] and not d["needs_action"]


def test_live_json_for_attention_and_closed_orders(env):
    ready_all(env)
    _n1, o1 = mk(env, PLUS, "PH", "a@x.co", "pa@x.co", "attention")
    _n2, o2 = mk(env, PLUS, "US", "b@x.co", "pb@x.co", "closed")
    c, _ = admin_client(env)
    d = c.get("/admin/api/orders/%d/live" % o1).get_json()
    assert d["stage"]["text"] == "需要人工处理" and d["terminal"] == {"text": "需要人工处理", "tone": "bad"} and d["needs_action"] and d["attention_reason"] == "积分不足"
    d = c.get("/admin/api/orders/%d/live" % o2).get_json()
    assert d["stage"]["text"] == "已关闭" and d["terminal"]["text"] == "已关闭" and not d["needs_action"]
    rows = c.get("/admin/api/orders/live?ids=%d,%d,abc" % (o1, o2)).get_json()
    assert rows["rows"][str(o1)]["needs_attention"] is True and rows["rows"][str(o1)]["stage_text"] == "需要人工处理"
    assert rows["rows"][str(o2)]["stage_text"] == "已关闭" and rows["attention_total"] == 1


def test_live_json_never_leaks_secrets_or_raw_payment_data(env):
    no, oid, loc = paid_recharge_order(env)
    token = loc.split("t=")[1]
    env.alipay.pay(no)
    env.post_notify(env.alipay.notify_form(no))
    worker.sweep_once(env.core)
    c, _ = admin_client(env)
    texts = [c.get("/admin/api/orders/%d/live" % oid).get_data(as_text=True), c.get("/admin/api/orders/live?ids=%d" % oid).get_data(as_text=True)]
    raw_payment = env.core.conn.execute("SELECT raw FROM payments").fetchone()[0]
    trade_no = env.core.conn.execute("SELECT trade_no FROM payments").fetchone()[0]
    secrets_ = ["AT-SECRET-TOKEN", token, "sessionToken", "accessToken", "recharge_input_enc", "guest_token", "notify_id", "buyer_logon_id",
                raw_payment[:40], trade_no, "KEY-123", "private_key", "gAAAA"]
    for t in texts:
        for s_ in secrets_:
            assert s_ not in t, s_
    allowed = {"ok", "id", "stage", "steps", "terminal", "payment", "fulfillment", "worker_text", "upstream_text", "headline", "headline_tone",
               "needs_action", "attention_reason", "paid_at_text", "completed_at_text", "closed_at_text"}
    assert set(json.loads(texts[0])) == allowed
    assert set(json.loads(texts[1])) == {"ok", "rows", "attention_total"}
    assert set(json.loads(texts[1])["rows"][str(oid)]) == {"id", "stage", "stage_text", "tone", "needs_attention", "payment_text", "payment_tone",
                                                           "fulfillment_text", "fulfillment_tone", "reason"}


def test_polling_the_admin_status_never_submits_a_recharge(env):
    no, oid, _loc = paid_recharge_order(env)
    env.alipay.pay(no)
    env.post_notify(env.alipay.notify_form(no))
    c, _ = admin_client(env)
    posts = lambda: [r for r in env.upstream.requests if r[0] == "POST"]
    events_before = env.core.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    for _ in range(6):
        c.get("/admin/api/orders/%d/live" % oid)
        c.get("/admin/api/orders/live?ids=%d" % oid)
        c.get("/admin/orders")
        c.get("/admin/orders/%d" % oid)
    assert posts() == [] and env.upstream.created == 0
    assert env.ful(no)["status"] == "pending" and env.ful(no)["upstream_task_id"] == ""
    assert env.core.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events_before
    worker.sweep_once(env.core)                                                         # only the worker submits
    assert len(posts()) == 1 and env.upstream.created == 1


def test_live_scripts_are_calm_and_honest(env):
    ready_all(env)
    _no, oid = mk(env, PLUS, "PH", "a@x.co", "pa@x.co")
    c, _ = admin_client(env)
    js = (STATIC / "admin-live.js").read_text(encoding="utf-8")
    assert "visibilityState" in js and "Math.min(delay * 2, 60000)" in js and "console." not in js
    assert "fetch(url" in js and "method: 'POST'" not in js and "location.reload" not in js and ".value =" not in js      # read-only, no reload, never edits inputs
    lst, det = c.get("/admin/orders").get_data(as_text=True), c.get("/admin/orders/%d" % oid).get_data(as_text=True)
    assert 'data-interval="9000"' in lst and 'data-interval="5000"' in det
    for html in (lst, det):
        assert "<progress" not in html and "width:" not in re.sub(r"<style.*?</style>", "", html, flags=re.S).replace("max-width", "")    # no fake percentage bars
    assert "实时进度" in det and 'class="stages"' in det and "页面会自动更新" in det

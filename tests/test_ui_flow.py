import json
import re

from app import fulfillment, humanize, payments, settings, upstream_sync
from app.migrate import migrate

from .conftest import AUTH
from .test_recharge import buy_recharge, pay

TECH = ["planId", "paymentSource", "authSessionJson", "clientOrderId", "cardPlatformId", "automate_", "card_platform_",
        "HTTP 4", "HTTP 5", "manual_review", "auto_card"]


def visible(html):
    """What a person sees: drop collapsed technical blocks, scripts, styles and attributes."""
    html = re.sub(r"<details.*?</details>", "", html, flags=re.S)
    html = re.sub(r"<(script|style).*?</\1>", "", html, flags=re.S)
    return re.sub(r"<[^>]+>", " ", html)


def admin_client(env):
    env.make_user("admin@example.com", admin=True)
    c = env.app.test_client()
    env.login(c, "admin@example.com")
    return c, env.csrf(c)


def test_public_pages_have_no_login_or_register(env):
    pid = env.card_product(2)
    env.api_product()
    c = env.app.test_client()
    for url in ("/", "/help", "/orders", "/product/%d" % pid):
        body = c.get(url).data.decode()
        assert "/login" not in body and "/register" not in body, url
        assert "注册" not in visible(body), url
    assert c.get("/register").status_code == 404


def test_static_assets_use_cache_busting_versions(env):
    body = env.app.test_client().get("/").data.decode()
    assert re.search(r"/static/shop\.css\?v=\d+", body)
    assert re.search(r"/static/shop\.js\?v=\d+", body)


def test_guest_buys_without_any_account_main_flow(env):
    pid = env.api_product()
    c = env.app.test_client()
    page = c.get("/product/%d" % pid).data.decode()
    assert "选择商品" in page and "填写资料" in page and "确认订单" in page and "支付宝付款" in page and "自动充值" in page and "查看结果" in page
    r = c.post("/product/%d/confirm" % pid, data={"quantity": "1", "region": "PH", "auth_json": AUTH, "lookup_email": "buyer@example.com",
                                                  "_csrf": env.csrf(c)})
    body = r.data.decode()
    assert r.status_code == 200 and "确认并去付款" in body and "菲律宾" in body and "待充值账号" in body and "buyer@example.com" in body
    assert "AT-SECRET-TOKEN" not in visible(body)
    assert env.core.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0        # confirm does not create an order
    r = c.post("/product/%d/buy" % pid, data={"quantity": "1", "region": "PH", "auth_json": AUTH, "lookup_email": "buyer@example.com",
                                              "_csrf": env.csrf(c)})
    assert r.status_code == 302 and "/pay/" in r.headers["Location"] and "t=" in r.headers["Location"]


def test_confirm_keeps_what_the_buyer_typed_and_explains_problem(env):
    pid = env.api_product()
    c = env.app.test_client()
    r = c.post("/product/%d/confirm" % pid, data={"quantity": "1", "region": "PH", "auth_json": '{"accessToken":"x"}',
                                                  "lookup_email": "buyer@example.com", "_csrf": env.csrf(c)})
    body = r.data.decode()
    assert r.status_code == 200 and ("没有识别到充值账号" in body or "无法确认要充值的账号" in body or "缺少" in body)
    assert "authSessionJson" not in visible(body) and "JSON" not in visible(body)
    assert "buyer@example.com" in body                      # form is re-filled


def test_order_lookup_needs_number_and_email_and_never_shows_card_keys(env):
    pid = env.card_product(2)
    c = env.app.test_client()
    loc = env.buy_card(c, pid)
    no = env.order_no_from(loc)
    env.alipay.pay(no)
    env.post_notify(env.alipay.notify_form(no))
    other = env.app.test_client()
    tok = env.csrf(other)
    r = other.post("/orders", data={"order_no": no, "email": "wrong@example.com", "_csrf": tok}, follow_redirects=True)
    assert "没有找到匹配的订单" in r.data.decode()
    assert other.get("/orders/" + no).status_code == 404
    r = other.post("/orders", data={"order_no": no.lower(), "email": "BUYER@example.com", "_csrf": tok}, follow_redirects=True)
    body = r.data.decode()
    assert r.status_code == 200 and "付款成功" in body
    assert not re.search(r"CARD-\d+-\d+-SECRET", body)                       # lookup proves identity, not key ownership
    assert "专属链接" in visible(body)


def test_lookup_is_rate_limited(env):
    c = env.app.test_client()
    tok = env.csrf(c)
    hits = [c.post("/orders", data={"order_no": "X", "email": "a@b.co", "_csrf": tok}, follow_redirects=True).data.decode() for _ in range(13)]
    assert any("查询太频繁" in h for h in hits)




def test_real_upstream_region_objects_and_plan_allowlist_are_supported():
    data = {
        "paymentSources": [{"id": "auto_card", "label": "自动选卡"}],
        "plans": [
            {"id": "plus-monthly", "name": "ChatGPT Plus"},
            {"id": "pro5x-monthly", "name": "ChatGPT Pro 5x"},
            {"id": "pro20x-direct-monthly", "name": "ChatGPT Pro 20x"},
            {"id": "promax-monthly", "name": "ChatGPT Pro Max"},
            {"id": "go-monthly", "name": "ChatGPT Go"},
        ],
        "planRegions": {
            "plus-monthly": [{"code": "PH", "label": "菲律宾"}, {"code": "US", "label": "美区"}],
            "pro5x-monthly": [{"code": "EG", "label": "埃及"}],
            "pro20x-direct-monthly": [{"code": "PH", "label": "菲律宾"}],
            "promax-monthly": [{"code": "SG", "label": "新加坡"}],
            "go-monthly": [{"code": "PH", "label": "菲律宾"}],
        },
    }
    plans, auto_open = upstream_sync.parse_config(data)
    assert auto_open is True
    assert [p["plan_id"] for p in plans] == [
        "plus-monthly", "pro5x-monthly", "pro20x-direct-monthly", "promax-monthly"
    ]
    assert plans[0]["regions"] == ["PH", "US"]
    assert all(p["usable"] for p in plans)


def test_sensitive_checkout_responses_are_not_cached(env):
    pid = env.api_product()
    c = env.app.test_client()
    r = c.post("/product/%d/confirm" % pid, data={
        "quantity": "1", "region": "PH", "auth_json": AUTH,
        "lookup_email": "buyer@example.com", "_csrf": env.csrf(c),
    })
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-store"
    assert r.headers["Referrer-Policy"] == "no-referrer"






def test_connection_test_and_status_cards_use_plain_words(env):
    c, tok = admin_client(env)
    r = c.post("/admin/upstream/test", data={"_csrf": tok}, follow_redirects=True).data.decode()
    assert "连接成功" in r
    v = visible(r)
    assert "已连接" in v and "已保存" in v and "自动选择" in v and "已启用" in v
    env.upstream.key = "ANOTHER"
    r = c.post("/admin/upstream/test", data={"_csrf": tok}, follow_redirects=True).data.decode()
    assert "连接失败" in r and "密钥无效" in r and "未连接" in visible(r)
    assert "automate_key_invalid" not in visible(r) and "401" not in visible(r)
    env.upstream.config_error = 503
    env.upstream.key = "KEY-123"
    r = c.post("/admin/upstream/sync", data={"_csrf": tok}, follow_redirects=True).data.decode()
    assert "暂时出错" in r and "503" not in visible(r) and "已保留上一次的成本" in r


def test_enabling_requires_key_and_relationship_is_explained(env):
    settings.save_upstream(env.core, False, "https://pro20x.com/api/v3/automate", "", "")
    c, tok = admin_client(env)
    page = c.get("/admin/upstream").data.decode()
    assert "先保存密钥，再打开「启用自动充值」" in page and "未启用" in visible(page)
    env.core.conn.execute("UPDATE upstream_config SET api_key_enc=''")
    r = c.post("/admin/upstream", data={"_csrf": tok, "enabled": "1", "base_url": "https://pro20x.com/api/v3/automate"}, follow_redirects=True).data.decode()
    assert "启用前请填写" in r


def test_errors_are_translated_and_codes_hidden_in_admin(env):
    env.upstream.script = ["reject:automate_points_insufficient:402"]
    no, _ = buy_recharge(env)
    pay(env, no)
    f = env.ful(no)
    assert f["reason"] == "上游账户积分不足，请先充值积分。" and f["status"] == "blocked"
    c, tok = admin_client(env)
    oid = env.order(no)["id"]
    detail = c.get("/admin/orders/%d" % oid).data.decode()
    v = visible(detail)
    assert "上游账户积分不足，请先充值积分。" in v and "重新尝试充值" in v and "需要处理" in v
    for t in TECH:
        assert t not in v, t
    assert "automate_points_insufficient" in detail                       # kept, but inside 技术详情
    for url in ("/admin/", "/admin/orders", "/admin/orders?view=attention"):
        assert "automate_" not in visible(c.get(url).data.decode()), url


def test_plain_translations_for_documented_codes():
    assert humanize.explain("card_platform_no_eligible_card") == "卡台目前没有可用的支付卡，请检查卡台库存。"
    assert humanize.explain("automate_plan_region_not_allowed") == "该套餐暂不支持用户选择的地区。"
    assert "积分不足" in humanize.explain("automate_points_insufficient")
    assert "401" not in humanize.explain("", 401) and "暂时出错" in humanize.explain("", 502)
    assert humanize.region_name("ph") == "菲律宾" and humanize.region_name("ZZ") == "ZZ"


def test_fulfillment_reasons_never_contain_technical_words(env):
    cases = [["http500"], ["timeout_before"] * 8, ["timeout_after"], ["reject:automate_card_invalid:400"], ["reject:automate_plan_region_not_allowed:400"]]
    for script in cases:
        env.upstream.script = list(script)
        if script == ["timeout_after"]:
            env.upstream.list_shape = "unknown"
        no, _ = buy_recharge(env)
        pay(env, no)
        for _ in range(8):
            env.t[0] += 700
            from app.worker import sweep_once
            sweep_once(env.core)
        reason = env.ful(no)["reason"]
        for t in TECH + ["HTTP", "clientOrderId"]:
            assert t not in reason, (script, reason)
        env.upstream.list_shape = "tasks"
        env.t[0] += 15 * 60 * 60                       # keep account throttles out of the way


def test_site_content_controls_footer_help_and_support(env):
    c = env.app.test_client()
    home = c.get("/").data.decode()
    assert "/terms" not in home and "/privacy" not in home and "客服微信" not in home
    adm, tok = admin_client(env)
    r = adm.post("/admin/site", data={"_csrf": tok, "site_name": "测试店", "support_wechat": "kefu_001", "support_hours": "10:00-22:00",
                                      "terms_text": "条款正文", "faq": "能退款吗？\n看规则。", "refund_policy": "失败原路退回"}, follow_redirects=True)
    assert "已保存" in r.data.decode()
    home = c.get("/").data.decode()
    assert "测试店" in home and "kefu_001" in home and "/terms" in home and "/privacy" not in home and "开通失败可退款" in home
    help_ = c.get("/help").data.decode()
    assert "能退款吗？" in help_ and "失败原路退回" in help_ and "10:00-22:00" in help_
    assert "条款正文" in c.get("/terms").data.decode()


def test_admin_pages_show_no_technical_words_outside_details(env):
    c, tok = admin_client(env)
    c.post("/admin/upstream/sync", data={"_csrf": tok})
    env.api_product()
    for url in ("/admin/", "/admin/upstream", "/admin/products", "/admin/products/new", "/admin/alipay", "/admin/site", "/admin/orders"):
        v = visible(c.get(url).data.decode())
        for t in ("planId", "paymentSource", "authSessionJson", "clientOrderId", "cardPlatformId", "automate_"):
            assert t not in v, (url, t)


def test_migrations_are_idempotent(env):
    assert migrate(env.db_path, verbose=False) == []


def test_card_product_wizard_is_plain_and_creates_card_product(env):
    c, tok = admin_client(env)
    wiz = c.get("/admin/products/new").data.decode()
    assert "创建卡密商品" in wiz and "plus-monthly" not in visible(wiz)
    r = c.post("/admin/products/new", data={"_csrf": tok, "name": "测试卡", "price": "9.9", "max_per_order": "3", "enabled": "1"})
    assert r.status_code == 302
    p = env.core.conn.execute("SELECT * FROM products").fetchone()
    assert (p["type"], p["price_cents"], p["enabled"]) == ("card", 990, 1)
    r = c.post("/admin/products/new", data={"_csrf": tok, "name": "", "price": "9.9"})
    assert "商品名称不能为空" in r.data.decode()

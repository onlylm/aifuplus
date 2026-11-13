"""Display-layer tests for the checkout dialog (markup, privacy of what the API shows, mobile CSS contract).
Payment-safety / upstream-call-count tests belong to the business-logic owner (see HANDOFF_COMPOSER_PAYMENT_SAFETY.md)."""
import json
import logging
import re
from pathlib import Path

from app import pricing_service

from .conftest import AUTH
from .test_pricing import PLUS, PRO5, product_for, ready, var

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
FULL_EMAIL = "buyer@example.com"                      # the address inside the shared AUTH fixture (fake)


def post(c, env, url, body):
    return c.post(url, data=json.dumps(body), content_type="application/json", headers={"X-CSRF-Token": env.csrf(c)})


def valid_body(env, region="PH", **over):
    b = {"product_id": product_for(env, PLUS)["id"], "region": region, "contact_email": "contact@example.com", "session": AUTH}
    b.update(over)
    return b


def test_home_cards_are_minimal_and_open_the_dialog(env):
    ready(env)
    home = env.app.test_client().get("/").data.decode()
    assert "付款成功后，系统自动完成支付并把套餐充值到你提供的账号。" not in home
    cards = re.findall(r'<article class="plan".*?</article>', home, flags=re.S)
    assert len(cards) == 1                                              # only Plus has something on sale in this fixture
    c = cards[0]
    assert "<h3>ChatGPT Plus</h3>" in c and "自动充值" in c and "立即充值" in c and "¥" in c
    assert 'class="tag"' not in c and "<p>" not in c                    # no region chips, no description paragraphs
    pid = product_for(env, PLUS)["id"]
    assert 'data-checkout="%d"' % pid in c
    assert 'href="/product/%d"' % pid in c                              # no-JS fallback keeps working
    for need in ('id="co"', 'role="dialog"', 'aria-modal="true"', 'id="co-close"', 'checkout.js', 'id="plans-data"'):
        assert need in home, need
    for step in ("填写资料", "确认订单", "支付订单", "充值结果"):
        assert step in home
    assert "确认并去付款" in (STATIC / "checkout.js").read_text(encoding="utf-8")
    assert "<h3>ChatGPT Pro 5x</h3>" not in home                        # nothing sellable -> no card


def test_only_four_gpt_cards_on_the_home_page(env):
    ready(env)
    env.card_product(2)                                                  # a card-key product must not appear next to them
    home = env.app.test_client().get("/").data.decode()
    assert "卡密A" not in home


def test_dialog_markup_has_the_copy_the_brief_asks_for(env):
    ready(env)
    home = env.app.test_client().get("/").data.decode()
    for text in ("套餐地区", "联系邮箱", "提交充值凭证", "不一定是 ChatGPT 登录邮箱", "已识别充值账号", "充值凭证完整", "可以创建订单",
                 "怎么获取充值凭证？", "我已确认充值账号、套餐地区和付款金额无误", "待充值账号", "我已完成付款" if False else "支付订单"):
        assert text in home, text
    js = (STATIC / "checkout.js").read_text(encoding="utf-8")
    for text in ("下一步：确认订单", "确认并去付款", "我已完成付款", "重新获取二维码", "联系客服", "查看订单进度", "充值完成", "正在充值"):
        assert text in js, text
    visible = re.sub(r"<script.*?</script>", "", home, flags=re.S)
    for tech in ("authSessionJson", "planId", "paymentSource", "accessToken", "JSON"):
        assert tech not in visible, tech


def test_plans_api_lists_only_open_regions_and_follows_changes(env):
    ready(env)
    c = env.app.test_client()
    plans = c.get("/api/checkout/plans").get_json()["plans"]
    assert [p["name"] for p in plans] == ["ChatGPT Plus"]
    regions = {r["code"]: r for r in plans[0]["regions"]}
    assert set(regions) == {"PH", "US"} and regions["PH"]["price_text"] == "138.24" and regions["US"]["price_text"] == "120.00"
    assert regions["PH"]["label"] == "菲律宾"
    pricing_service.toggle_variant(env.core, "t", var(env, PLUS, "US")["id"], False)
    plans = c.get("/api/checkout/plans").get_json()["plans"]
    assert {r["code"] for r in plans[0]["regions"]} == {"PH"}
    assert "cost" not in json.dumps(plans) and "profit" not in json.dumps(plans) and "plan_id" not in json.dumps(plans)


def test_valid_credential_shows_the_full_account_email_only_in_the_secure_flow(env):
    ready(env)
    c = env.app.test_client()
    r = post(c, env, "/api/checkout/validate", valid_body(env))
    d = r.get_json()
    assert r.status_code == 200 and d["ok"] is True and d["account_email"] == FULL_EMAIL        # full, not b•••••@…
    assert d["contact_email"] == "contact@example.com" and d["amount_text"] == "138.24" and d["region_label"] == "菲律宾"
    assert r.headers["Cache-Control"] == "no-store"
    assert FULL_EMAIL not in r.request.url                                                         # never in a URL
    for k in ("cost", "profit", "plan_id", "upstream"):
        assert k not in json.dumps(d)


def test_public_order_views_stay_masked(env):
    ready(env)
    c = env.app.test_client()
    d = post(c, env, "/api/checkout/order", valid_body(env, confirmed=True)).get_json()
    no, tok = d["order_no"], d["token"]
    assert FULL_EMAIL not in json.dumps(d)                                   # order creation response never repeats the account email
    assert d["account_masked"] == "b•••••@example.com"
    status = c.get("/api/orders/%s/status?t=%s" % (no, tok))
    assert FULL_EMAIL not in status.get_data(as_text=True)
    for url in ("/orders/%s?t=%s" % (no, tok), "/pay/%s?t=%s" % (no, tok)):
        assert FULL_EMAIL not in c.get(url, follow_redirects=True).get_data(as_text=True), url
    assert "b•••••@example.com" in c.get("/pay/%s?t=%s" % (no, tok)).get_data(as_text=True)
    other = env.app.test_client()                                            # the public lookup page proves identity with order no + contact e-mail
    r = other.post("/orders", data={"order_no": no, "email": "contact@example.com", "_csrf": env.csrf(other)}, follow_redirects=True)
    assert FULL_EMAIL not in r.get_data(as_text=True)


def test_no_email_in_credential_means_no_order(env):
    ready(env)
    c = env.app.test_client()
    for session in (json.dumps({"accessToken": "x", "account_id": "a"}), json.dumps({"accessToken": "x", "account_id": "a", "user": {"email": "nope"}}), "{not json", ""):
        r = post(c, env, "/api/checkout/validate", valid_body(env, session=session))
        d = r.get_json()
        assert r.status_code == 400 and d["ok"] is False and d["field"] == "session" and "重新" in d["message"] or "请粘贴" in d["message"]
        r = post(c, env, "/api/checkout/order", valid_body(env, session=session, confirmed=True))
        assert r.status_code == 400 and r.get_json()["ok"] is False
    assert env.core.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    d = post(c, env, "/api/checkout/validate", valid_body(env, session=json.dumps({"accessToken": "x", "account_id": "a"}))).get_json()
    assert "没有识别到充值账号" in d["message"]


def test_closed_region_cannot_be_validated_or_ordered(env):
    ready(env)
    pricing_service.toggle_variant(env.core, "t", var(env, PLUS, "US")["id"], False)
    c = env.app.test_client()
    for url, extra in (("/api/checkout/validate", {}), ("/api/checkout/order", {"confirmed": True})):
        r = post(c, env, url, valid_body(env, region="US", **extra))
        assert r.status_code == 400 and r.get_json()["field"] == "region"
    assert env.core.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_order_needs_the_confirmation_tick_and_sends_nothing_upstream(env):
    ready(env)
    c = env.app.test_client()
    posts = lambda: [r for r in env.upstream.requests if r[0] == "POST"]
    r = post(c, env, "/api/checkout/order", valid_body(env))
    assert r.status_code == 400 and r.get_json()["field"] == "confirm"
    r = post(c, env, "/api/checkout/order", valid_body(env, confirmed="yes"))
    assert r.status_code == 400                                              # only a real boolean true counts
    assert env.core.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    r = post(c, env, "/api/checkout/order", valid_body(env, confirmed=True))
    d = r.get_json()
    assert r.status_code == 200 and d["stage"] == "pay" and d["qr_svg"].startswith("<svg") and d["amount_text"] == "138.24"
    assert env.order(d["order_no"])["payment_status"] == "unpaid" and posts() == []


def test_tampered_prices_names_and_regions_are_ignored(env):
    ready(env)
    c = env.app.test_client()
    body = valid_body(env, confirmed=True, amount=1, amount_cents=1, price="0.01", unit_price_cents=1, plan_name="免费套餐", region_label="伪造", cost=0)
    d = post(c, env, "/api/checkout/order", body).get_json()
    o = env.order(d["order_no"])
    assert o["amount_cents"] == 13824 and o["product_name"] == "ChatGPT Plus" and o["snap_region_label"] == "菲律宾"
    assert env.alipay.trades[d["order_no"]]["amount"] == "138.24"


def test_status_api_stages_use_plain_words(env):
    ready(env)
    c = env.app.test_client()
    d = post(c, env, "/api/checkout/order", valid_body(env, confirmed=True)).get_json()
    no, tok = d["order_no"], d["token"]
    s = c.get("/api/orders/%s/status?t=%s" % (no, tok)).get_json()
    assert s["stage"] == "pay" and s["message"] == "等待付款"
    assert c.get("/api/orders/%s/status" % no).status_code == 404            # no token, no access
    assert c.get("/api/orders/%s/status?t=wrong" % no).status_code == 404
    env.alipay.pay(no)
    env.t[0] += 5                                                              # the "I have paid" button asks Alipay at most every 3 s
    r = c.post("/api/orders/%s/check?t=%s" % (no, tok), headers={"X-CSRF-Token": env.csrf(c)})
    assert r.get_json()["stage"] in ("recharging", "done")
    blob = json.dumps(r.get_json(), ensure_ascii=False)
    for tech in ("blocked", "pending", "processing", "unpaid", "automate_", "HTTP"):
        assert tech not in blob


def test_full_email_never_reaches_logs_or_events(env, caplog, capsys):
    ready(env)
    c = env.app.test_client()
    with caplog.at_level(logging.DEBUG):
        post(c, env, "/api/checkout/validate", valid_body(env))
        post(c, env, "/api/checkout/validate", valid_body(env, session=AUTH.replace('"accessToken"', '"x"')))
        d = post(c, env, "/api/checkout/order", valid_body(env, confirmed=True)).get_json()
        c.get("/api/orders/%s/status?t=%s" % (d["order_no"], d["token"]))
    out = capsys.readouterr()
    assert FULL_EMAIL not in caplog.text and FULL_EMAIL not in out.out + out.err
    ev = " ".join(r["message"] + r["detail"] for r in env.core.conn.execute("SELECT message, detail FROM events"))
    assert FULL_EMAIL not in ev
    js = (STATIC / "checkout.js").read_text(encoding="utf-8")
    assert "console." not in js


def test_checkout_script_privacy_and_keyboard_contract():
    js = (STATIC / "checkout.js").read_text(encoding="utf-8")
    assert "'Escape'" in js and "co-close" in js                                  # Esc and the × button close the dialog
    assert "confirmed: true" in js and "/api/checkout/order" in js and "/api/checkout/validate" in js
    for m in re.finditer(r"store\.set\([^;]*\);", js):                            # the credential is never written to browser storage
        assert "session" not in m.group(0), m.group(0)
    assert "localStorage" not in js
    assert "loading(b, true" in js and "S.busy" in js                             # loading state + double-submit guard


def test_mobile_dialog_css_contract():
    css = (STATIC / "shop.css").read_text(encoding="utf-8")
    m = re.search(r"@media\(max-width:640px\)\{(.*?)\n\}", css, flags=re.S)
    assert m, "mobile rules for the dialog are missing"
    block = m.group(1)
    for need in (".co-dialog{width:100%", "height:100dvh", "border-radius:0", ".co-foot", "position:sticky", "safe-area-inset-bottom",
                 ".co-qr{width:min(260px,72vw)}", ".co-regions{grid-template-columns:1fr 1fr}"):
        assert need in block, need
    assert re.search(r"\.co-input\{[^}]*font-size:16px", css)                     # iOS does not zoom into 16px inputs
    assert "overflow:hidden" in css and "html.co-open" in css                     # page behind the dialog does not scroll
    assert "width=device-width,initial-scale=1" in (STATIC.parent / "templates" / "base.html").read_text(encoding="utf-8")


def test_fallback_confirm_page_shows_full_email_and_requires_the_tick(env):
    ready(env)
    c = env.app.test_client()
    pid = product_for(env, PLUS)["id"]
    r = c.post("/product/%d/confirm" % pid, data={"quantity": "1", "region": "PH", "auth_json": AUTH, "lookup_email": "contact@example.com", "_csrf": env.csrf(c)})
    body = r.get_data(as_text=True)
    assert "待充值账号" in body and FULL_EMAIL in body and 'type="checkbox" required' in body
    assert "我已确认充值账号、套餐地区和付款金额无误" in body

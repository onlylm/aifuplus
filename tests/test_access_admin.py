import re

from app import payments, settings

from .conftest import gen_key, priv_pem, pub_pem

PAT = re.compile(r"CARD-\d+-\d+-SECRET")


def delivered_order(env, user_client=None):
    pid = env.card_product(2)
    c = user_client or env.app.test_client()
    loc = env.buy_card(c, pid)
    no = env.order_no_from(loc)
    env.alipay.pay(no)
    env.post_notify(env.alipay.notify_form(no))
    return no, loc, c


def test_guest_needs_unguessable_token(env):
    no, loc, owner = delivered_order(env)
    qs = loc.partition("?")[2]
    base = "/orders/" + env.order_no_from(loc)
    assert PAT.search(env.view(owner, loc).data.decode())
    stranger = env.app.test_client()
    assert stranger.get(base).status_code == 404                       # no token
    assert stranger.get(base + "?t=wrong").status_code == 404
    assert stranger.get(base + "?t=" + qs[2:][:-1] + "x").status_code == 404
    assert stranger.get(base + "/status").status_code == 404
    assert PAT.search(stranger.get(base + "?" + qs).data.decode())          # holder of the token can view
    assert len(qs) > 40                                                 # token is long / random


def test_logged_in_order_only_visible_to_owner(env):
    env.make_user("a@example.com")
    env.make_user("b@example.com")
    ca, cb = env.app.test_client(), env.app.test_client()
    assert env.login(ca, "a@example.com").status_code == 302
    assert env.login(cb, "b@example.com").status_code == 302
    no, loc, _ = delivered_order(env, ca)
    assert "?t=" not in loc                                             # owned orders carry no token in URL
    assert PAT.search(ca.get("/orders/" + no).data.decode())
    assert cb.get("/orders/" + no).status_code == 404
    assert env.app.test_client().get("/orders/" + no).status_code == 404
    assert cb.get("/orders/" + no + "/status").status_code == 404
    assert no not in cb.get("/orders").data.decode()
    assert no in ca.get("/orders").data.decode()


def test_token_cannot_open_someone_elses_owned_order(env):
    env.make_user("a@example.com")
    ca = env.app.test_client()
    env.login(ca, "a@example.com")
    no, _, _ = delivered_order(env, ca)
    guest_no, guest_loc, _ = delivered_order(env)
    token = guest_loc.split("t=")[1]
    assert env.app.test_client().get("/orders/%s?t=%s" % (no, token)).status_code == 404


def test_card_plaintext_encrypted_in_db_and_masked_in_admin_and_logs(env):
    env.make_user("admin@example.com", admin=True)
    no, loc, c = delivered_order(env)
    code = PAT.search(env.view(c, loc).data.decode()).group(0)
    env.core.conn.execute("PRAGMA wal_checkpoint(FULL)")
    blob = open(env.db_path, "rb").read()
    assert code.encode() not in blob
    adm = env.app.test_client()
    env.login(adm, "admin@example.com")
    pid = env.core.conn.execute("SELECT id FROM products").fetchone()["id"]
    for url in ("/admin/products/%d/cards" % pid, "/admin/orders", "/admin/", "/admin/orders/%d" % env.order(no)["id"]):
        body = adm.get(url).data.decode()
        assert code not in body and not PAT.search(body), url
    assert b"CARD-" not in b"".join(r["message"].encode() for r in env.core.conn.execute("SELECT message FROM events"))


def test_admin_pages_require_admin(env):
    env.make_user("u@example.com")
    c = env.app.test_client()
    assert c.get("/admin/").status_code == 302
    env.login(c, "u@example.com")
    for u in ("/admin/", "/admin/alipay", "/admin/products", "/admin/orders", "/admin/upstream"):
        assert c.get(u).status_code == 403
    assert c.post("/admin/alipay", data={}).status_code == 400            # CSRF enforced


def test_alipay_private_key_never_echoed_and_blank_keeps_it(env):
    env.make_user("admin@example.com", admin=True)
    adm = env.app.test_client()
    env.login(adm, "admin@example.com")
    page = adm.get("/admin/alipay").data.decode()
    assert "BEGIN" not in page.split("name=\"private_key\"")[1].split("</textarea>")[0]
    assert "PRIVATE KEY" not in page and "已保存" in page
    assert "/notify/alipay" in page and "https://shop.example.com/notify/alipay" in page
    stored = env.core.conn.execute("SELECT private_key_enc FROM alipay_config").fetchone()[0]
    assert "PRIVATE KEY" not in stored and stored.startswith("gAAAA")       # Fernet ciphertext
    settings.save_alipay(env.core, True, "production", "2021000000000001", "2088000000000001", "", "",
                         "https://shop.example.com", "20")
    assert env.core.conn.execute("SELECT private_key_enc FROM alipay_config").fetchone()[0] == stored
    assert settings.alipay_client(env.core).gateway.startswith("https://openapi.alipay.com")
    r = adm.post("/admin/alipay/test", data={"_csrf": env.csrf(adm)}, follow_redirects=True)
    assert r.status_code == 200


def test_alipay_config_test_button_detects_wrong_public_key(env):
    env.make_user("admin@example.com", admin=True)
    adm = env.app.test_client()
    env.login(adm, "admin@example.com")
    r = adm.post("/admin/alipay/test", data={"_csrf": env.csrf(adm)}, follow_redirects=True).data.decode()
    assert "配置检查通过" in r
    settings.save_alipay(env.core, True, "sandbox", "2021000000000001", "2088000000000001", "",
                         pub_pem(gen_key()), "https://shop.example.com", "15")
    r = adm.post("/admin/alipay/test", data={"_csrf": env.csrf(adm)}, follow_redirects=True).data.decode()
    assert "验签失败" in r


def test_disabled_alipay_blocks_new_orders_but_still_processes_late_notifications(env):
    pid = env.card_product(2)
    c = env.app.test_client()
    no = env.order_no_from(env.buy_card(c, pid))
    settings.save_alipay(env.core, False, "sandbox", "2021000000000001", "2088000000000001", "", "",
                         "https://shop.example.com", "15")
    r = c.post("/product/%d/buy" % pid, data={"quantity": "1", "lookup_email": "b@example.com", "_csrf": env.csrf(c)})
    assert r.status_code == 200 and "支付暂未开放" in r.data.decode()
    env.alipay.pay(no)
    assert env.post_notify(env.alipay.notify_form(no)).data == b"success"
    assert env.order(no)["payment_status"] == "paid"


def test_admin_import_dedupe_void_and_orders_actions_over_http(env):
    env.make_user("admin@example.com", admin=True)
    adm = env.app.test_client()
    env.login(adm, "admin@example.com")
    tok = env.csrf(adm)
    r = adm.post("/admin/products/new", data={"_csrf": tok, "type": "card", "name": "测试卡", "price": "1.5",
                                              "max_per_order": "3", "enabled": "1"})
    assert r.status_code == 302
    pid = env.core.conn.execute("SELECT id FROM products").fetchone()["id"]
    r = adm.post("/admin/products/%d/cards" % pid, data={"_csrf": tok, "codes": "AAA\nBBB\nAAA\n\n  CCC  \nBBB"},
                 follow_redirects=True).data.decode()
    assert "新增 3" in r and "重复跳过 2" in r
    r = adm.post("/admin/products/%d/cards" % pid, data={"_csrf": tok, "codes": "AAA\nDDD"}, follow_redirects=True).data.decode()
    assert "新增 1" in r and "重复跳过 1" in r
    assert env.core.conn.execute("SELECT COUNT(*) FROM card_keys").fetchone()[0] == 4
    cid = env.core.conn.execute("SELECT id FROM card_keys WHERE status='available' LIMIT 1").fetchone()[0]
    adm.post("/admin/cards/%d/void" % cid, data={"_csrf": tok})
    assert env.core.conn.execute("SELECT status FROM card_keys WHERE id=?", (cid,)).fetchone()[0] == "void"
    # price is computed by the server from the product, never from the client
    c = env.app.test_client()
    r = c.post("/product/%d/buy" % pid, data={"quantity": "2", "price": "0.01", "amount_cents": "1", "lookup_email": "b@example.com", "_csrf": env.csrf(c)})
    assert env.order(env.order_no_from(r.headers["Location"]))["amount_cents"] == 300
    # offline product cannot be bought
    adm.post("/admin/products/%d" % pid, data={"_csrf": tok, "name": "测试卡", "price": "1.5", "max_per_order": "3"})
    r = c.post("/product/%d/buy" % pid, data={"quantity": "1", "lookup_email": "b@example.com", "_csrf": env.csrf(c)})
    assert r.status_code == 404
    assert c.get("/product/%d" % pid).status_code == 404


def test_blocked_order_visible_in_admin_with_reason_and_retry_button(env):
    env.make_user("admin@example.com", admin=True)
    pid = env.card_product(1)
    no = env.order_no_from(env.buy_card(env.app.test_client(), pid))
    payments.close_local(env.core, no, alipay_confirmed=False)
    orders_other = env.core.conn.execute("UPDATE card_keys SET status='void'")      # stock vanishes
    env.alipay.pay(no)
    env.post_notify(env.alipay.notify_form(no))
    adm = env.app.test_client()
    env.login(adm, "admin@example.com")
    dash = adm.get("/admin/").data.decode()
    assert "库存不足" in dash and ("/admin/orders/%d" % env.order(no)["id"]) in dash
    detail = adm.get("/admin/orders/%d" % env.order(no)["id"]).data.decode()
    assert "重新发货" in detail and "迟到付款" in detail
    r = adm.post("/admin/orders/%d/action" % env.order(no)["id"], data={"_csrf": env.csrf(adm), "action": "retry"})
    assert r.status_code == 302

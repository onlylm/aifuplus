import re
import threading

from app import fulfillment, orders, payments
from app.worker import sweep_once

from .conftest import PID, gen_key


def paid_order(env, pid=None, qty=1, client=None):
    pid = pid or env.card_product()
    client = client or env.app.test_client()
    loc = env.buy_card(client, pid, qty)
    no = env.order_no_from(loc)
    env.alipay.pay(no)
    return no, loc, client


def test_normal_scan_pay_delivers_cards_and_reopen_shows_same(env):
    pid = env.card_product(3)
    c = env.app.test_client()
    loc = env.buy_card(c, pid, 2)
    no = env.order_no_from(loc)
    o = env.order(no)
    assert o["amount_cents"] == 1980                     # server computed 9.90 * 2
    assert o["qr_code"].startswith("https://qr.alipay.com/")
    page = c.get(loc).data.decode()
    assert "<svg" in page and "19.80" in page
    assert "等待付款" in page
    env.alipay.pay(no)
    r = env.post_notify(env.alipay.notify_form(no))
    assert (r.status_code, r.data) == (200, b"success")
    assert env.order(no)["payment_status"] == "paid"
    assert env.ful(no)["status"] == "delivered"
    pat = re.compile(r"CARD-\d+-\d+-SECRET")
    first = set(pat.findall(env.view(c, loc).data.decode()))
    assert len(first) == 2
    assert set(pat.findall(env.view(c, loc).data.decode())) == first        # reopening shows the same batch


def test_forged_callback_rejected(env):
    pid = env.card_product()
    c = env.app.test_client()
    no = env.order_no_from(env.buy_card(c, pid))
    env.alipay.pay(no)
    forged = env.alipay.notify_form(no, key=gen_key())            # signed with a different key
    r = env.post_notify(forged)
    assert r.status_code == 400 and r.data == b"failure"
    tampered = env.alipay.notify_form(no)
    tampered["total_amount"] = "0.01"                              # tamper after signing
    assert env.post_notify(tampered).status_code == 400
    no_sign = env.alipay.notify_form(no)
    del no_sign["sign"]
    assert env.post_notify(no_sign).status_code == 400
    assert env.order(no)["payment_status"] == "unpaid"
    assert env.core.conn.execute("SELECT COUNT(*) n FROM payments").fetchone()["n"] == 0


def test_wrong_app_id_or_seller_rejected(env):
    pid = env.card_product()
    no = env.order_no_from(env.buy_card(env.app.test_client(), pid))
    env.alipay.pay(no)
    assert env.post_notify(env.alipay.notify_form(no, app_id="2021999999999999")).status_code == 400
    assert env.post_notify(env.alipay.notify_form(no, seller_id="2088999999999999")).status_code == 400
    assert env.order(no)["payment_status"] == "unpaid"


def test_amount_mismatch_not_confirmed(env):
    pid = env.card_product()
    c = env.app.test_client()
    no = env.order_no_from(env.buy_card(c, pid))
    env.alipay.pay(no)
    r = env.post_notify(env.alipay.notify_form(no, total_amount="0.01"))     # validly signed, wrong amount
    assert r.data == b"success"                                               # ack so Alipay stops retrying
    o = env.order(no)
    assert o["payment_status"] == "unpaid" and "金额不符" in o["attention"] or o["attention"]
    assert env.ful(no)["status"] == "none"
    # reconcile by query also refuses when the trade amount differs from the order amount
    env.alipay.trades[no]["amount"] = "0.01"
    assert payments.check_payment(env.core, no) == "unknown"
    assert env.order(no)["payment_status"] == "unpaid"


def test_non_success_status_ignored_and_unknown_order_acked(env):
    pid = env.card_product()
    no = env.order_no_from(env.buy_card(env.app.test_client(), pid))
    f = env.alipay.notify_form(no, trade_status="WAIT_BUYER_PAY")
    assert env.post_notify(f).data == b"success"
    assert env.order(no)["payment_status"] == "unpaid"
    env.alipay.trades["NOPE"] = {"amount": "1.00", "status": "TRADE_SUCCESS", "trade_no": "T1"}
    assert env.post_notify(env.alipay.notify_form("NOPE")).data == b"success"


def test_duplicate_notifications_fulfil_once(env):
    pid = env.card_product(3)
    c = env.app.test_client()
    loc = env.buy_card(c, pid)
    no = env.order_no_from(loc)
    env.alipay.pay(no)
    form = env.alipay.notify_form(no)
    for _ in range(4):
        assert env.post_notify(form).data == b"success"
    q = env.core.conn
    assert q.execute("SELECT COUNT(*) n FROM payments").fetchone()["n"] == 1
    assert q.execute("SELECT COUNT(*) n FROM card_keys WHERE status='sold'").fetchone()["n"] == 1
    assert q.execute("SELECT COUNT(*) n FROM events WHERE kind='delivered'").fetchone()["n"] == 1


def test_notify_and_query_race_confirm_once(env):
    pid = env.card_product(5)
    c = env.app.test_client()
    no = env.order_no_from(env.buy_card(c, pid))
    env.alipay.pay(no)
    form = env.alipay.notify_form(no)
    errors, results = [], []

    def notify():
        try:
            results.append(env.post_notify(form).data)
        except Exception as e:
            errors.append(e)

    def query():
        try:
            results.append(payments.check_payment(env.core, no))
        except Exception as e:
            errors.append(e)
    ts = [threading.Thread(target=notify) for _ in range(3)] + [threading.Thread(target=query) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors, errors
    q = env.core.conn
    assert q.execute("SELECT COUNT(*) n FROM payments").fetchone()["n"] == 1
    assert q.execute("SELECT COUNT(*) n FROM card_keys WHERE status='sold'").fetchone()["n"] == 1
    assert env.ful(no)["status"] == "delivered"


def test_missed_callback_is_recovered_by_trade_query(env):
    pid = env.card_product()
    no = env.order_no_from(env.buy_card(env.app.test_client(), pid))
    env.alipay.pay(no)                    # buyer paid, but no notification ever arrives
    env.t[0] += 60
    sweep_once(env.core)
    assert env.order(no)["payment_status"] == "paid"
    assert env.ful(no)["status"] == "delivered"
    assert env.core.conn.execute("SELECT source FROM payments").fetchone()["source"] == "query"


def test_last_card_concurrent_purchase_no_oversell(env):
    pid = env.card_product(1)
    results, errors = [], []

    def buy():
        try:
            no, _ = orders.create_order(env.core, pid, 1, lookup_email="t@example.com")
            results.append(no)
        except orders.OrderError as e:
            errors.append(str(e))
    ts = [threading.Thread(target=buy) for _ in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(results) == 1 and len(errors) == 11 and set(errors) == {"库存不足"}
    q = env.core.conn
    assert q.execute("SELECT COUNT(*) n FROM card_keys WHERE status='reserved'").fetchone()["n"] == 1
    assert q.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"] == 1     # failed attempts left no orphan orders


def test_each_card_goes_to_exactly_one_order(env):
    pid = env.card_product(6)
    nos = []

    def buy():
        try:
            nos.append(orders.create_order(env.core, pid, 2, lookup_email="t@example.com")[0])
        except orders.OrderError:
            pass
    ts = [threading.Thread(target=buy) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(nos) == 3
    rows = env.core.conn.execute("SELECT order_id, COUNT(*) n FROM card_keys WHERE status='reserved' GROUP BY order_id").fetchall()
    assert sorted(r["n"] for r in rows) == [2, 2, 2]


def test_unpaid_order_expires_closes_at_alipay_and_releases_stock(env):
    pid = env.card_product(1)
    c = env.app.test_client()
    no = env.order_no_from(env.buy_card(c, pid))
    assert env.core.conn.execute("SELECT status FROM card_keys").fetchone()["status"] == "reserved"
    try:
        orders.create_order(env.core, pid, 1, lookup_email="t@example.com")
        assert False, "should be sold out while reserved"
    except orders.OrderError:
        pass
    env.t[0] += 15 * 60 + 100
    sweep_once(env.core)
    assert env.order(no)["payment_status"] == "closed"
    assert env.alipay.trades[no]["status"] == "TRADE_CLOSED"
    assert env.core.conn.execute("SELECT status FROM card_keys").fetchone()["status"] == "available"
    orders.create_order(env.core, pid, 1, lookup_email="t@example.com")       # stock is back


def test_pay_between_query_and_close_is_not_lost(env):
    pid = env.card_product(1)
    no = env.order_no_from(env.buy_card(env.app.test_client(), pid))
    env.t[0] += 15 * 60 + 100
    real_query = env.alipay._query
    state = {"n": 0}

    def racing_query(biz, form):
        r = real_query(biz, form)
        state["n"] += 1
        if state["n"] == 1:                 # buyer pays right after our first query saw WAIT_BUYER_PAY
            env.alipay.pay(no)
        return r
    env.alipay._query = racing_query
    sweep_once(env.core)                     # query(WAIT) -> close(refused: already paid) -> re-query(SUCCESS)
    assert env.order(no)["payment_status"] == "paid"
    assert env.ful(no)["status"] == "delivered"


def test_late_payment_after_local_close_and_no_stock_keeps_payment_and_shows_reason(env):
    pid = env.card_product(1)
    c = env.app.test_client()
    no = env.order_no_from(env.buy_card(c, pid))
    payments.close_local(env.core, no, alipay_confirmed=False)       # closed without Alipay confirmation
    other, _ = orders.create_order(env.core, pid, 1, lookup_email="t@example.com")                  # someone else takes the freed card
    env.alipay.pay(no)
    env.post_notify(env.alipay.notify_form(no))
    o = env.order(no)
    assert o["payment_status"] == "paid" and o["late_payment"] == 1
    f = env.ful(no)
    assert f["status"] == "blocked" and f["reason_code"] == "out_of_stock" and "库存不足" in f["reason"]
    assert env.core.conn.execute("SELECT COUNT(*) n FROM payments").fetchone()["n"] == 1
    # the other (unpaid) order is closed -> card freed -> sweeper auto-delivers the paid late order
    payments.close_local(env.core, other, alipay_confirmed=True)
    sweep_once(env.core)
    assert env.ful(no)["status"] == "delivered"


def test_late_payment_with_stock_is_delivered(env):
    pid = env.card_product(2)
    no = env.order_no_from(env.buy_card(env.app.test_client(), pid))
    payments.close_local(env.core, no, alipay_confirmed=False)
    env.alipay.pay(no)
    env.post_notify(env.alipay.notify_form(no))
    assert env.order(no)["late_payment"] == 1 and env.ful(no)["status"] == "delivered"


def test_precreate_rejected_by_alipay_closes_order_and_releases(env):
    pid = env.card_product(1)
    env.alipay.precreate_error = "ACQ.INVALID_PARAMETER"
    c = env.app.test_client()
    r = c.post("/product/%d/buy" % pid, data={"quantity": "1", "lookup_email": "b@example.com", "_csrf": env.csrf(c)})
    assert r.status_code == 302 and "/product/" in r.headers["Location"]
    assert env.core.conn.execute("SELECT status FROM card_keys").fetchone()["status"] == "available"


def test_precreate_timeout_keeps_order_and_retry_gets_qr(env):
    pid = env.card_product(1)
    env.alipay.fail = ["alipay.trade.precreate"]
    c = env.app.test_client()
    loc = env.buy_card(c, pid)
    no = env.order_no_from(loc)
    assert env.order(no)["qr_code"] == "" and env.order(no)["payment_status"] == "unpaid"
    qs = loc.partition("?")[2]
    r = c.post("/orders/%s/qr?%s" % (no, qs), data={"_csrf": env.csrf(c)})
    assert r.status_code == 302
    assert env.order(no)["qr_code"]


def test_payment_fails_to_ship_keeps_payment_and_admin_can_retry(env, monkeypatch):
    pid = env.card_product(1)
    no = env.order_no_from(env.buy_card(env.app.test_client(), pid))
    env.alipay.pay(no)
    boom = {"on": True}
    real = fulfillment.deliver_cards

    def flaky(core, order_id):
        if boom["on"]:
            raise RuntimeError("db hiccup")
        return real(core, order_id)
    monkeypatch.setattr(fulfillment, "deliver_cards", flaky)
    assert env.post_notify(env.alipay.notify_form(no)).data == b"success"      # payment still acknowledged
    assert env.order(no)["payment_status"] == "paid"
    assert env.ful(no)["status"] == "pending"                                   # not lost
    boom["on"] = False
    sweep_once(env.core)
    assert env.ful(no)["status"] == "delivered"
    assert env.core.conn.execute("SELECT COUNT(*) n FROM card_keys WHERE status='sold'").fetchone()["n"] == 1
    assert fulfillment.admin_retry(env.core, env.order(no)["id"]) is False       # already delivered: no-op

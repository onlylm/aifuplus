"""Release-gate tests owned by Codex: payment evidence, worker-only recharge and sensitive API caching."""

import json

from app.worker import sweep_once

from .conftest import AUTH
from .test_recharge import buy_recharge


def _posts(env):
    return [r for r in env.upstream.requests if r[0] == "POST"]


def test_forged_paid_flag_without_payment_record_never_recharges(env):
    no, _ = buy_recharge(env)
    oid = env.order(no)["id"]
    env.core.conn.execute("UPDATE orders SET payment_status='paid' WHERE id=?", (oid,))
    env.core.conn.execute("UPDATE fulfillments SET status='pending', next_attempt_at=0 WHERE order_id=?", (oid,))

    sweep_once(env.core)

    assert _posts(env) == []
    f = env.ful(no)
    assert f["status"] == "blocked" and f["reason_code"] == "payment_not_confirmed"


def test_recharge_waits_for_worker_and_duplicate_notify_is_idempotent(env):
    no, _ = buy_recharge(env)
    env.alipay.pay(no)
    form = env.alipay.notify_form(no)

    assert env.post_notify(form).data == b"success"
    assert env.order(no)["payment_status"] == "paid"
    assert _posts(env) == []

    sweep_once(env.core)
    assert len(_posts(env)) == 1 and env.upstream.created == 1

    for _ in range(3):
        assert env.post_notify(form).data == b"success"
        sweep_once(env.core)
    assert len(_posts(env)) == 1 and env.upstream.created == 1


def test_wrong_amount_never_reaches_worker_submission(env):
    no, _ = buy_recharge(env)
    env.alipay.pay(no)

    assert env.post_notify(env.alipay.notify_form(no, total_amount="0.01")).data == b"success"
    sweep_once(env.core)

    assert env.order(no)["payment_status"] == "unpaid"
    assert _posts(env) == []


def test_guest_order_status_api_is_not_cacheable(env):
    client = env.app.test_client()
    response = client.post(
        "/api/checkout/order",
        data=json.dumps({"product_id": env.api_product(), "region": "PH", "session": AUTH,
                         "contact_email": "buyer@example.com", "confirmed": True}),
        content_type="application/json",
        headers={"X-CSRF-Token": env.csrf(client)},
    )
    data = response.get_json()
    assert response.status_code == 200 and data["ok"] is True

    status = client.get("/api/orders/%s/status?t=%s" % (data["order_no"], data["token"]))

    assert status.status_code == 200
    assert status.headers["Cache-Control"] == "no-store"
    assert status.headers["Referrer-Policy"] == "no-referrer"

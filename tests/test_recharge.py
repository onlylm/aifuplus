import json

from app import fulfillment, settings
from app.worker import sweep_once

from .conftest import AUTH


def buy_recharge(env, pid=None, region="PH", auth=AUTH):
    pid = pid or env.api_product()
    c = env.app.test_client()
    r = c.post("/product/%d/buy" % pid, data={"quantity": "1", "region": region, "auth_json": auth,
                                              "lookup_email": "buyer@example.com", "_csrf": env.csrf(c)})
    assert r.status_code == 302, r.data
    no = env.order_no_from(r.headers["Location"])
    return no, c


def pay(env, no):
    """Buyer pays (notification arrives). Recharges are submitted upstream only by the worker, so run one worker tick."""
    env.alipay.pay(no)
    assert env.post_notify(env.alipay.notify_form(no)).data == b"success"
    sweep_once(env.core)


def coid(no, n=1):
    return "%s-%d" % (no, n)


def test_input_validation_and_no_order_on_bad_input(env):
    pid = env.api_product()
    c = env.app.test_client()
    for data in ({"region": "XX", "auth_json": AUTH}, {"region": "PH", "auth_json": "not json"},
                 {"region": "PH", "auth_json": json.dumps({"accessToken": "x"})}):
        r = c.post("/product/%d/buy" % pid, data=dict(data, quantity="1", lookup_email="b@example.com", _csrf=env.csrf(c)))
        assert r.status_code == 200 and "请" in r.data.decode()          # re-shows the form with a plain-language hint
    assert env.core.conn.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"] == 0


def test_not_submitted_before_payment_and_secrets_encrypted(env):
    no, _ = buy_recharge(env)
    assert env.upstream.requests == []
    raw = open(env.db_path, "rb").read() + open(str(env.db_path) + "-wal", "rb").read()
    assert b"AT-SECRET-TOKEN" not in raw


def test_paid_then_recharged_via_auto_card_and_secret_purged(env):
    no, _ = buy_recharge(env)
    pay(env, no)
    f = env.ful(no)
    assert f["status"] == "pending" and f["upstream_status"] == "queued"
    sent = env.upstream.tasks[coid(no)]["body"]
    assert sent["paymentSource"] == "auto_card" and sent["planId"] == "plus-monthly"
    assert sent["checkoutCountry"] == "PH" and sent["authSessionJson"]["account_id"] == "acc-1"
    assert "card" not in sent                                    # never sends/stores card data
    env.upstream.set_status(coid(no), "running")
    env.t[0] += 10
    sweep_once(env.core)
    assert env.ful(no)["upstream_status"] == "running" and env.ful(no)["status"] == "pending"
    env.upstream.set_status(coid(no), "succeeded")
    env.t[0] += 10
    sweep_once(env.core)
    f = env.ful(no)
    assert f["status"] == "delivered" and f["upstream_task_id"] == "AUTO-0001"
    assert env.order(no)["recharge_input_enc"] == ""             # credentials wiped after success
    assert env.upstream.created == 1


def test_card_platform_id_is_forwarded_when_configured(env):
    settings.save_upstream(env.core, True, "https://pro20x.com/api/v3/automate", "", "cp_01")
    no, _ = buy_recharge(env)
    pay(env, no)
    assert env.upstream.tasks[coid(no)]["body"]["cardPlatformId"] == "cp_01"


def test_timeout_after_upstream_created_task_is_found_by_query_not_resubmitted(env):
    env.upstream.script = ["timeout_after"]
    no, _ = buy_recharge(env)
    pay(env, no)
    assert env.upstream.created == 1
    f = env.ful(no)
    assert f["upstream_task_id"] == "AUTO-0001"                  # adopted via GET /tasks?clientOrderId=
    posts = [r for r in env.upstream.requests if r[0] == "POST"]
    assert len(posts) == 1
    env.upstream.set_status(coid(no), "succeeded")
    env.t[0] += 10
    sweep_once(env.core)
    assert env.ful(no)["status"] == "delivered" and env.upstream.created == 1


def test_timeout_before_creation_retries_with_same_client_order_id_once(env):
    env.upstream.script = ["timeout_before"]
    no, _ = buy_recharge(env)
    pay(env, no)
    f = env.ful(no)
    assert f["status"] == "pending" and f["upstream_task_id"] == "" and env.upstream.created == 0
    env.t[0] += 15
    sweep_once(env.core)
    assert env.upstream.created == 1
    assert [r[2]["clientOrderId"] for r in env.upstream.requests if r[0] == "POST"] == [coid(no), coid(no)]


def test_http500_is_treated_as_unknown_and_looked_up(env):
    env.upstream.script = ["http500"]
    no, _ = buy_recharge(env)
    pay(env, no)
    assert env.ful(no)["status"] == "pending" and env.upstream.created == 0
    assert any(r[0] == "GET" and r[1] == "/tasks" for r in env.upstream.requests)


def test_unparseable_lookup_goes_to_manual_not_blind_retry(env):
    env.upstream.script = ["timeout_after"]
    env.upstream.list_shape = "unknown"
    no, _ = buy_recharge(env)
    pay(env, no)
    f = env.ful(no)
    assert f["status"] == "manual" and f["reason_code"] == "lookup_unparseable"
    assert len([r for r in env.upstream.requests if r[0] == "POST"]) == 1
    sweep_once(env.core)                                       # sweeper must not touch manual orders
    assert len([r for r in env.upstream.requests if r[0] == "POST"]) == 1


def test_unrecognized_success_response_is_not_treated_as_success(env):
    env.upstream.script = ["weird"]
    no, _ = buy_recharge(env)
    pay(env, no)
    assert env.ful(no)["status"] != "delivered"


def test_retries_exhausted_becomes_manual(env):
    env.upstream.script = ["timeout_before"] * 20
    no, _ = buy_recharge(env)
    pay(env, no)
    for _ in range(10):
        env.t[0] += 700
        sweep_once(env.core)
    f = env.ful(no)
    assert f["status"] == "manual" and f["reason_code"] == "retries_exhausted"
    assert env.upstream.created == 0


def test_upstream_failed_task_admin_retry_uses_new_client_order_id(env):
    no, _ = buy_recharge(env)
    pay(env, no)
    env.upstream.set_status(coid(no), "failed", error="automate_card_invalid")
    env.t[0] += 10
    sweep_once(env.core)
    f = env.ful(no)
    assert f["status"] == "failed" and f["upstream_status"] == "failed"
    oid = env.order(no)["id"]
    assert fulfillment.admin_retry(env.core, oid) is True
    assert env.upstream.created == 1                                  # the admin button only queues it...
    sweep_once(env.core)                                              # ...the worker submits
    assert env.upstream.created == 2 and coid(no, 2) in env.upstream.tasks
    env.upstream.set_status(coid(no, 2), "succeeded")
    env.t[0] += 10
    sweep_once(env.core)
    assert env.ful(no)["status"] == "delivered"


def test_manual_review_requires_human_and_retry_only_repolls(env):
    no, _ = buy_recharge(env)
    pay(env, no)
    env.upstream.set_status(coid(no), "manual_review")
    env.t[0] += 10
    sweep_once(env.core)
    assert env.ful(no)["status"] == "manual"
    oid = env.order(no)["id"]
    fulfillment.admin_retry(env.core, oid)
    sweep_once(env.core)
    assert env.upstream.created == 1                              # no duplicate recharge
    assert env.ful(no)["status"] == "manual"
    assert fulfillment.admin_mark(env.core, oid, True) is True
    assert env.ful(no)["status"] == "delivered"


def test_operator_error_blocks_with_reason_then_retry_after_fix(env):
    env.upstream.script = ["reject:card_platform_not_bound:409"]
    no, _ = buy_recharge(env)
    pay(env, no)
    f = env.ful(no)
    assert f["status"] == "blocked" and f["reason_code"] == "card_platform_not_bound" and "卡台" in f["reason"]
    assert env.order(no)["payment_status"] == "paid"
    assert fulfillment.admin_retry(env.core, env.order(no)["id"]) is True
    sweep_once(env.core)
    assert env.upstream.created == 1
    posts = [r[2]["clientOrderId"] for r in env.upstream.requests if r[0] == "POST"]
    assert posts == [coid(no), coid(no)]                         # rejected-without-task => same id (idempotent)


def test_busy_account_is_retried_later_automatically(env):
    env.upstream.script = ["reject:automate_account_busy:409"]
    no, _ = buy_recharge(env)
    pay(env, no)
    assert env.ful(no)["status"] == "pending" and "10 分钟" not in env.ful(no)["reason"]
    env.t[0] += 15
    sweep_once(env.core)
    assert env.upstream.created == 1


def test_upstream_unconfigured_after_payment_is_blocked_and_recoverable(env):
    settings.save_upstream(env.core, False, "https://pro20x.com/api/v3/automate", "", "")
    no, _ = buy_recharge(env)
    pay(env, no)
    f = env.ful(no)
    assert f["status"] == "blocked" and f["reason_code"] == "upstream_unconfigured"
    settings.save_upstream(env.core, True, "https://pro20x.com/api/v3/automate", "", "")
    fulfillment.admin_retry(env.core, env.order(no)["id"])
    sweep_once(env.core)
    assert env.upstream.created == 1


def test_crashed_worker_lock_expires_and_same_id_is_reused(env):
    no, _ = buy_recharge(env)
    env.alipay.pay(no)
    oid = env.order(no)["id"]
    from app import payments
    payments.confirm_payment(env.core, no, "T-1", env.alipay.trades[no]["amount"], "query")
    f = fulfillment._claim(env.core, oid, False)                  # worker claims, then "crashes"
    assert f is not None and fulfillment._claim(env.core, oid, False) is None
    env.t[0] += 200
    sweep_once(env.core)
    assert env.upstream.created == 1

"""Composer: Session account_id normalization + unpaid-before-recharge guarantees."""
import base64
import json

import pytest

from app import orders, session_auth
from app.worker import sweep_once

from .conftest import AUTH
from .test_recharge import buy_recharge, coid, pay


def _b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).rstrip(b"=").decode()


def make_jwt(claims):
    return "%s.%s.sig" % (_b64({"alg": "none", "typ": "JWT"}), _b64(claims))


def session_without_account(**extra):
    data = {"accessToken": "AT-SECRET-TOKEN", "sessionToken": "ST", "user": {"email": "buyer@example.com"}}
    data.update(extra)
    return json.dumps(data)


def test_top_level_account_id_still_works(env):
    data, email = orders.parse_recharge_input(AUTH, core=env.core)
    assert data["account_id"] == "acc-1" and email == "buyer@example.com"


def test_top_level_accountId_still_works(env):
    raw = json.dumps({"accessToken": "AT", "accountId": "acc-camel", "user": {"email": "a@example.com"}})
    data, email = orders.parse_recharge_input(raw, core=env.core, use_upstream=False)
    assert data["account_id"] == "acc-camel" and email == "a@example.com"


def test_jwt_claim_fills_missing_account_id(env):
    token = make_jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acc-jwt-1"}})
    raw = session_without_account(accessToken=token)
    data, email = orders.parse_recharge_input(raw, core=env.core, use_upstream=False)
    assert data["account_id"] == "acc-jwt-1" and email == "buyer@example.com"
    # JWT is discovery only — we still require accessToken + email; identity is not "verified" by JWT alone.
    assert data["accessToken"] == token


def test_account_check_unique_selected_id(env):
    env.upstream.account_check_script = ["ok:acc-check-9"]
    raw = session_without_account()
    data, email = orders.parse_recharge_input(raw, core=env.core)
    assert data["account_id"] == "acc-check-9"
    assert any(r[0] == "POST" and r[1] == "/account-check" for r in env.upstream.requests)
    # Pre-payment check must never create a recharge task.
    assert env.upstream.created == 0
    assert not any(r[0] == "POST" and r[1] == "/tasks" for r in env.upstream.requests)


def test_account_check_failure_creates_no_order(env):
    env.upstream.account_check_script = ["fail"]
    pid = env.api_product()
    before = env.core.conn.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"]
    with pytest.raises(orders.OrderError) as ei:
        orders.create_order(env.core, pid, 1, region="PH", recharge_auth_text=session_without_account(),
                            lookup_email="buyer@example.com")
    assert ei.value.status == "invalid"
    assert "无法确认" in str(ei.value)
    assert env.core.conn.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"] == before
    assert env.upstream.created == 0


def test_account_check_empty_id_creates_no_order(env):
    env.upstream.account_check_script = ["empty"]
    with pytest.raises(orders.OrderError):
        orders.create_order(env.core, env.api_product(), 1, region="PH", recharge_auth_text=session_without_account(),
                            lookup_email="buyer@example.com")
    assert env.core.conn.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"] == 0


def test_account_check_timeout_creates_no_order(env, monkeypatch):
    monkeypatch.setattr(session_auth, "CHECK_TIMEOUT_SEC", 0.2)
    monkeypatch.setattr(session_auth, "CHECK_POLL_SEC", 0.05)
    env.upstream.account_check_script = ["timeout_job"]
    with pytest.raises(orders.OrderError) as ei:
        orders.create_order(env.core, env.api_product(), 1, region="PH", recharge_auth_text=session_without_account(),
                            lookup_email="buyer@example.com")
    assert ei.value.status == "invalid"
    assert env.core.conn.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"] == 0
    assert env.upstream.created == 0


def test_ambiguous_accounts_reject_without_order(env):
    env.upstream.account_check_script = ["ambiguous"]
    with pytest.raises(orders.OrderError) as ei:
        orders.create_order(env.core, env.api_product(), 1, region="PH", recharge_auth_text=session_without_account(),
                            lookup_email="buyer@example.com")
    assert ei.value.status == "ambiguous"
    assert "多个账号" in str(ei.value)
    assert env.core.conn.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"] == 0


def test_nested_multi_account_session_is_ambiguous(env):
    raw = session_without_account(accounts=[{"accountId": "a1"}, {"accountId": "a2"}])
    with pytest.raises(orders.OrderError) as ei:
        orders.parse_recharge_input(raw, core=env.core, use_upstream=False)
    assert ei.value.status == "ambiguous"


def test_identify_success_creates_unpaid_with_zero_upstream_posts(env):
    env.upstream.account_check_script = ["ok:acc-pay-1"]
    no, _ = buy_recharge(env, auth=session_without_account())
    o = env.order(no)
    assert o["payment_status"] == "unpaid"
    assert [r for r in env.upstream.requests if r[0] == "POST" and r[1] == "/tasks"] == []
    assert env.upstream.created == 0
    # Normalized account_id is encrypted into the order payload.
    stored = json.loads(env.core.crypto.decrypt(o["recharge_input_enc"]))
    assert stored["account_id"] == "acc-pay-1"


def test_paid_then_worker_submits_once_and_duplicates_stay_one(env):
    env.upstream.account_check_script = ["ok:acc-pay-2"]
    no, _ = buy_recharge(env, auth=session_without_account())
    # Payment alone (without the pay() helper's worker tick)
    env.alipay.pay(no)
    form = env.alipay.notify_form(no)
    assert env.post_notify(form).data == b"success"
    assert env.upstream.created == 0
    sweep_once(env.core)
    assert env.upstream.created == 1
    assert env.upstream.tasks[coid(no)]["body"]["authSessionJson"]["account_id"] == "acc-pay-2"
    for _ in range(3):
        assert env.post_notify(form).data == b"success"
        sweep_once(env.core)
    assert env.upstream.created == 1


def test_secrets_never_appear_in_db_or_public_lookup(env):
    env.upstream.account_check_script = ["ok:acc-secret"]
    secret = session_without_account(accessToken="AT-SUPER-SECRET-VALUE")
    no, client = buy_recharge(env, auth=secret)
    raw = open(env.db_path, "rb").read()
    assert b"AT-SUPER-SECRET-VALUE" not in raw
    r = client.post("/lookup", data={"order_no": no, "email": "buyer@example.com", "_csrf": env.csrf(client)},
                    follow_redirects=True)
    body = r.data.decode()
    assert "AT-SUPER-SECRET-VALUE" not in body
    assert "acc-secret" not in body

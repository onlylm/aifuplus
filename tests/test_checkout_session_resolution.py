"""TASK_CHECKOUT_SESSION_ACCOUNT_RESOLUTION: frontend gate + backend normalize_session."""
import base64
import json
from pathlib import Path

import pytest

from app import orders, session_auth
from app.worker import sweep_once

from .conftest import AUTH
from .test_checkout_ui import FULL_EMAIL, post, valid_body
from .test_pricing import PLUS, product_for, ready
from .test_recharge import coid

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def _b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).rstrip(b"=").decode()


def make_jwt(claims):
    return "%s.%s.sig" % (_b64({"alg": "none", "typ": "JWT"}), _b64(claims))


def session(**extra):
    data = {"accessToken": "AT-SECRET-TOKEN", "sessionToken": "ST", "user": {"email": FULL_EMAIL}}
    data.update(extra)
    return json.dumps(data)


def test_frontend_next_button_does_not_require_top_level_account_id():
    js = (STATIC / "checkout.js").read_text(encoding="utf-8")
    assert "p.acct" not in js
    assert "canGoNext" in js
    # Gate is email + token + contact + region; account_id is server-side only.
    assert "account_id" not in js or "Do NOT require top-level account_id" in js
    assert "normalize_session" in js or "Do NOT require top-level account_id" in js


def test_validate_top_level_account_id(env):
    ready(env)
    c = env.app.test_client()
    d = post(c, env, "/api/checkout/validate", valid_body(env, session=AUTH)).get_json()
    assert d["ok"] is True and d["account_email"] == FULL_EMAIL and d["status"] == "valid"


def test_validate_top_level_accountId(env):
    ready(env)
    c = env.app.test_client()
    raw = session(accountId="acc-camel")
    d = post(c, env, "/api/checkout/validate", valid_body(env, session=raw)).get_json()
    assert d["ok"] is True and d["account_email"] == FULL_EMAIL
    data, _ = orders.parse_recharge_input(raw, core=env.core, use_upstream=False)
    assert data["account_id"] == "acc-camel"


def test_validate_jwt_fills_missing_account_id(env):
    ready(env)
    c = env.app.test_client()
    token = make_jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acc-jwt-x"}})
    raw = session(accessToken=token)
    d = post(c, env, "/api/checkout/validate", valid_body(env, session=raw)).get_json()
    assert d["ok"] is True and d["account_email"] == FULL_EMAIL
    data, _ = orders.parse_recharge_input(raw, core=env.core, use_upstream=False)
    assert data["account_id"] == "acc-jwt-x"


def test_validate_nested_unique_account_id(env):
    ready(env)
    c = env.app.test_client()
    raw = session(account={"accountId": "acc-nested-1"})
    d = post(c, env, "/api/checkout/validate", valid_body(env, session=raw)).get_json()
    assert d["ok"] is True and d["account_email"] == FULL_EMAIL
    data, _ = orders.parse_recharge_input(raw, core=env.core, use_upstream=False)
    assert data["account_id"] == "acc-nested-1"


def test_validate_account_check_unique(env):
    ready(env)
    env.upstream.account_check_script = ["ok:acc-from-check"]
    c = env.app.test_client()
    d = post(c, env, "/api/checkout/validate", valid_body(env, session=session())).get_json()
    assert d["ok"] is True and d["account_email"] == FULL_EMAIL
    assert any(r[0] == "POST" and r[1] == "/account-check" for r in env.upstream.requests)
    assert env.upstream.created == 0
    assert env.core.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_ambiguous_multi_account_blocks_order(env):
    ready(env)
    env.upstream.account_check_script = ["ambiguous", "ambiguous"]
    c = env.app.test_client()
    raw = session()
    v = post(c, env, "/api/checkout/validate", valid_body(env, session=raw)).get_json()
    assert v["ok"] is False and v.get("status") == "ambiguous"
    o = post(c, env, "/api/checkout/order", valid_body(env, session=raw, confirmed=True)).get_json()
    assert o["ok"] is False
    assert env.core.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert env.upstream.created == 0


def test_account_check_fail_and_timeout_create_no_order(env, monkeypatch):
    ready(env)
    c = env.app.test_client()
    env.upstream.account_check_script = ["fail"]
    assert post(c, env, "/api/checkout/order", valid_body(env, session=session(), confirmed=True)).get_json()["ok"] is False
    assert env.core.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0

    monkeypatch.setattr(session_auth, "CHECK_TIMEOUT_SEC", 0.2)
    monkeypatch.setattr(session_auth, "CHECK_POLL_SEC", 0.05)
    env.upstream.account_check_script = ["timeout_job"]
    assert post(c, env, "/api/checkout/order", valid_body(env, session=session(), confirmed=True)).get_json()["ok"] is False
    assert env.core.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert env.upstream.created == 0


def test_missing_user_email_cannot_create_order(env):
    ready(env)
    c = env.app.test_client()
    raw = json.dumps({"accessToken": "AT", "account_id": "acc-1"})
    d = post(c, env, "/api/checkout/validate", valid_body(env, session=raw)).get_json()
    assert d["ok"] is False and d["field"] == "session"
    assert post(c, env, "/api/checkout/order", valid_body(env, session=raw, confirmed=True)).get_json()["ok"] is False
    assert env.core.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_repeated_validate_creates_neither_order_nor_recharge(env):
    ready(env)
    env.upstream.account_check_script = ["ok:acc-v"] * 5
    c = env.app.test_client()
    body = valid_body(env, session=session())
    for _ in range(5):
        d = post(c, env, "/api/checkout/validate", body).get_json()
        assert d["ok"] is True and d["account_email"] == FULL_EMAIL
    assert env.core.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert not any(r[0] == "POST" and r[1] == "/tasks" for r in env.upstream.requests)


def test_confirm_flow_uses_server_account_email_and_public_stays_masked(env):
    ready(env)
    env.upstream.account_check_script = ["ok:acc-confirm"]
    c = env.app.test_client()
    raw = session()
    v = post(c, env, "/api/checkout/validate", valid_body(env, session=raw)).get_json()
    assert v["account_email"] == FULL_EMAIL
    # Fallback confirm page (no-JS) also shows full email after server parse
    pid = product_for(env, PLUS)["id"]
    page = c.post("/product/%d/confirm" % pid, data={
        "quantity": "1", "region": "PH", "auth_json": AUTH, "lookup_email": "contact@example.com",
        "_csrf": env.csrf(c)}).get_data(as_text=True)
    assert "待充值账号" in page and FULL_EMAIL in page
    assert "我已确认充值账号、套餐地区和付款金额无误" in page

    d = post(c, env, "/api/checkout/order", valid_body(env, session=AUTH, confirmed=True)).get_json()
    assert FULL_EMAIL not in json.dumps(d)
    assert d["account_masked"].startswith("b")
    st = c.get("/api/orders/%s/status?t=%s" % (d["order_no"], d["token"])).get_json()
    assert FULL_EMAIL not in json.dumps(st)
    pay = c.get("/pay/%s?t=%s" % (d["order_no"], d["token"])).get_data(as_text=True)
    assert FULL_EMAIL not in pay and "•••••" in pay


def test_only_worker_submits_upstream_after_pay(env):
    ready(env)
    c = env.app.test_client()
    d = post(c, env, "/api/checkout/order", valid_body(env, confirmed=True)).get_json()
    no = d["order_no"]
    assert [r for r in env.upstream.requests if r[0] == "POST" and r[1] == "/tasks"] == []
    env.alipay.pay(no)
    assert env.post_notify(env.alipay.notify_form(no)).data == b"success"
    assert [r for r in env.upstream.requests if r[0] == "POST" and r[1] == "/tasks"] == []
    # Browser-like status poll must not submit either
    c.get("/api/orders/%s/status?t=%s" % (no, d["token"]))
    assert [r for r in env.upstream.requests if r[0] == "POST" and r[1] == "/tasks"] == []
    sweep_once(env.core)
    assert env.upstream.created == 1
    assert coid(no) in env.upstream.tasks

import json
import re
import secrets
import threading
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from app.security import hash_password

from app import alipay as ali
from app import catalog, create_app, settings
from app.core import Core
from app.migrate import migrate
from app.fx import FxError
from app.upstream import UpstreamTransportError

APP_ID = "2021000000000001"
PID = "2088000000000001"


def gen_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def priv_pem(k):
    return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption()).decode()


def pub_pem(k):
    return ali.public_key_pem(k)


class FakeAlipay:
    """Plays the Alipay gateway: verifies OUR request signature, signs ITS responses."""

    def __init__(self, merchant_pub, alipay_priv):
        self.merchant_pub = merchant_pub
        self.alipay_priv = alipay_priv
        self.trades = {}
        self.lock = threading.RLock()
        self.calls = []
        self.fail = []          # list of method names whose next call raises a transport error
        self.precreate_error = None

    def transport(self, url, form):
        assert ali.rsa2_verify(self.merchant_pub, ali.sign_string(form), form["sign"]), "request signature invalid"
        assert form["app_id"] == APP_ID and form["charset"] == "utf-8" and form["sign_type"] == "RSA2"
        method = form["method"]
        biz = json.loads(form["biz_content"])
        self.calls.append((method, biz))
        if method in self.fail:
            self.fail.remove(method)
            raise ali.AlipayTransportError("boom")
        with self.lock:
            node = getattr(self, "_" + method.split(".")[-1])(biz, form)
        return self._respond(method, node)

    def _respond(self, method, node):
        text = json.dumps(node, ensure_ascii=False, separators=(",", ":"))
        sig = ali.rsa2_sign(self.alipay_priv, text)
        return '{"%s":%s,"sign":"%s"}' % (method.replace(".", "_") + "_response", text, sig)

    def _precreate(self, biz, form):
        if self.precreate_error:
            return {"code": "40004", "msg": "Business Failed", "sub_code": self.precreate_error, "sub_msg": "x"}
        no = biz["out_trade_no"]
        t = self.trades.setdefault(no, {"amount": biz["total_amount"], "status": "WAIT_BUYER_PAY", "trade_no": ""})
        t["expire"] = biz["time_expire"]
        return {"code": "10000", "msg": "Success", "out_trade_no": no, "qr_code": "https://qr.alipay.com/fake_" + no}

    def _query(self, biz, form):
        t = self.trades.get(biz["out_trade_no"])
        if not t:
            return {"code": "40004", "msg": "Business Failed", "sub_code": "ACQ.TRADE_NOT_EXIST", "sub_msg": "交易不存在"}
        return {"code": "10000", "msg": "Success", "out_trade_no": biz["out_trade_no"], "trade_no": t["trade_no"],
                "trade_status": t["status"], "total_amount": t["amount"]}

    def _close(self, biz, form):
        t = self.trades.get(biz["out_trade_no"])
        if not t:
            return {"code": "40004", "msg": "Business Failed", "sub_code": "ACQ.TRADE_NOT_EXIST", "sub_msg": "x"}
        if t["status"] == "TRADE_SUCCESS":
            return {"code": "40004", "msg": "Business Failed", "sub_code": "ACQ.TRADE_STATUS_ERROR", "sub_msg": "x"}
        t["status"] = "TRADE_CLOSED"
        return {"code": "10000", "msg": "Success", "out_trade_no": biz["out_trade_no"]}

    # ---- test helpers
    def pay(self, out_trade_no, amount=None):
        with self.lock:
            t = self.trades[out_trade_no]
            if t["status"] != "TRADE_SUCCESS":
                t["status"] = "TRADE_SUCCESS"
                t["trade_no"] = "2026" + secrets.token_hex(8)
            if amount:
                t["amount"] = amount
            return t["trade_no"]

    def notify_form(self, out_trade_no, key=None, **over):
        t = self.trades[out_trade_no]
        f = {"notify_time": "2026-11-14 12:00:00", "notify_type": "trade_status_sync", "notify_id": secrets.token_hex(8),
             "app_id": APP_ID, "charset": "utf-8", "version": "1.0", "sign_type": "RSA2",
             "trade_no": t["trade_no"], "out_trade_no": out_trade_no, "seller_id": PID,
             "trade_status": t["status"], "total_amount": t["amount"], "receipt_amount": t["amount"]}
        f.update(over)
        f["sign"] = ali.rsa2_sign(key or self.alipay_priv, ali.sign_string(f, exclude=("sign", "sign_type")))
        return f


def default_config():
    """Shaped like the documented /config: planRegions = where a plan is sold, wallet.prices = USD reference cost
    (number or null), regionalDisplayPrices = local-currency display price (must never be used as cost)."""
    plans = ["plus-monthly", "pro5x-monthly", "pro20x-direct-monthly", "promax-monthly",
             "go-monthly", "credits", "team-subscription", "subscription-upgrade", "grok-xpass-monthly"]
    return {
        "success": True,
        "plans": [{"id": p} for p in plans],
        "planRegions": {"plus-monthly": ["PH", "US", "JP"], "pro5x-monthly": ["EG", "JP", "CL"],
                        "pro20x-direct-monthly": ["PH", "SG"], "promax-monthly": ["PH", "US"],
                        "go-monthly": ["PH"], "credits": ["PH"], "team-subscription": ["PH"], "subscription-upgrade": ["PH"],
                        "grok-xpass-monthly": ["US"]},
        "wallet": {"prices": {"plus-monthly": {"PH": 16.0, "US": 20.35, "JP": None},
                              "pro5x-monthly": {"EG": 90.0, "CL": 90.0, "JP": None},
                              "pro20x-direct-monthly": {"PH": 150.0, "SG": None},
                              "promax-monthly": {"PH": None, "US": None},
                              "go-monthly": {"PH": 5.0}}},
        "regionalDisplayPrices": {"plus-monthly": {"PH": 99999, "US": 88888}, "pro5x-monthly": {"EG": 77777}},
        "paymentSources": [{"id": "card", "label": "填卡支付"}, {"id": "auto_card", "label": "自动选卡"}],
    }


class FakeUpstream:
    def __init__(self):
        self.tasks = {}            # clientOrderId -> task
        self.created = 0
        self.script = []           # behaviors for upcoming POST /tasks
        self.requests = []
        self.list_shape = "tasks"
        self.key = "KEY-123"
        self.config_error = 0
        self.config_payload = default_config()
        self.account_checks = {}   # jobId -> job
        self.account_check_created = 0
        # Next POST /account-check behaviors: "ok", "ok:ACC", "ambiguous", "empty", "fail", "timeout_job"
        self.account_check_script = []

    def transport(self, method, url, headers, body, timeout):
        if headers["X-Automate-Key"] != self.key:
            return 401, json.dumps({"success": False, "code": "automate_key_invalid", "message": "bad key", "requestId": "R"})
        path = urlparse(url).path.replace("/api/v3/automate", "")
        q = parse_qs(urlparse(url).query)
        data = json.loads(body) if body else None
        self.requests.append((method, path, data))
        if method == "POST" and path == "/account-check":
            beh = self.account_check_script.pop(0) if self.account_check_script else "ok"
            if beh == "fail":
                return 400, json.dumps({"success": False, "code": "automate_auth_invalid", "message": "bad", "requestId": "R"})
            self.account_check_created += 1
            jid = "ptj_%04d" % self.account_check_created
            if beh.startswith("ok"):
                acc = beh.split(":", 1)[1] if ":" in beh else "acc-from-check"
                result = {"email": "buyer@example.com", "selectedAccountId": acc, "accountCount": 1,
                          "accounts": [{"accountId": acc, "planLabel": "Free", "tier": "free"}]}
                status = "success"
            elif beh == "ambiguous":
                result = {"email": "buyer@example.com", "selectedAccountId": "", "accountCount": 2,
                          "accounts": [{"accountId": "a1"}, {"accountId": "a2"}]}
                status = "success"
            elif beh == "empty":
                result = {"email": "buyer@example.com", "selectedAccountId": "", "accountCount": 0, "accounts": []}
                status = "success"
            elif beh == "timeout_job":
                result = {}
                status = "running"
            else:
                result = {"selectedAccountId": "acc-from-check", "accountCount": 1, "accounts": [{"accountId": "acc-from-check"}]}
                status = "success"
            job = {"id": jid, "type": "account_check", "status": status, "errorCode": "", "result": result}
            self.account_checks[jid] = job
            return 200, json.dumps({"success": True, "job": job, "requestId": "R"})
        if method == "GET" and path.startswith("/account-check/jobs/"):
            jid = path.rsplit("/", 1)[-1]
            job = self.account_checks.get(jid)
            if not job:
                return 404, json.dumps({"success": False, "code": "automate_account_check_job_not_found"})
            # If still running in fake, leave it running (tests that need success use immediate ok).
            return 200, json.dumps({"success": True, "job": job})
        if method == "POST" and path == "/tasks":
            beh = self.script.pop(0) if self.script else "ok"
            if beh == "timeout_before":
                raise UpstreamTransportError("timeout")
            if beh.startswith("reject:"):
                _, code, status = beh.split(":")
                return int(status), json.dumps({"success": False, "code": code, "message": "m", "requestId": "R"})
            if beh == "http500":
                return 500, "oops"
            coid = data["clientOrderId"]
            t = self.tasks.get(coid)
            if t is None:
                self.created += 1
                t = self.tasks[coid] = {"id": "AUTO-%04d" % self.created, "clientOrderId": coid, "status": "queued",
                                        "paymentSource": data["paymentSource"], "body": data}
            if beh == "timeout_after":
                raise UpstreamTransportError("timeout")
            if beh == "weird":
                return 200, json.dumps({"success": True, "hello": "world"})
            return 200, json.dumps({"success": True, "task": self._pub(t), "requestId": "R"})
        if method == "GET" and path == "/tasks":
            coid = q.get("clientOrderId", [""])[0]
            if self.list_shape == "unknown":
                return 200, json.dumps({"success": True, "foo": 1})
            found = [self._pub(t) for c, t in self.tasks.items() if c == coid]
            return 200, json.dumps({"success": True, "tasks": found, "total": len(found)})
        if method == "GET" and path.startswith("/tasks/"):
            tid = path.split("/")[-1]
            for t in self.tasks.values():
                if t["id"] == tid:
                    return 200, json.dumps({"success": True, "task": self._pub(t)})
            return 404, json.dumps({"success": False, "code": "automate_task_not_found"})
        if method == "GET" and path == "/config":
            if self.config_error:
                return self.config_error, "{}"
            return 200, json.dumps(self.config_payload)
        return 404, "{}"

    @staticmethod
    def _pub(t):
        d = {k: v for k, v in t.items() if k != "body"}
        if t.get("error"):
            d["error"] = t["error"]
        return d

    def set_status(self, coid, status, error=None):
        self.tasks[coid]["status"] = status
        if error:
            self.tasks[coid]["error"] = {"code": error, "message": "x"}


class Env:
    pass


@pytest.fixture
def env(tmp_path):
    e = Env()
    e.t = [1_800_000_000]
    e.merchant = gen_key()
    e.alipay_key = gen_key()
    e.alipay = FakeAlipay(e.merchant.public_key(), e.alipay_key)
    e.upstream = FakeUpstream()
    db_path = tmp_path / "t.db"
    migrate(db_path, verbose=False)
    e.fx_rate = [7_200_000]
    e.fx_fail = [False]

    def fx_provider():
        if e.fx_fail[0]:
            raise FxError("provider down")
        return e.fx_rate[0], "测试汇率源"
    e.core = Core(db_path, Fernet.generate_key(), clock=lambda: e.t[0], alipay_transport=e.alipay.transport,
                  upstream_transport=e.upstream.transport, fx_provider=fx_provider)
    settings.save_alipay(e.core, True, "sandbox", APP_ID, PID, priv_pem(e.merchant), pub_pem(e.alipay_key),
                         "https://shop.example.com", "15")
    settings.save_upstream(e.core, True, "https://pro20x.com/api/v3/automate", "KEY-123", "")
    e.app = create_app(core=e.core, secret_key="test-secret")
    e.app.config["TESTING"] = True
    e.db_path = db_path

    def card_product(n_cards=3, price="9.90", name="卡密A", max_per=5):
        pid = catalog.save_product(e.core, None, name, "", "card", price, True, max_per, "", "")
        if n_cards:
            catalog.import_cards(e.core, pid, "\n".join("CARD-%s-%03d-SECRET" % (pid, i) for i in range(n_cards)))
        return pid
    e.card_product = card_product

    def api_product(price="50", plan="plus-monthly", regions="PH,TZ"):
        return catalog.save_product(e.core, None, "Plus 充值", "", "api_recharge", price, True, 1, plan, regions)
    e.api_product = api_product

    def make_user(email, admin=False):
        cur = e.core.conn.execute("INSERT INTO users(email,password_hash,is_admin,created_at) VALUES (?,?,?,?)",
                                  (email, hash_password("password123"), 1 if admin else 0, e.t[0]))
        return cur.lastrowid
    e.make_user = make_user

    def login(client, email, pw="password123"):
        client.get("/login")
        with client.session_transaction() as s:
            tok = s.setdefault("csrf", "tok")
        return client.post("/login", data={"email": email, "password": pw, "_csrf": tok})
    e.login = login

    def csrf(client):
        with client.session_transaction() as s:
            return s.setdefault("csrf", "tok")
    e.csrf = csrf

    def buy_card(client, pid, qty=1):
        r = client.post("/product/%d/buy" % pid, data={"quantity": str(qty), "lookup_email": "buyer@example.com", "_csrf": csrf(client)})
        assert r.status_code == 302, r.data
        return r.headers["Location"]
    e.buy_card = buy_card

    def order_no_from(loc):
        return re.search(r"/(?:pay|orders?)/([A-Z0-9]+)", loc).group(1)
    e.order_no_from = order_no_from

    def view(client, loc):
        return client.get(loc, follow_redirects=True)
    e.view = view

    def post_notify(form):
        c = e.app.test_client()
        return c.post("/notify/alipay", data=form)
    e.post_notify = post_notify

    def ful(order_no):
        return e.core.conn.execute("SELECT f.* FROM fulfillments f JOIN orders o ON o.id=f.order_id WHERE o.order_no=?",
                                   (order_no,)).fetchone()
    e.ful = ful

    def order(order_no):
        return e.core.conn.execute("SELECT * FROM orders WHERE order_no=?", (order_no,)).fetchone()
    e.order = order
    return e


AUTH = json.dumps({"accessToken": "AT-SECRET-TOKEN", "sessionToken": "ST", "account_id": "acc-1",
                   "user": {"email": "buyer@example.com"}})


@pytest.fixture(autouse=True)
def _reset_throttle():
    from app import web
    web._hits.clear()
    yield
    web._hits.clear()

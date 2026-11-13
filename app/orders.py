"""Order creation, access control, QR attachment."""
import hashlib
import hmac
import json
import secrets
import time

from . import alipay as ali
from . import pricing_service, settings
from .db import tx

MAX_AUTH_JSON = 20000


class OrderError(Exception):
    """A problem the buyer can fix. `field` tells the checkout dialog which input to highlight."""

    def __init__(self, msg, field=""):
        super().__init__(msg)
        self.field = field


def _hash_token(t):
    return hashlib.sha256(t.encode()).hexdigest()


def new_order_no(now):
    return "A%s%s" % (time.strftime("%y%m%d%H%M%S", time.gmtime(now)), secrets.token_hex(5).upper())


def parse_recharge_input(auth_text):
    auth_text = (auth_text or "").strip()
    if not auth_text:
        raise OrderError("请粘贴充值凭证", "session")
    if len(auth_text) > MAX_AUTH_JSON:
        raise OrderError("充值凭证过长，请确认复制的是正确内容", "session")
    try:
        data = json.loads(auth_text)
    except ValueError:
        raise OrderError("充值凭证格式不对，请按说明重新复制完整内容", "session")
    if not isinstance(data, dict):
        raise OrderError("充值凭证格式不对，请按说明重新复制完整内容", "session")
    token = data.get("accessToken")
    acct = data.get("account_id") or data.get("accountId")
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    email = user.get("email")
    if not (isinstance(token, str) and token.strip()):
        raise OrderError("充值凭证里缺少账号授权信息，请重新提交有效的充值凭证", "session")
    if not (isinstance(acct, str) and acct.strip()):
        raise OrderError("充值凭证里缺少账号标识，请重新提交有效的充值凭证", "session")
    if not (isinstance(email, str) and "@" in email):
        raise OrderError("充值凭证里缺少账号邮箱，没有识别到充值账号，请重新提交有效的充值凭证", "session")
    return data, email.strip()


EMAIL_RE = __import__("re").compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _recharge_price(conn, p, region):
    """Server-side price for a recharge product + region. Returns (unit_price_fen, variant_row_or_None, region_code).
    Plans that have pricing variants can ONLY be bought in an enabled, priced region; anything else is rejected here,
    so a forged request for a closed region cannot get an order."""
    region = (region or "").strip().upper()
    if pricing_service.has_variants(conn, p["plan_id"]):
        v = pricing_service.variant_for_sale(conn, p["plan_id"], region)
        if v is None:
            raise OrderError("这个地区暂时不能购买，请选择其他地区", "region")
        return v["sale_price_cny"], v, region
    if region not in p["regions"].split(","):
        raise OrderError("请选择有效的充值地区", "region")
    return p["price_cents"], None, region


def _snapshot(c, oid, v):
    c.execute("UPDATE orders SET variant_id=?, snap_plan_label=?, snap_region_label=?, snap_package_cost_cny=?, snap_other_cost_cny=?,"
              " snap_total_cost_cny=?, snap_fx_rate=?, snap_pricing_mode=?, snap_cost_source=?, snap_expected_profit_cny=? WHERE id=?",
              (v["id"], v["plan_label"], v["region_label"], v["effective_package_cost_cny"], v["other_cost_cny"], v["total_cost_cny"],
               v["fx_rate_snapshot"], v["pricing_mode"], v["cost_source"],
               None if v["total_cost_cny"] is None else v["sale_price_cny"] - v["total_cost_cny"], oid))


def create_order(core, product_id, quantity, user_id=None, contact="", recharge_auth_text="", region="", lookup_email=""):
    cfg = settings.get_alipay(core)
    if not cfg["enabled"]:
        raise OrderError("支付暂未开放，请稍后再试")
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        raise OrderError("数量无效")
    contact = (contact or "").strip()[:100]
    lookup_email = (lookup_email or "").strip().lower()
    if not EMAIL_RE.match(lookup_email) or len(lookup_email) > 120:
        raise OrderError("请填写有效的联系邮箱", "contact")
    now = core.now()
    window = cfg["order_timeout_minutes"] * 60
    guest_token = None if user_id else secrets.token_urlsafe(32)
    order_no = new_order_no(now)
    with tx(core.conn) as c:
        p = c.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if not p or not p["enabled"]:
            raise OrderError("这个套餐暂时不能购买，请刷新页面后重新选择", "product")
        if not 1 <= quantity <= p["max_per_order"]:
            raise OrderError("数量需在 1~%d" % p["max_per_order"])
        unit_price, variant = p["price_cents"], None
        enc, email, plan, reg = "", "", "", ""
        if p["type"] == "api_recharge":
            unit_price, variant, region = _recharge_price(c, p, region)
            data, email = parse_recharge_input(recharge_auth_text)
            enc = core.crypto.encrypt(json.dumps(data, ensure_ascii=False))
            plan, reg = p["plan_id"], region
        amount = unit_price * quantity
        cur = c.execute(
            "INSERT INTO orders(order_no, user_id, guest_token_hash, product_id, product_name, product_type,"
            " unit_price_cents, quantity, amount_cents, contact, recharge_input_enc, recharge_plan_id,"
            " recharge_region, recharge_email, created_at, expires_at, pay_deadline, lookup_email)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order_no, user_id, _hash_token(guest_token) if guest_token else None, p["id"], p["name"], p["type"],
             unit_price, quantity, amount, contact, enc, plan, reg, email, now, now + window + 90, now + window, lookup_email))
        oid = cur.lastrowid
        if variant is not None:
            _snapshot(c, oid, variant)
        c.execute("INSERT INTO fulfillments(order_id, type, updated_at) VALUES (?,?,?)", (oid, p["type"], now))
        if p["type"] == "card":
            r = c.execute(
                "UPDATE card_keys SET status='reserved', order_id=?, reserved_at=? WHERE id IN ("
                " SELECT id FROM card_keys WHERE product_id=? AND status='available' ORDER BY id LIMIT ?)",
                (oid, now, p["id"], quantity))
            if r.rowcount != quantity:
                raise OrderError("库存不足")
        core.event(c, oid, "order_created", "创建订单：%s × %d，应付 %s" % (p["name"], quantity, ali.format_cents(amount)))
    return order_no, guest_token


def preview_order(core, product_id, quantity, recharge_auth_text="", region="", lookup_email=""):
    """Validate exactly what create_order would validate, without creating anything (for the confirm step)."""
    p = core.conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not p or not p["enabled"]:
        raise OrderError("这个套餐暂时不能购买，请刷新页面后重新选择", "product")
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        raise OrderError("数量无效")
    if not 1 <= quantity <= p["max_per_order"]:
        raise OrderError("数量需在 1~%d" % p["max_per_order"])
    lookup_email = (lookup_email or "").strip().lower()
    if not EMAIL_RE.match(lookup_email) or len(lookup_email) > 120:
        raise OrderError("请填写有效的联系邮箱", "contact")
    out = {"product": p, "quantity": quantity, "amount_cents": p["price_cents"] * quantity, "lookup_email": lookup_email,
           "region": "", "account_email": ""}
    if p["type"] == "card":
        n = core.conn.execute("SELECT COUNT(*) n FROM card_keys WHERE product_id=? AND status='available'", (p["id"],)).fetchone()["n"]
        if n < quantity:
            raise OrderError("库存不足")
    else:
        unit_price, _v, region = _recharge_price(core.conn, p, region)
        _, out["account_email"] = parse_recharge_input(recharge_auth_text)
        out["region"] = region
        out["amount_cents"] = unit_price * quantity
    return out


def get_by_no(conn, order_no):
    return conn.execute("SELECT * FROM orders WHERE order_no=?", (order_no,)).fetchone()


def can_view(order, user_id=None, token=None):
    if order is None:
        return False
    if order["user_id"] is not None:
        return user_id is not None and user_id == order["user_id"]
    if not token or not order["guest_token_hash"]:
        return False
    return hmac.compare_digest(_hash_token(token), order["guest_token_hash"])


def ensure_qr(core, order_no):
    """Create (or re-fetch; Alipay precreate is idempotent per out_trade_no+params) the QR code."""
    o = get_by_no(core.conn, order_no)
    if o is None or o["payment_status"] != "unpaid":
        return o
    if o["qr_code"]:
        return o
    if core.now() >= o["pay_deadline"]:
        return o
    client = settings.alipay_client(core)
    core.conn.execute("UPDATE orders SET precreate_attempted=1 WHERE id=?", (o["id"],))
    qr = client.precreate(o["order_no"], o["amount_cents"], o["product_name"], o["pay_deadline"])
    core.conn.execute("UPDATE orders SET qr_code=? WHERE id=? AND payment_status='unpaid'", (qr, o["id"]))
    return get_by_no(core.conn, order_no)

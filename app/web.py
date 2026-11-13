"""Buyer-facing pages, login, order lookup, Alipay notify endpoint."""
import io
import re
import threading
import time

import qrcode
import qrcode.image.svg
from flask import (Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request,
                   session, url_for)

from . import alipay as ali
from . import fulfillment, orders, payments, pricing_service, site
from .security import check_password_hash, hash_password

bp = Blueprint("web", __name__)
notify_bp = Blueprint("notify", __name__)

_hits = {}
_hits_lock = threading.Lock()


def throttle(key, limit=10, window=60):
    now = time.time()
    with _hits_lock:
        q = [t for t in _hits.get(key, []) if now - t < window]
        ok = len(q) < limit
        if ok:
            q.append(now)
        _hits[key] = q
        if len(_hits) > 5000:
            _hits.clear()
    return ok


def core():
    return current_app.core


def me():
    return current_app.current_user()


def qr_svg(text):
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    s = buf.getvalue().decode()
    return s[s.index("<svg"):]


@bp.app_context_processor
def inject_site():
    return {"site": site.get_all(core().conn), "default_guide": site.DEFAULT_GUIDE}


# ---- auth -----------------------------------------------------------------
@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if not throttle("login:" + (request.remote_addr or ""), 10, 300):
            flash("尝试次数过多，请几分钟后再试")
            return render_template("login.html")
        email = request.form.get("email", "").strip().lower()
        row = core().conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if row and check_password_hash(row["password_hash"], request.form.get("password", "")):
            session.clear()
            session["uid"] = row["id"]
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("web.index"))
        flash("邮箱或密码错误")
    return render_template("login.html")


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("web.index"))


# ---- storefront -----------------------------------------------------------
def _stock(pid):
    return core().conn.execute("SELECT COUNT(*) n FROM card_keys WHERE product_id=? AND status='available'",
                               (pid,)).fetchone()["n"]


def _plan_order(p):
    ids = [x[0] for x in pricing_service.PLANS]
    return (ids.index(p["plan_id"]) if p["plan_id"] in ids else 99, p["id"])


def _regions_for(p):
    """[{code, price}] a buyer may choose for this product. Variant-priced plans only expose enabled, priced regions."""
    c = core().conn
    if p["type"] != "api_recharge":
        return []
    if pricing_service.has_variants(c, p["plan_id"]):
        return [{"code": v["region_code"], "price": v["sale_price_cny"]} for v in pricing_service.sellable_variants(c, p["plan_id"])]
    return [{"code": r, "price": p["price_cents"]} for r in p["regions"].split(",") if r]


@bp.route("/")
def index():
    rows = core().conn.execute("SELECT * FROM products WHERE enabled=1").fetchall()
    recharge = []
    for p in sorted([p for p in rows if p["type"] == "api_recharge"], key=_plan_order):
        regs = _regions_for(p)
        if regs:                                  # a plan with every region closed is not shown at all
            recharge.append({"p": p, "regions": regs, "min_price": min(r["price"] for r in regs)})
    cards = [(p, _stock(p["id"])) for p in rows if p["type"] == "card"]
    min_price = min([r["min_price"] for r in recharge], default=None)
    faq = site.parse_faq(site.get_all(core().conn)["faq"] or site.DEFAULT_FAQ)
    return render_template("index.html", recharge=recharge, cards=cards, min_price=min_price, faq=faq[:5])


@bp.route("/help")
def help_page():
    return render_template("help.html", faq=site.parse_faq(site.get_all(core().conn)["faq"] or site.DEFAULT_FAQ))


@bp.route("/terms")
def terms():
    return render_template("legal.html", title="服务条款", body=site.get_all(core().conn)["terms_text"])


@bp.route("/privacy")
def privacy():
    return render_template("legal.html", title="隐私说明", body=site.get_all(core().conn)["privacy_text"])


@bp.route("/product/<int:pid>")
def product(pid):
    p = core().conn.execute("SELECT * FROM products WHERE id=? AND enabled=1", (pid,)).fetchone()
    if not p:
        abort(404)
    p, stock, regions = _product_ctx(pid)
    return render_template("product.html", p=p, stock=stock, regions=regions, form={})


def _product_ctx(pid):
    p = core().conn.execute("SELECT * FROM products WHERE id=? AND enabled=1", (pid,)).fetchone()
    if not p:
        abort(404)
    regions = _regions_for(p)
    if p["type"] == "api_recharge" and not regions:
        abort(404)
    return p, (_stock(pid) if p["type"] == "card" else None), regions


@bp.route("/product/<int:pid>/confirm", methods=["POST"])
def confirm(pid):
    p, stock, regions = _product_ctx(pid)
    u = me()
    email = request.form.get("lookup_email", "") or (u["email"] if u else "")
    try:
        prev = orders.preview_order(core(), pid, request.form.get("quantity", "1"), request.form.get("auth_json", ""),
                                    request.form.get("region", ""), email)
    except orders.OrderError as e:
        flash(str(e))
        return render_template("product.html", p=p, stock=stock, regions=regions, form=request.form)
    return render_template("confirm.html", p=p, prev=prev, form=request.form)


@bp.route("/product/<int:pid>/buy", methods=["POST"])
def buy(pid):
    u = me()
    email = request.form.get("lookup_email", "") or (u["email"] if u else "")
    try:
        order_no, token = orders.create_order(
            core(), pid, request.form.get("quantity", "1"), user_id=u["id"] if u else None,
            contact=request.form.get("contact", ""), recharge_auth_text=request.form.get("auth_json", ""),
            region=request.form.get("region", ""), lookup_email=email)
    except orders.OrderError as e:
        flash(str(e))
        p, stock, regions = _product_ctx(pid)
        return render_template("product.html", p=p, stock=stock, regions=regions, form=request.form)
    try:
        orders.ensure_qr(core(), order_no)
    except Exception as e:       # order stays; buyer can press "重新获取二维码"
        if isinstance(e, ali.AlipayError):
            payments.close_local(core(), order_no, alipay_confirmed=False)
            flash("支付宝下单失败：%s" % (e.msg or e.sub_code or e.code))
            return redirect(url_for("web.product", pid=pid))
    return redirect(url_for("web.pay", order_no=order_no, t=token))


# ---- access: full (owner / token) or limited (proved order no + email) ----------
def _access(order_no):
    o = orders.get_by_no(core().conn, order_no)
    if o is None:
        abort(404)
    u = me()
    if orders.can_view(o, u["id"] if u else None, request.args.get("t")):
        return o, True
    if o["order_no"] in session.get("lookup", []):
        return o, False
    abort(404)


def _tail():
    t = request.args.get("t")
    return {"t": t} if t else {}


@bp.route("/pay/<order_no>")
def pay(order_no):
    o, full = _access(order_no)
    if o["payment_status"] != "unpaid":
        return redirect(url_for("web.order", order_no=order_no, **_tail()))
    svg = qr_svg(o["qr_code"]) if o["qr_code"] else ""
    return render_template("pay.html", o=o, qr=svg, token=request.args.get("t", ""))


@bp.route("/orders/<order_no>")
def order(order_no):
    o, full = _access(order_no)
    f = core().conn.execute("SELECT * FROM fulfillments WHERE order_id=?", (o["id"],)).fetchone()
    cards = []
    if full and o["product_type"] == "card" and f["status"] == "delivered":
        for r in core().conn.execute("SELECT code_enc FROM card_keys WHERE order_id=? AND status='sold' ORDER BY id", (o["id"],)):
            cards.append(core().crypto.decrypt(r["code_enc"]))
    masked = re.sub(r"^(.).*(@.*)$", r"\1•••••\2", o["lookup_email"])
    return render_template("order.html", o=o, f=f, cards=cards, full=full, token=request.args.get("t", ""), masked=masked)


@bp.route("/orders/<order_no>/status")
def order_status(order_no):
    o, full = _access(order_no)
    c = core()
    if o["payment_status"] == "unpaid" and o["precreate_attempted"] and c.now() - o["last_query_at"] >= 8:
        try:
            payments.check_payment(c, order_no)
        except Exception:
            pass
        o = orders.get_by_no(c.conn, order_no)
    f = c.conn.execute("SELECT * FROM fulfillments WHERE order_id=?", (o["id"],)).fetchone()
    if o["payment_status"] == "paid" and f["status"] == "pending":
        try:
            fulfillment.run(c, o["id"])
        except Exception:
            pass
        f = c.conn.execute("SELECT * FROM fulfillments WHERE order_id=?", (o["id"],)).fetchone()
    return jsonify(payment=o["payment_status"], fulfillment=f["status"], has_qr=bool(o["qr_code"]))


@bp.route("/orders/<order_no>/qr", methods=["POST"])
def order_qr(order_no):
    o, full = _access(order_no)
    try:
        orders.ensure_qr(core(), o["order_no"])
    except ali.AlipayError as e:
        flash("支付宝下单失败：%s" % (e.msg or e.sub_code))
    except Exception:
        flash("暂时无法获取二维码，请稍后重试")
    return redirect(url_for("web.pay", order_no=order_no, **_tail()))


@bp.route("/order/<order_no>")                      # legacy path
def order_legacy(order_no):
    return redirect(url_for("web.order", order_no=order_no, **_tail()))


@bp.route("/orders", methods=["GET", "POST"])
def lookup():
    u = me()
    if request.method == "POST":
        if not throttle("lookup:" + (request.remote_addr or ""), 10, 60):
            flash("查询太频繁，请稍后再试")
            return redirect(url_for("web.lookup"))
        no = request.form.get("order_no", "").strip().upper()
        email = request.form.get("email", "").strip().lower()
        o = orders.get_by_no(core().conn, no) if no and email else None
        if o is None or o["lookup_email"] != email:
            flash("没有找到匹配的订单，请检查订单号和下单邮箱")
            return redirect(url_for("web.lookup"))
        seen = session.get("lookup", [])
        session["lookup"] = ([no] + [x for x in seen if x != no])[:20]
        return redirect(url_for("web.order", order_no=no))
    rows = []
    if u:
        rows = core().conn.execute(
            "SELECT o.*, f.status AS fstatus FROM orders o JOIN fulfillments f ON f.order_id=o.id"
            " WHERE o.user_id=? ORDER BY o.id DESC LIMIT 200", (u["id"],)).fetchall()
    return render_template("lookup.html", rows=rows)


@bp.route("/my/orders")
def my_orders():
    return redirect(url_for("web.lookup"))


# ---- Alipay async notification (no CSRF, authenticated by signature) -------
@notify_bp.route("/notify/alipay", methods=["POST"])
def alipay():
    form = {k: v for k, v in request.form.items()}
    body, status = payments.handle_notify(core(), form)
    return body, status, {"Content-Type": "text/plain; charset=utf-8"}

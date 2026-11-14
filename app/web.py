"""Buyer-facing pages, login, order lookup, Alipay notify endpoint."""
import io
import json
import re
import threading
import time

import qrcode
import qrcode.image.svg
from flask import (Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request,
                   session, url_for)

from . import alipay as ali
from . import fulfillment, humanize, orders, payments, pricing_service, site
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
    plans = _checkout_plans()          # only the plans that currently have a sellable region
    min_price = min([p["min_price"] for p in plans], default=None)
    faq = site.parse_faq(site.get_all(core().conn)["faq"] or site.DEFAULT_FAQ)
    return render_template("index.html", plans=plans, min_price=min_price, faq=faq[:5])


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
    if o["payment_status"] == "paid" and f["status"] == "pending" and f["type"] == "card":
        try:
            fulfillment.run(c, o["id"], worker=False)
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


# ======================================================================================
# Checkout dialog JSON API (no account needed). Prices always come from the server.
# The full account e-mail is returned ONLY to the buyer who just submitted the credential,
# never placed in a URL, never logged.
# ======================================================================================
def _checkout_plans():
    """The cards on the home page: only products that currently have something sellable."""
    rows = core().conn.execute("SELECT * FROM products WHERE enabled=1 AND type='api_recharge'").fetchall()
    out = []
    for p in sorted(rows, key=_plan_order):
        regs = _regions_for(p)
        if not regs:
            continue
        out.append({"id": p["id"], "name": p["name"], "min_price": min(r["price"] for r in regs),
                    "min_price_text": ali.format_cents(min(r["price"] for r in regs)),
                    "varies": len({r["price"] for r in regs}) > 1,
                    "regions": [{"code": r["code"], "label": humanize.region_name(r["code"]), "price": r["price"],
                                 "price_text": ali.format_cents(r["price"])} for r in regs]})
    return out


def _json_error(message, field="", status=400, session_status="invalid"):
    return jsonify(ok=False, field=field, message=message, status=session_status), status


def _body():
    d = request.get_json(silent=True)
    return d if isinstance(d, dict) else {}


def _session_text(d):
    v = d.get("session", "")
    return json.dumps(v) if isinstance(v, (dict, list)) else str(v or "")


def _stage(o, f):
    if o["payment_status"] == "unpaid":
        return "pay"
    if o["payment_status"] == "closed":
        return "closed"
    if f["status"] == "delivered":
        return "done"
    if f["status"] in ("blocked", "failed", "manual"):
        return "attention"
    return "recharging"


def _status_payload(o, f):
    stage = _stage(o, f)
    msg = {"pay": "等待付款", "closed": "订单已超时关闭", "done": "充值完成",
           "attention": "充值暂时没有完成，付款记录已保存，请联系客服处理", "recharging": "已收到付款，正在充值"}[stage]
    return {"ok": True, "stage": stage, "message": msg, "order_no": o["order_no"], "amount_text": ali.format_cents(o["amount_cents"]),
            "has_qr": bool(o["qr_code"]), "submitted": bool(f["upstream_task_id"]), "deadline": o["pay_deadline"],
            "account_masked": humanize.mask_email(o["recharge_email"]), "plan": o["snap_plan_label"] or o["product_name"],
            "region": o["snap_region_label"] or humanize.region_name(o["recharge_region"])}


@bp.route("/api/checkout/plans")
def api_plans():
    return jsonify(ok=True, plans=_checkout_plans())


@bp.route("/api/checkout/validate", methods=["POST"])
def api_validate():
    if not throttle("co-validate:" + (request.remote_addr or ""), 40, 60):
        return _json_error("操作太频繁，请稍等一会儿再试", status=429)
    d = _body()
    try:
        pid = int(d.get("product_id") or 0)
    except (TypeError, ValueError):
        pid = 0
    try:
        prev = orders.preview_order(core(), pid, 1, _session_text(d), str(d.get("region", "")), str(d.get("contact_email", "")))
    except orders.OrderError as e:
        return _json_error(str(e), e.field, session_status=getattr(e, "status", "invalid"))
    p = prev["product"]
    if p["type"] != "api_recharge":
        return _json_error("这个套餐暂时不能购买，请刷新页面后重新选择", "product")
    return jsonify(ok=True, status="valid", plan=p["name"], region_code=prev["region"], region_label=humanize.region_name(prev["region"]),
                   amount_cents=prev["amount_cents"], amount_text=ali.format_cents(prev["amount_cents"]),
                   account_email=prev["account_email"], contact_email=prev["lookup_email"])


@bp.route("/api/checkout/order", methods=["POST"])
def api_create_order():
    """Creates an UNPAID order and its payment QR code. Nothing is sent upstream here - ever."""
    if not throttle("co-order:" + (request.remote_addr or ""), 10, 60):
        return _json_error("操作太频繁，请稍等一会儿再试", status=429)
    d = _body()
    if d.get("confirmed") is not True:
        return _json_error("请先勾选「我已确认充值账号、套餐地区和付款金额无误」", "confirm")
    try:
        pid = int(d.get("product_id") or 0)
    except (TypeError, ValueError):
        pid = 0
    try:
        order_no, token = orders.create_order(core(), pid, 1, user_id=None, recharge_auth_text=_session_text(d),
                                              region=str(d.get("region", "")), lookup_email=str(d.get("contact_email", "")))
    except orders.OrderError as e:
        return _json_error(str(e), e.field)
    try:
        orders.ensure_qr(core(), order_no)
    except ali.AlipayError:
        payments.close_local(core(), order_no, alipay_confirmed=False)
        return _json_error("支付宝暂时无法生成付款码，订单没有创建，请稍后再试")
    except Exception:       # keep the order; the dialog offers “重新获取二维码”
        pass
    o = orders.get_by_no(core().conn, order_no)
    f = core().conn.execute("SELECT * FROM fulfillments WHERE order_id=?", (o["id"],)).fetchone()
    data = _status_payload(o, f)
    data.update(token=token, qr_svg=qr_svg(o["qr_code"]) if o["qr_code"] else "")
    return jsonify(data)


@bp.route("/api/orders/<order_no>/status")
def api_order_status(order_no):
    o, _full = _access(order_no)
    c = core()
    if o["payment_status"] == "unpaid" and o["precreate_attempted"] and c.now() - o["last_query_at"] >= 8:
        try:
            payments.check_payment(c, order_no)
        except Exception:
            pass
        o = orders.get_by_no(c.conn, order_no)
    f = c.conn.execute("SELECT * FROM fulfillments WHERE order_id=?", (o["id"],)).fetchone()
    return jsonify(_status_payload(o, f))


@bp.route("/api/orders/<order_no>/check", methods=["POST"])
def api_order_check(order_no):
    """The “我已完成付款” button: ask Alipay right now (at most once every 3 seconds per order)."""
    o, _full = _access(order_no)
    c = core()
    if o["payment_status"] == "unpaid" and o["precreate_attempted"] and c.now() - o["last_query_at"] >= 3:
        try:
            payments.check_payment(c, order_no)
        except Exception:
            pass
        o = orders.get_by_no(c.conn, order_no)
    f = c.conn.execute("SELECT * FROM fulfillments WHERE order_id=?", (o["id"],)).fetchone()
    return jsonify(_status_payload(o, f))


@bp.route("/api/orders/<order_no>/qr", methods=["POST"])
def api_order_qr(order_no):
    o, _full = _access(order_no)
    err = ""
    try:
        orders.ensure_qr(core(), o["order_no"])
    except ali.AlipayError:
        err = "支付宝暂时无法生成付款码，请稍后重试"
    except Exception:
        err = "暂时无法获取付款码，请稍后重试"
    o = orders.get_by_no(core().conn, order_no)
    f = core().conn.execute("SELECT * FROM fulfillments WHERE order_id=?", (o["id"],)).fetchone()
    data = _status_payload(o, f)
    data.update(qr_svg=qr_svg(o["qr_code"]) if (o["qr_code"] and o["payment_status"] == "unpaid") else "", error=err)
    return jsonify(data)

"""Admin back office. Everything an operator sees is plain Chinese; technical values live in collapsed blocks."""
import secrets
from functools import wraps

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

from . import alipay as ali
from . import catalog, fulfillment, payments, pricing, pricing_service, settings, site, upstream_sync
from .humanize import explain  # noqa: F401

bp = Blueprint("admin", __name__)


def core():
    return current_app.core


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        u = current_app.current_user()
        if not u:
            return redirect(url_for("web.login", next=request.path))
        if not u["is_admin"]:
            abort(403)
        return fn(*a, **kw)
    return wrapper


def actor():
    return current_app.current_user()["email"]


def _one(sql, *a):
    return core().conn.execute(sql, a).fetchone()[0]


# ---- dashboard ------------------------------------------------------------
@bp.route("/")
@admin_required
def dashboard():
    c = core().conn
    ali_cfg = settings.alipay_status(core())
    up = settings.upstream_status(core())
    attention = c.execute(
        "SELECT o.*, f.status AS fstatus, f.reason AS freason FROM orders o JOIN fulfillments f ON f.order_id=o.id"
        " WHERE (o.payment_status='paid' AND f.status IN ('blocked','failed','manual'))"
        "    OR (o.attention!='' AND f.status!='delivered' AND o.payment_status!='closed') ORDER BY o.id DESC LIMIT 30").fetchall()
    stats = {
        "paid": _one("SELECT COUNT(*) FROM orders WHERE payment_status='paid'"),
        "unpaid": _one("SELECT COUNT(*) FROM orders WHERE payment_status='unpaid'"),
        "done": _one("SELECT COUNT(*) FROM fulfillments WHERE status='delivered'"),
        "attention": len(attention),
    }
    recharge_products = _one("SELECT COUNT(*) FROM products WHERE type='api_recharge'")
    card_products = _one("SELECT COUNT(*) FROM products WHERE type='card'")
    steps = [
        {"title": "配置支付宝收款", "done": bool(ali_cfg["enabled"] and ali_cfg["has_private_key"]),
         "hint": "填入支付宝应用信息，买家才能扫码付款。", "url": url_for("admin.alipay_config"), "btn": "去配置"},
        {"title": "连接上游充值", "done": bool(up["enabled"] and up["has_key"] and up["last_test_ok"]),
         "hint": "保存上游密钥并测试连接，账号充值商品才能自动充值。", "url": url_for("admin.upstream_config"), "btn": "去连接"},
        {"title": "同步套餐成本并设置价格", "done": _one("SELECT COUNT(*) FROM plan_variants") > 0,
         "hint": "把上游的四种套餐和各地区成本同步下来，设置加价后得到售价。", "url": url_for("admin.upstream_config"), "btn": "去同步"},
        {"title": "上架套餐地区或卡密商品", "done": (_one("SELECT COUNT(*) FROM plan_variants WHERE enabled=1 AND sale_price_cny IS NOT NULL") > 0
                                         or _one("SELECT COUNT(*) FROM products WHERE enabled=1 AND type='card'") > 0),
         "hint": "打开某个套餐地区的销售开关，买家就能看到并购买。", "url": url_for("admin.products"), "btn": "去上架"},
    ]
    return render_template("admin/dashboard.html", attention=attention, stats=stats, steps=steps, ali=ali_cfg, up=up, stale=pricing_service.staleness(core()),
                           anomalies=_one("SELECT COUNT(*) FROM plan_variants WHERE anomaly!=''") + (1 if pricing_service.get_settings(core().conn)["fx_pending_micro"] else 0),
                           all_done=all(s["done"] for s in steps), recharge_products=recharge_products, card_products=card_products)


# ---- Alipay ---------------------------------------------------------------
@bp.route("/alipay", methods=["GET", "POST"])
@admin_required
def alipay_config():
    if request.method == "POST":
        f = request.form
        try:
            settings.save_alipay(core(), f.get("enabled") == "1", f.get("mode", "sandbox"), f.get("app_id", ""),
                                 f.get("merchant_pid", ""), f.get("private_key", ""), f.get("alipay_public_key", ""),
                                 f.get("public_base_url", ""), f.get("order_timeout_minutes", "15"))
            flash("已保存")
        except settings.ConfigError as e:
            flash("保存失败：%s" % e)
        return redirect(url_for("admin.alipay_config"))
    return render_template("admin/alipay.html", cfg=settings.alipay_status(core()))


@bp.route("/alipay/test", methods=["POST"])
@admin_required
def alipay_test():
    try:
        client = settings.alipay_client(core(), require_enabled=False)
        client.query("TEST" + secrets.token_hex(8).upper())
        flash("配置检查：结果异常，查询一个不存在的订单却返回了成功，请核对 App ID 和环境（沙箱 / 正式）。")
    except ali.AlipayError as e:
        if e.sub_code == "ACQ.TRADE_NOT_EXIST" and e.signed:
            flash("配置检查通过：支付宝已接受你的应用，私钥和支付宝公钥匹配。")
        else:
            flash("支付宝返回了错误：%s（%s）" % (e.msg or "未知原因", "已验签" if e.signed else "未验签"))
    except ali.AlipaySignatureError:
        flash("验签失败：「支付宝公钥」不正确。注意要填「支付宝公钥」，不是你自己生成的「应用公钥」。")
    except ali.AlipayTransportError:
        flash("连不上支付宝网关，请检查服务器网络。")
    except settings.Unavailable as e:
        flash(str(e))
    except ValueError as e:
        flash("密钥无法使用：%s" % e)
    return redirect(url_for("admin.alipay_config"))


# ---- storefront content ---------------------------------------------------
@bp.route("/site", methods=["GET", "POST"])
@admin_required
def site_config():
    if request.method == "POST":
        try:
            site.save(core(), request.form)
            flash("已保存")
        except ValueError as e:
            flash("保存失败：%s" % e)
        return redirect(url_for("admin.site_config"))
    return render_template("admin/site.html", s=site.get_all(core().conn), default_faq=site.DEFAULT_FAQ,
                           default_guide=site.DEFAULT_GUIDE)


# ---- upstream -------------------------------------------------------------
@bp.route("/upstream", methods=["GET", "POST"])
@admin_required
def upstream_config():
    if request.method == "POST":
        f = request.form
        try:
            settings.save_upstream(core(), f.get("enabled") == "1", f.get("base_url") or settings.get_upstream(core())["base_url"],
                                   f.get("api_key", ""), f.get("card_platform_id", ""))
            flash("配置已保存。")
        except settings.ConfigError as e:
            flash("保存失败：%s" % e)
        return redirect(url_for("admin.upstream_config"))
    cfg = settings.upstream_status(core())
    return render_template("admin/upstream.html", cfg=cfg, cards=pricing_service.plan_cards(core()), stale=pricing_service.staleness(core()),
                           variant_count=_one("SELECT COUNT(*) FROM plan_variants"))


@bp.route("/upstream/test", methods=["POST"])
@admin_required
def upstream_test():
    ok, msg = upstream_sync.test_connection(core())
    flash(("连接成功：" if ok else "连接失败：") + msg)
    return redirect(url_for("admin.upstream_config"))


@bp.route("/upstream/sync", methods=["POST"])
@admin_required
def upstream_sync_plans():
    res = pricing_service.run_cycle(core(), force=True, actor=actor())
    if res is None:
        flash("正在同步中，请稍后再看结果。")
    else:
        fx_msg, cost_msg = res
        flash("成本：%s 汇率：%s" % (cost_msg, fx_msg))
    return redirect(url_for("admin.upstream_config"))


# ---- products -------------------------------------------------------------
@bp.route("/products")
@admin_required
def products():
    plan_ids = tuple(pricing_service.ALLOWED)
    rows = core().conn.execute("SELECT * FROM products WHERE plan_id NOT IN (%s) ORDER BY id DESC" % ",".join("?" * len(plan_ids)), plan_ids).fetchall()
    stocks = {r["id"]: catalog.stock_counts(core().conn, r["id"]) for r in rows}
    return render_template("admin/products.html", rows=rows, stocks=stocks, cards=pricing_service.plan_cards(core()),
                           fx=pricing_service.fx_view(core()), stale=pricing_service.staleness(core()),
                           audit=pricing_service.recent_audit(core().conn, 12), rounding_text=pricing.ROUNDING_TEXT,
                           upstream_ready=settings.get_upstream(core())["api_key_enc"] != "")


def _back(plan_id=""):
    return redirect(url_for("admin.products") + ("#plan-" + plan_id if plan_id else ""))


@bp.route("/pricing/sync", methods=["POST"])
@admin_required
def pricing_sync_now():
    res = pricing_service.run_cycle(core(), force=True, actor=actor())
    flash("正在同步中，请稍后刷新页面。" if res is None else "成本：%s 汇率：%s" % (res[1], res[0]))
    return _back()


@bp.route("/pricing/recalc", methods=["POST"])
@admin_required
def pricing_recalc():
    n = pricing_service.recalc_all(core(), actor())
    flash("已重新计算全部售价，%d 个价格有变化。" % n)
    return _back()


@bp.route("/pricing/settings", methods=["POST"])
@admin_required
def pricing_settings():
    f = request.form
    try:
        pricing_service.save_settings(core(), actor(), f.get("fx_mode", "auto"), f.get("fx_manual", ""), f.get("safety", "0"),
                                      f.get("anomaly", "10"), f.get("default_markup", "20"))
        flash("汇率与保护设置已保存，售价已按新设置重新计算。")
    except pricing_service.PricingError as e:
        flash("保存失败：%s" % e)
    return _back()


@bp.route("/pricing/confirm-fx", methods=["POST"])
@admin_required
def pricing_confirm_fx():
    flash("已确认新汇率，售价已更新。" if pricing_service.confirm_fx(core(), actor()) else "现在没有待确认的汇率。")
    return _back()


def _variant_plan(vid):
    row = core().conn.execute("SELECT plan_id FROM plan_variants WHERE id=?", (vid,)).fetchone()
    if not row:
        abort(404)
    return row["plan_id"]


@bp.route("/variants/<int:vid>/toggle", methods=["POST"])
@admin_required
def variant_toggle(vid):
    plan_id = _variant_plan(vid)
    try:
        on = request.form.get("enabled") == "1"
        pricing_service.toggle_variant(core(), actor(), vid, on)
        flash("已上架销售。" if on else "已关闭销售。关闭后，新用户将无法选择该地区，历史订单不受影响。")
    except pricing_service.PricingError as e:
        flash(str(e))
    return _back(plan_id)


@bp.route("/variants/<int:vid>/update", methods=["POST"])
@admin_required
def variant_update(vid):
    plan_id = _variant_plan(vid)
    try:
        pricing_service.update_variant(core(), actor(), vid, request.form)
        flash("已保存，售价已重新计算。")
    except pricing_service.PricingError as e:
        flash("保存失败：%s" % e)
    return _back(plan_id)


@bp.route("/variants/<int:vid>/confirm", methods=["POST"])
@admin_required
def variant_confirm(vid):
    plan_id = _variant_plan(vid)
    flash("已确认，新价格已生效。" if pricing_service.confirm_variant(core(), actor(), vid) else "这个地区没有待确认的价格。")
    return _back(plan_id)


@bp.route("/plans/<plan_id>/bulk", methods=["POST"])
@admin_required
def plan_bulk(plan_id):
    ids = [int(x) for x in request.form.getlist("variant_ids") if x.isdigit()]
    try:
        flash(pricing_service.bulk(core(), actor(), plan_id, request.form.get("action", ""), ids,
                                   {"value": request.form.get("value", ""), "rounding": request.form.get("rounding", "")}))
    except pricing_service.PricingError as e:
        flash("操作失败：%s" % e)
    return _back(plan_id)


def _form_regions():
    got = request.form.getlist("regions")
    if len(got) == 1 and "," in got[0]:
        got = got[0].split(",")
    return ",".join(x.strip().upper() for x in got if x.strip())


def _check_plan_regions(plan_id, regions):
    """Operator may only sell regions the upstream plan supports."""
    plan = core().conn.execute("SELECT * FROM upstream_plans WHERE plan_id=?", (plan_id,)).fetchone()
    if plan is None:
        return
    allowed = set(r for r in plan["regions"].split(",") if r)
    bad = [r for r in regions.split(",") if r and r not in allowed]
    if bad:
        raise catalog.CatalogError("所选地区不在这个套餐支持的范围内，请重新选择。")


@bp.route("/products/new", methods=["GET", "POST"])
@bp.route("/products/<int:pid>", methods=["GET", "POST"])
@admin_required
def product_edit(pid=None):
    p = None
    if pid:
        p = core().conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not p:
            abort(404)
    if p and p["type"] == "api_recharge" and p["plan_id"] in pricing_service.ALLOWED:
        flash("ChatGPT 套餐的价格和地区在「商品」页的套餐卡片里管理。")
        return _back(p["plan_id"])
    if request.method == "POST":
        f = request.form
        type_ = p["type"] if p else "card"
        plan_id = p["plan_id"] if p else ""
        regions = _form_regions()
        try:
            if type_ == "api_recharge":
                _check_plan_regions(plan_id, regions)
            new_id = catalog.save_product(core(), pid, f.get("name", ""), f.get("description", ""), type_, f.get("price", ""),
                                          f.get("enabled") == "1", f.get("max_per_order", "1"), plan_id, regions)
            flash("已保存。")
            return redirect(url_for("admin.product_edit", pid=new_id))
        except catalog.CatalogError as e:
            flash("保存失败：%s" % e)
    if p is None:
        return render_template("admin/product_wizard.html", form=request.form if request.method == "POST" else {})
    return render_template("admin/product_form.html", p=p, plan=None, stock=catalog.stock_counts(core().conn, pid) if p["type"] == "card" else None)


@bp.route("/products/<int:pid>/cards", methods=["GET", "POST"])
@admin_required
def cards(pid):
    c = core().conn
    p = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not p or p["type"] != "card":
        abort(404)
    if request.method == "POST":
        text = request.form.get("codes", "")
        up = request.files.get("file")
        if up and up.filename:
            text += "\n" + up.read(2 * 1024 * 1024).decode("utf-8", "replace")
        try:
            added, dups, invalid = catalog.import_cards(core(), pid, text)
            flash("导入完成：新增 %d 条，重复跳过 %d 条，无效 %d 条" % (added, dups, invalid))
        except catalog.CatalogError as e:
            flash("导入失败：%s" % e)
        return redirect(url_for("admin.cards", pid=pid))
    status = request.args.get("status", "")
    page = max(1, request.args.get("page", 1, type=int))
    where, args = "product_id=?", [pid]
    if status in ("available", "reserved", "sold", "void"):
        where += " AND status=?"
        args.append(status)
    rows = c.execute("SELECT id, mask, status, order_id, created_at, sold_at FROM card_keys WHERE %s ORDER BY id DESC"
                     " LIMIT 50 OFFSET ?" % where, args + [(page - 1) * 50]).fetchall()
    return render_template("admin/cards.html", p=p, rows=rows, stock=catalog.stock_counts(c, pid), status=status, page=page)


@bp.route("/cards/<int:cid>/void", methods=["POST"])
@admin_required
def card_void(cid):
    row = core().conn.execute("SELECT product_id FROM card_keys WHERE id=?", (cid,)).fetchone()
    if not row:
        abort(404)
    flash("已作废这张卡密" if catalog.void_card(core(), cid) else "只有「可售」状态的卡密才能作废")
    return redirect(url_for("admin.cards", pid=row["product_id"]))


# ---- orders ---------------------------------------------------------------
VIEWS = [("all", "全部"), ("unpaid", "待付款"), ("processing", "处理中"), ("attention", "需要处理"), ("done", "已完成"), ("closed", "已关闭")]
VIEW_SQL = {
    "unpaid": "o.payment_status='unpaid'",
    "processing": "o.payment_status='paid' AND f.status IN ('pending','processing')",
    "attention": "o.payment_status='paid' AND f.status IN ('blocked','failed','manual')",
    "done": "f.status='delivered'",
    "closed": "o.payment_status='closed'",
}


@bp.route("/orders")
@admin_required
def orders_list():
    view = request.args.get("view", "all")
    q = request.args.get("q", "").strip()
    where, args = "1=1", []
    if view in VIEW_SQL:
        where += " AND " + VIEW_SQL[view]
    if q:
        where += " AND (o.order_no LIKE ? OR o.lookup_email LIKE ? OR o.recharge_email LIKE ? OR o.contact LIKE ?)"
        args += ["%" + q + "%"] * 4
    page = max(1, request.args.get("page", 1, type=int))
    rows = core().conn.execute(
        "SELECT o.*, f.status AS fstatus, f.reason AS freason FROM orders o JOIN fulfillments f ON f.order_id=o.id"
        " WHERE %s ORDER BY o.id DESC LIMIT 50 OFFSET ?" % where, args + [(page - 1) * 50]).fetchall()
    return render_template("admin/orders.html", rows=rows, views=VIEWS, view=view, q=q, page=page)


def _headline(o, f):
    if o["payment_status"] == "unpaid":
        return "wait", "买家还没有付款。超过支付时限后，订单会自动关闭并释放库存。"
    if o["payment_status"] == "closed":
        return "off", "订单已关闭，没有收到付款。"
    s = f["status"]
    if s == "delivered":
        return "ok", "已完成：买家已收到商品或充值已到账，无需处理。"
    if s in ("pending", "processing"):
        return "wait", "系统正在自动处理，通常几分钟内完成，无需你操作。"
    return ("bad" if s in ("blocked", "failed") else "warn"), (f["reason"] or "系统没能自动完成这笔订单，需要你处理。")


@bp.route("/orders/<int:oid>")
@admin_required
def order_detail(oid):
    c = core().conn
    o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        abort(404)
    f = c.execute("SELECT * FROM fulfillments WHERE order_id=?", (oid,)).fetchone()
    pay = c.execute("SELECT * FROM payments WHERE order_id=?", (oid,)).fetchone()
    events = c.execute("SELECT * FROM events WHERE order_id=? ORDER BY id", (oid,)).fetchall()
    cards = c.execute("SELECT mask, status FROM card_keys WHERE order_id=? ORDER BY id", (oid,)).fetchall()
    tone, headline = _headline(o, f)
    needs_action = o["payment_status"] == "paid" and f["status"] in ("blocked", "failed", "manual")
    return render_template("admin/order_detail.html", o=o, f=f, pay=pay, events=events, cards=cards, tone=tone,
                           headline=headline, needs_action=needs_action,
                           retry_label="重新发货" if o["product_type"] == "card" else "重新尝试充值",
                           can_manual=o["product_type"] == "api_recharge" and o["payment_status"] == "paid" and f["status"] in ("blocked", "failed", "manual"))


REQUERY_TEXT = {"paid": "支付宝确认：已收到买家付款。", "waiting": "支付宝确认：买家还没有付款。",
                "closed": "支付宝确认：这笔交易已关闭。", "not_exist": "支付宝确认：没有这笔交易（买家未扫码）。",
                "unknown": "暂时无法向支付宝确认，请稍后再试。"}


@bp.route("/orders/<int:oid>/action", methods=["POST"])
@admin_required
def order_action(oid):
    c = core()
    o = c.conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        abort(404)
    act = request.form.get("action")
    if act == "retry":
        flash("已重新提交处理，请稍后刷新页面查看结果。" if fulfillment.admin_retry(c, oid, actor()) else "这笔订单现在不需要重试。")
    elif act == "requery":
        flash(REQUERY_TEXT.get(payments.check_payment(c, o["order_no"]), REQUERY_TEXT["unknown"]))
    elif act in ("mark_success", "mark_failed"):
        if request.form.get("confirm") != "yes":
            flash("请先勾选「我已在上游核对过」。")
        else:
            ok = fulfillment.admin_mark(c, oid, act == "mark_success", actor())
            flash("已记录你的处理结果。" if ok else "这笔订单现在不能这样处理。")
    return redirect(url_for("admin.order_detail", oid=oid))

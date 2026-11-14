"""Admin back office. Everything an operator sees is plain Chinese; technical values live in collapsed blocks."""
import secrets
from functools import wraps

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for

from . import alipay as ali
from . import catalog, fulfillment, ops, payments, pricing, pricing_service, settings, site, upstream_sync
from . import humanize
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
    period = request.args.get("period", "today")
    if period not in ("today", "7d", "30d"):
        period = "today"
    ops_overview = ops.overview(core(), period)
    return render_template("admin/dashboard.html", attention=attention, stats=stats, steps=steps, ali=ali_cfg, up=up, stale=pricing_service.staleness(core()),
                           anomalies=_one("SELECT COUNT(*) FROM plan_variants WHERE anomaly!=''") + (1 if pricing_service.get_settings(core().conn)["fx_pending_micro"] else 0),
                           all_done=all(s["done"] for s in steps), recharge_products=recharge_products, card_products=card_products,
                           ops_overview=ops_overview, ops_period=period, tz_label=ops.TZ_LABEL)


# ---- system settings: one entry in the sidebar, three tabs that keep their own URLs / forms ----------
SETTINGS_ENDPOINTS = ("admin.settings_home", "admin.alipay_config", "admin.alipay_test", "admin.upstream_config", "admin.upstream_test",
                      "admin.upstream_sync_plans", "admin.site_config")


@bp.route("/settings")
@admin_required
def settings_home():
    return redirect(url_for("admin.alipay_config"))


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
def _decorate_cards(cards, fx_micro):
    """Display-only extras for the pricing table (no pricing rules here): formatted strings and a few flags."""
    for pc in cards:
        for v in pc["variants"]:
            v["has_cost"] = v["effective_package_cost_cny"] is not None
            v["has_up"] = v["upstream_cost_usd_cents"] is not None
            v["up_ok"] = bool(v["has_up"] and fx_micro)
            v["up_cny"] = (pricing.fmt_cny(pricing.usd_cents_to_cny_fen(v["upstream_cost_usd_cents"], fx_micro))
                           if v["up_ok"] else "")
            v["mode_text"] = pricing.MODE_TEXT.get(v["pricing_mode"], v["pricing_mode"])
    return cards


def _variant_json(v):
    f = pricing.fmt_cny
    return {
        "id": v["id"], "enabled": bool(v["enabled"]), "can_sell": bool(v["can_sell"]), "has_cost": bool(v["has_cost"]),
        "status": "销售中" if v["enabled"] else "未销售", "mode": v["pricing_mode"], "mode_text": v["mode_text"],
        "price": ("¥" + f(v["sale_price_cny"])) if v["sale_price_cny"] is not None else "未定价",
        "package_cost": ("¥" + f(v["effective_package_cost_cny"])) if v["has_cost"] else "未填写",
        "other_cost": "¥" + f(v["other_cost_cny"]),
        "total_cost": ("¥" + f(v["total_cost_cny"])) if v["total_cost_cny"] is not None else "-",
        "profit": ("¥" + f(v["profit"])) if v["profit"] is not None else "暂无成本，无法计算",
        "loss": bool(v["loss"]), "cost_note": v["cost_note"], "anomaly": v["anomaly"], "anomaly_detail": v["anomaly_detail"],
        "up_ok": bool(v["up_ok"]),
        "fields": {"cost_source": v["cost_source"], "manual_cost": f(v["manual_package_cost_cny"]) if v["manual_package_cost_cny"] is not None else "",
                   "other_cost": f(v["other_cost_cny"]), "mode": v["pricing_mode"], "markup": "%g" % (v["markup_basis_points"] / 100),
                   "fixed_profit": f(v["fixed_profit_cny"]), "retail": f(v["manual_sale_price_cny"]) if v["manual_sale_price_cny"] else "",
                   "rounding": v["rounding"]},
    }


def _plan_payload(plan_id):
    fx_micro = pricing_service.effective_fx(pricing_service.get_settings(core().conn))
    pc = next(x for x in _decorate_cards(pricing_service.plan_cards(core()), fx_micro) if x["plan_id"] == plan_id)
    return {"variants": [_variant_json(v) for v in pc["variants"]],
            "summary": {"live": pc["live"], "total": pc["total"], "min_price": ("¥" + pricing.fmt_cny(pc["min_price"])) if pc["min_price"] else "暂无",
                        "strategy": pc["strategy"]}}


@bp.route("/products")
@admin_required
def products():
    plan_ids = tuple(pricing_service.ALLOWED)
    rows = core().conn.execute("SELECT * FROM products WHERE plan_id NOT IN (%s) ORDER BY id DESC" % ",".join("?" * len(plan_ids)), plan_ids).fetchall()
    stocks = {r["id"]: catalog.stock_counts(core().conn, r["id"]) for r in rows}
    fxv = pricing_service.fx_view(core())
    cards = _decorate_cards(pricing_service.plan_cards(core()), fxv["effective"])
    active = request.args.get("plan", "")
    if active not in pricing_service.ALLOWED:
        active = next((c["plan_id"] for c in cards if c["total"]), cards[0]["plan_id"])
    return render_template("admin/products.html", rows=rows, stocks=stocks, cards=cards, fx=fxv, stale=pricing_service.staleness(core()),
                           rounding_text=pricing.ROUNDING_TEXT, mode_text=pricing.MODE_TEXT,
                           anomaly_total=sum(c["anomalies"] for c in cards) + (1 if fxv["pending"] else 0), active=active, open_vid=request.args.get("edit", type=int),
                           upstream_ready=settings.get_upstream(core())["api_key_enc"] != "")


def _ajax():
    return request.headers.get("X-Requested-With") == "fetch"


def _back(plan_id="", vid=None):
    url = url_for("admin.products", **({"plan": plan_id} if plan_id else {}), **({"edit": vid} if vid else {}))
    return redirect(url + (("#row-%d" % vid) if vid else ("#plan-" + plan_id if plan_id else "")))


def _reply(plan_id, ok, message, vid=None, status=200):
    """The pricing table saves in place (fetch -> JSON). Without JavaScript it falls back to flash + redirect to the same plan/row."""
    if _ajax():
        data = {"ok": ok, "message": message, "plan_id": plan_id}
        data.update(_plan_payload(plan_id))
        return jsonify(data), status
    flash(message)
    return _back(plan_id, vid)


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
        return _reply(plan_id, True, "已上架销售。" if on else "已关闭销售。关闭后，新用户将无法选择该地区，历史订单不受影响。", vid)
    except pricing_service.PricingError as e:
        return _reply(plan_id, False, str(e), vid, 400)


def _has_cost_after(v, f, fx_micro):
    """Would this region have a package cost after applying the submitted cost fields? (uses the existing cost helper)"""
    src = f.get("cost_source") or v["cost_source"]
    manual = v["manual_package_cost_cny"]
    if f.get("manual_package_cost") is not None:
        txt = str(f["manual_package_cost"]).strip()
        try:
            manual = pricing.parse_money_fen(txt) if txt else None
        except ValueError:
            return True                                    # a malformed number gets its own message from the service
    eff, _ = pricing.effective_package_cost(src, v["upstream_cost_usd_cents"], manual, fx_micro)
    return eff is not None


NO_COST_MSG = "请先填写套餐成本。没有成本时，只能使用「直接设置零售价」；「按成本百分比加价」和「固定利润」需要先有套餐成本。"


@bp.route("/variants/<int:vid>/update", methods=["POST"])
@admin_required
def variant_update(vid):
    plan_id = _variant_plan(vid)
    f = request.form
    v = dict(core().conn.execute("SELECT * FROM plan_variants WHERE id=?", (vid,)).fetchone())
    mode = f.get("pricing_mode") or v["pricing_mode"]
    if (f.get("manual_sale_price") or "").strip() and mode != "manual_retail":
        # never silently ignore a typed retail price
        return _reply(plan_id, False, "你填写了零售价，但当前选的定价方式是「%s」，这个价格不会生效。请选择「直接设置零售价」后再保存。"
                      % pricing.MODE_TEXT.get(mode, mode), vid, 400)
    if mode in ("percentage", "fixed") and not _has_cost_after(v, f, pricing_service.effective_fx(pricing_service.get_settings(core().conn))):
        return _reply(plan_id, False, NO_COST_MSG, vid, 400)
    try:
        pricing_service.update_variant(core(), actor(), vid, f)
    except pricing_service.PricingError as e:
        return _reply(plan_id, False, "保存失败：%s" % e, vid, 400)
    return _reply(plan_id, True, "已保存。", vid)


@bp.route("/variants/<int:vid>/confirm", methods=["POST"])
@admin_required
def variant_confirm(vid):
    plan_id = _variant_plan(vid)
    done = pricing_service.confirm_variant(core(), actor(), vid)
    return _reply(plan_id, done, "已确认，新价格已生效。" if done else "这个地区没有待确认的价格。", vid, 200 if done else 400)


@bp.route("/plans/<plan_id>/bulk", methods=["POST"])
@admin_required
def plan_bulk(plan_id):
    if plan_id not in pricing_service.ALLOWED:
        abort(404)
    f = request.form
    ids = [int(x) for x in f.getlist("variant_ids") if x.isdigit()]
    action = f.get("action", "")
    value = f.get("value_" + action) if (f.get("value_" + action) or "").strip() else f.get("value", "")
    skipped = []
    if action in ("markup", "profit"):                       # these two need a package cost: apply only where there is one
        rows = [dict(r) for r in core().conn.execute("SELECT * FROM plan_variants WHERE plan_id=? ORDER BY id", (plan_id,)).fetchall()]
        chosen = [r for r in rows if not ids or r["id"] in set(ids)]
        fx_micro = pricing_service.effective_fx(pricing_service.get_settings(core().conn))
        eligible = [r for r in chosen if _has_cost_after(r, {}, fx_micro)]
        skipped = [r["region_label"] for r in chosen if not _has_cost_after(r, {}, fx_micro)]
        if not eligible:
            return _reply(plan_id, False, "这些地区还没有套餐成本：%s。%s" % ("、".join(skipped), NO_COST_MSG), None, 400)
        ids = [r["id"] for r in eligible]
    try:
        msg = pricing_service.bulk(core(), actor(), plan_id, action, ids, {"value": value, "rounding": f.get("rounding", "")})
        if skipped:
            msg += " 另有 %s 还没有套餐成本，已跳过（它们只能使用「直接设置零售价」）。" % "、".join(skipped)
        return _reply(plan_id, True, msg)
    except pricing_service.PricingError as e:
        return _reply(plan_id, False, "操作失败：%s" % e, None, 400)


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
PLAN_OPTIONS = [("", "全部套餐")] + [(pid, label.replace("ChatGPT ", "")) for pid, label, _ in pricing_service.PLANS]
PERIOD_OPTIONS = [("", "全部时间"), ("today", "今天"), ("yesterday", "昨天"), ("7d", "近 7 天"), ("30d", "近 30 天"), ("custom", "自定义日期")]
CUSTOMER_OPTIONS = [("all", "全部客户"), ("first", "首次下单"), ("repeat", "复购客户")]
PAGE_SIZE = 30


def _region_options():
    """Regions that exist in the current pricing table (plus any region already used by an order), Chinese names only."""
    c = core().conn
    codes = {r[0] for r in c.execute("SELECT DISTINCT region_code FROM plan_variants")}
    codes |= {r[0] for r in c.execute("SELECT DISTINCT recharge_region FROM orders WHERE recharge_region!=''")}
    from .humanize import region_name
    return sorted(((code, region_name(code)) for code in codes if code), key=lambda x: x[1])


def _order_filters():
    """Read + sanitise the filter bar from the query string. Returns (filters, search kwargs, date_error)."""
    a = request.args
    view = a.get("view", "all")
    f = {"view": view if view in dict(VIEWS) else "all",
         "plan": a.get("plan", "") if a.get("plan", "") in dict(PLAN_OPTIONS) else "",
         "region": a.get("region", "").strip().upper()[:8],
         "period": a.get("period", "") if a.get("period", "") in dict(PERIOD_OPTIONS) else "",
         "from": a.get("from", "").strip()[:10], "to": a.get("to", "").strip()[:10],
         "customer": a.get("customer", "all") if a.get("customer", "all") in dict(CUSTOMER_OPTIONS) else "all",
         "q": a.get("q", "").strip()[:120]}
    kw = {"status": f["view"], "plan_id": f["plan"], "region": f["region"], "customer": f["customer"], "q": f["q"],
          "period": "", "date_from": "", "date_to": ""}
    date_error = ""
    if f["period"] == "custom":
        if f["from"] and f["to"]:
            kw["date_from"], kw["date_to"] = f["from"], f["to"]
        else:
            date_error = "自定义日期需要同时选择开始日期和结束日期，已先显示全部时间。"
    elif f["period"]:
        kw["period"] = f["period"]
    return f, kw, date_error


@bp.route("/orders")
@admin_required
def orders_list():
    f, kw, date_error = _order_filters()
    page = max(1, request.args.get("page", 1, type=int))
    try:
        result = ops.search_orders(core(), page=page, page_size=PAGE_SIZE, **kw)
    except ops.OpsError as e:                                   # e.g. an end date before the start date
        date_error = str(e)
        kw.update(period="", date_from="", date_to="")
        result = ops.search_orders(core(), page=page, page_size=PAGE_SIZE, **kw)
    live = _live_rows([r["id"] for r in result["rows"]])
    for r in result["rows"]:
        r["live"] = live[r["id"]]
    kept = {k: v for k, v in f.items() if v and v not in ("all",) and not (k in ("from", "to") and f["period"] != "custom")}
    if date_error:
        kept.pop("from", None); kept.pop("to", None)
        if f["period"] == "custom":
            kept.pop("period", None)
    total_all = _one("SELECT COUNT(*) FROM orders")
    return render_template("admin/orders.html", ops_result=result, rows=result["rows"], f=f, kept=kept, filters_active=bool(kept), date_error=date_error,
                           views=VIEWS, plan_options=PLAN_OPTIONS, period_options=PERIOD_OPTIONS, customer_options=CUSTOMER_OPTIONS,
                           region_options=_region_options(), total_all=total_all, page=result["page"], tz_label=ops.TZ_LABEL)


# ---- read-only live status (display fields only; polling never submits anything upstream) -----------
STEPS = [("unpaid", "等待付款"), ("paid", "已付款"), ("worker", "等待 Worker"), ("submitted", "已提交上游"), ("upstream", "上游处理中"), ("done", "充值完成")]


def _live_stage(o, f):
    """The real stage of an order, in the operator's words. Pure function of stored state."""
    recharge = o["product_type"] == "api_recharge"
    if o["payment_status"] == "closed":
        return "closed", "已关闭", "off"
    if o["payment_status"] == "unpaid":
        return "unpaid", "等待付款", "wait"
    st = f["status"]
    if st == "delivered":
        return "done", ("充值完成" if recharge else "发货完成"), "ok"
    if st in ("blocked", "failed", "manual"):
        return "attention", "需要人工处理", "bad"
    if not recharge or st == "none":
        return "paid", "已付款", "wait"
    if f["upstream_task_id"]:
        up = (f["upstream_status"] or "").lower()
        return (("submitted", "已提交上游") if up in ("", "queued") else ("upstream", "上游处理中")) + ("wait",)
    return "worker", "等待 Worker", "wait"


def _worker_text(o, f):
    if o["payment_status"] != "paid":
        return "还没到充值环节"
    if o["product_type"] != "api_recharge":
        return "卡密发货由系统直接完成"
    if f["status"] == "delivered":
        return "已完成"
    if f["status"] in ("blocked", "failed", "manual"):
        return "已停止自动处理，需要人工处理"
    if f["upstream_task_id"]:
        return "Worker 已提交上游"
    if f["status"] == "processing":
        return "Worker 正在提交"
    return "等待 Worker 领取" + ("（已尝试 %d 次）" % f["attempts"] if f["attempts"] else "")


def _live_rows(ids):
    """{order_id: display dict} for the given ids, one query."""
    ids = [int(i) for i in ids][:100]
    if not ids:
        return {}
    rows = core().conn.execute(
        "SELECT o.id, o.payment_status, o.product_type, f.status AS fstatus, f.upstream_task_id, f.upstream_status, f.attempts, f.reason"
        " FROM orders o JOIN fulfillments f ON f.order_id=o.id WHERE o.id IN (%s)" % ",".join("?" * len(ids)), ids).fetchall()
    out = {}
    for r in rows:
        o = {"payment_status": r["payment_status"], "product_type": r["product_type"]}
        f = {"status": r["fstatus"], "upstream_task_id": r["upstream_task_id"], "upstream_status": r["upstream_status"], "attempts": r["attempts"]}
        key, text, tone = _live_stage(o, f)
        needs = r["payment_status"] == "paid" and r["fstatus"] in ("blocked", "failed", "manual")
        out[r["id"]] = {"id": r["id"], "stage": key, "stage_text": text, "tone": tone, "needs_attention": needs,
                        "payment_text": humanize.PAY_TEXT.get(r["payment_status"], ""), "payment_tone": humanize.PAY_TONE.get(r["payment_status"], "off"),
                        "fulfillment_text": humanize.FUL_TEXT.get(r["fstatus"], "") if r["payment_status"] == "paid" else "",
                        "fulfillment_tone": humanize.FUL_TONE.get(r["fstatus"], "off"), "reason": (r["reason"] or "") if needs else ""}
    return out


@bp.route("/api/orders/live")
@admin_required
def api_orders_live():
    ids = [int(x) for x in request.args.get("ids", "").split(",") if x.strip().isdigit()]
    resp = jsonify(ok=True, rows=_live_rows(ids), attention_total=_one(
        "SELECT COUNT(*) FROM orders o JOIN fulfillments f ON f.order_id=o.id WHERE o.payment_status='paid' AND f.status IN ('blocked','failed','manual')"))
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _steps_payload(o, f):
    key, text, tone = _live_stage(o, f)
    order = [k for k, _ in STEPS]
    terminal = None
    if key == "closed":
        current, terminal = 0, {"text": "已关闭", "tone": "off"}
        steps = [{"text": STEPS[0][1], "state": "done"}]
        return steps, terminal
    if key == "attention":
        reached = "worker" if not f["upstream_task_id"] else ("submitted" if (f["upstream_status"] or "").lower() in ("", "queued") else "upstream")
        current = order.index(reached)
        terminal = {"text": "需要人工处理", "tone": "bad"}
        return [{"text": t, "state": "done"} for k, t in STEPS[:current + 1]], terminal
    if key == "paid":
        key = "paid"
    current = order.index(key) if key in order else 1
    steps = []
    for i, (k, t) in enumerate(STEPS):
        steps.append({"text": t, "state": "done" if (i < current or key == "done") else ("current" if i == current else "pending")})
    return steps, terminal


@bp.route("/api/orders/<int:oid>/live")
@admin_required
def api_order_live(oid):
    c = core().conn
    o = c.execute("SELECT id, payment_status, product_type, paid_at, closed_at FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        abort(404)
    f = c.execute("SELECT status, reason, attempts, upstream_task_id, upstream_status, delivered_at FROM fulfillments WHERE order_id=?", (oid,)).fetchone()
    key, text, tone = _live_stage(o, f)
    steps, terminal = _steps_payload(o, f)
    htone, headline = _headline(o, f)
    needs = o["payment_status"] == "paid" and f["status"] in ("blocked", "failed", "manual")
    resp = jsonify(ok=True, id=oid, stage={"key": key, "text": text, "tone": tone}, steps=steps, terminal=terminal,
                   payment={"text": humanize.PAY_TEXT.get(o["payment_status"], ""), "tone": humanize.PAY_TONE.get(o["payment_status"], "off")},
                   fulfillment={"text": humanize.FUL_TEXT.get(f["status"], "") if o["payment_status"] == "paid" else "", "tone": humanize.FUL_TONE.get(f["status"], "off")},
                   worker_text=_worker_text(o, f), upstream_text=humanize.UP_TEXT.get((f["upstream_status"] or "").lower(), "") if o["product_type"] == "api_recharge" else "",
                   headline=headline, headline_tone=htone, needs_action=needs, attention_reason=(f["reason"] or "") if needs else "",
                   paid_at_text=ops.format_ts(o["paid_at"]) or "", completed_at_text=ops.format_ts(f["delivered_at"]) or "", closed_at_text=ops.format_ts(o["closed_at"]) or "")
    resp.headers["Cache-Control"] = "no-store"
    return resp


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
    buyer = ops.buyer_summary(core(), o["lookup_email"])
    recharge_acct = ops.recharge_account_summary(core(), o["recharge_email"])
    timeline = ops.order_timeline(core(), oid)
    lookup_rank = ops._order_rank(c, o["lookup_email"], oid)
    steps, terminal = _steps_payload(o, f)
    return render_template("admin/order_detail.html", o=o, f=f, pay=pay, stage=_live_stage(o, f), steps=steps, terminal=terminal,
                           worker_text=_worker_text(o, f), events=events, cards=cards, tone=tone,
                           headline=headline, needs_action=needs_action,
                           retry_label="重新发货" if o["product_type"] == "card" else "重新尝试充值",
                           can_manual=o["product_type"] == "api_recharge" and o["payment_status"] == "paid" and f["status"] in ("blocked", "failed", "manual"),
                           buyer=buyer, recharge_acct=recharge_acct, timeline=timeline, tz_label=ops.TZ_LABEL,
                           lookup_order_rank=lookup_rank)


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

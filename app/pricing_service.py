"""GPT direct-recharge pricing: plan x region variants, upstream cost sync, FX sync, guards, audit.

Only the four plans below are ever created. Upstream `wallet.prices` is a USD *reference* cost (null is "unknown", never 0);
`regionalDisplayPrices` is deliberately ignored (it is a local-currency display price, not our purchase cost)."""
import json
import re

from . import fx, humanize, pricing, settings
from .db import tx
from .upstream import UpstreamTransportError

PLANS = [
    ("plus-monthly", "ChatGPT Plus", "ChatGPT Plus 月付，付款后自动充值到你的账号。"),
    ("pro5x-monthly", "ChatGPT Pro 5x", "ChatGPT Pro 5x 月付，付款后自动充值到你的账号。"),
    ("pro20x-direct-monthly", "ChatGPT Pro 20x", "ChatGPT Pro 20x 月付，付款后自动充值到你的账号。"),
    ("promax-monthly", "ChatGPT Pro Max", "ChatGPT Pro Max 月付，付款后自动充值到你的账号。"),
]
ALLOWED = {p[0] for p in PLANS}
PLAN_LABEL = {p[0]: p[1] for p in PLANS}
REGION_CURRENCY = {"PH": "PHP", "EG": "EGP", "JP": "JPY", "US": "USD", "CL": "CLP", "SG": "SGD", "TZ": "TZS", "SE": "SEK"}
SYNC_INTERVAL = 30 * 60
STALE_WARN, STALE_ERR = 2 * 3600, 6 * 3600


class PricingError(Exception):
    pass


def region_code(r):
    """The upstream returns regions either as plain codes ("PH") or as objects ({"code": "PH", "label": "..."})."""
    if isinstance(r, str):
        code = r
    elif isinstance(r, dict):
        code = next((r[k] for k in ("code", "regionCode", "region", "id", "value") if isinstance(r.get(k), str)), "")
    else:
        code = ""
    code = code.strip().upper()
    return code if re.fullmatch(r"[A-Z]{2}", code) else ""


def region_label_from(r):
    if isinstance(r, dict):
        for k in ("label", "name", "title"):
            if isinstance(r.get(k), str) and r[k].strip():
                return r[k].strip()
    return ""


# ---- settings --------------------------------------------------------------
def get_settings(conn):
    return dict(conn.execute("SELECT * FROM pricing_settings WHERE id=1").fetchone())


def effective_fx(s):
    """Rate actually used for pricing. Manual override is used as typed; the automatic rate gets the safety factor."""
    if s["fx_mode"] == "manual" and s["fx_manual_micro"]:
        return s["fx_manual_micro"]
    if s["fx_auto_micro"]:
        return pricing.with_safety(s["fx_auto_micro"], s["fx_safety_bp"])
    return None


def audit(conn, core, actor, kind, message, plan_id="", region="", detail=""):
    conn.execute("INSERT INTO pricing_audit(created_at, actor, kind, plan_id, region_code, message, detail) VALUES (?,?,?,?,?,?,?)",
                 (core.now(), actor, kind, plan_id, region, message[:500], detail[:2000]))


def save_settings(core, actor, fx_mode, fx_manual, safety_pct, anomaly_pct, default_markup_pct):
    if fx_mode not in ("auto", "manual"):
        raise PricingError("汇率模式不对")
    manual = None
    if fx_mode == "manual" or (fx_manual or "").strip():
        manual = pricing.parse_rate_micro(fx_manual) if (fx_manual or "").strip() else None
        if fx_mode == "manual" and (manual is None or not fx.MIN_RATE <= manual <= fx.MAX_RATE):
            raise PricingError("请填写合理的人工汇率（1 美元兑多少人民币，例如 7.20）")
    try:
        safety = pricing.parse_percent_bp(safety_pct or "0")
        anomaly = pricing.parse_percent_bp(anomaly_pct or "10")
        markup = pricing.parse_percent_bp(default_markup_pct or "20")
    except ValueError as e:
        raise PricingError(str(e))
    if safety > 2000 or not 100 <= anomaly <= 10000 or markup > 100000:
        raise PricingError("安全系数最大 20%，异常阈值需在 1%~100% 之间")
    with tx(core.conn) as c:
        c.execute("UPDATE pricing_settings SET fx_mode=?, fx_manual_micro=COALESCE(?, fx_manual_micro), fx_safety_bp=?, anomaly_bp=?,"
                  " default_markup_bp=? WHERE id=1", (fx_mode, manual, safety, anomaly, markup))
        audit(c, core, actor, "settings", "更新汇率与保护设置：%s，安全系数 %s%%，异常阈值 %s%%" % (
            "人工覆盖汇率" if fx_mode == "manual" else "自动汇率", safety_pct or 0, anomaly_pct or 10))
        _recalc_all(c, core, actor, "设置变更")


# ---- products / variants -----------------------------------------------------
def ensure_plan_products(conn, core, actor="system"):
    now = core.now()
    out = {}
    for plan_id, label, desc in PLANS:
        rows = conn.execute("SELECT * FROM products WHERE plan_id=? AND type='api_recharge' ORDER BY id", (plan_id,)).fetchall()
        if not rows:
            cur = conn.execute("INSERT INTO products(name, description, type, price_cents, enabled, max_per_order, plan_id, regions,"
                               " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (label, desc, "api_recharge", 1, 1, 1, plan_id, "", now, now))
            out[plan_id] = cur.lastrowid
            audit(conn, core, actor, "product", "创建套餐 %s" % label, plan_id)
            continue
        out[plan_id] = rows[0]["id"]
        if not rows[0]["enabled"]:
            conn.execute("UPDATE products SET enabled=1, updated_at=? WHERE id=?", (now, rows[0]["id"]))
        for extra in rows[1:]:      # duplicates from older versions would show a second card for the same plan
            if extra["enabled"]:
                conn.execute("UPDATE products SET enabled=0, updated_at=? WHERE id=?", (now, extra["id"]))
                audit(conn, core, actor, "product", "停用重复的套餐商品 #%d" % extra["id"], plan_id)
    return out


def has_variants(conn, plan_id):
    return conn.execute("SELECT 1 FROM plan_variants WHERE plan_id=? LIMIT 1", (plan_id,)).fetchone() is not None


def sellable_variants(conn, plan_id):
    return conn.execute("SELECT * FROM plan_variants WHERE plan_id=? AND enabled=1 AND sale_price_cny IS NOT NULL ORDER BY id",
                        (plan_id,)).fetchall()


def variant_for_sale(conn, plan_id, region):
    """The only way an order may get a price for a variant-priced plan."""
    return conn.execute("SELECT * FROM plan_variants WHERE plan_id=? AND region_code=? AND enabled=1 AND sale_price_cny IS NOT NULL",
                        (plan_id, (region or "").upper())).fetchone()


def _new_variant(conn, core, product_id, plan_id, region, s, upstream_label=""):
    now = core.now()
    label = humanize.region_name(region)
    if label == region and upstream_label:          # a region we have no Chinese name for: use the upstream's own label
        label = upstream_label
    conn.execute(
        "INSERT INTO plan_variants(product_id, plan_id, plan_label, region_code, region_label, region_currency, markup_basis_points,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (product_id, plan_id, PLAN_LABEL[plan_id], region, label, REGION_CURRENCY.get(region, ""),
         s["default_markup_bp"], now, now))


def _recalc_variant(conn, core, v, s, actor, reason):
    """Recompute cost / price for one variant row (dict).

    percentage/fixed: need a valid package cost; keep previous sale price when cost is missing.
    manual_retail: sale_price always equals manual_sale_price_cny; cost/FX sync never overwrites it.
    """
    fx_micro = effective_fx(s)
    eff, used = pricing.effective_package_cost(v["cost_source"], v["upstream_cost_usd_cents"], v["manual_package_cost_cny"], fx_micro)
    total = (eff + v["other_cost_cny"]) if eff is not None else None
    mode = v["pricing_mode"]
    now = core.now()

    if mode == "manual_retail":
        manual = v.get("manual_sale_price_cny")
        if manual is None or manual <= 0:
            return False
        price = manual
        conn.execute(
            "UPDATE plan_variants SET effective_package_cost_cny=?, total_cost_cny=?, sale_price_cny=?, fx_rate_snapshot=?,"
            " price_calculated_at=?, updated_at=? WHERE id=?",
            (eff, total, price, fx_micro if used == "upstream" else None, now, now, v["id"]))
        # Cost-only sync must not audit a "price change" when retail is unchanged.
        if price != v["sale_price_cny"]:
            audit(conn, core, actor, "price_change", "%s %s 售价 %s → %s（%s）" % (
                v["plan_label"], v["region_label"], pricing.fmt_cny(v["sale_price_cny"]), pricing.fmt_cny(price), reason),
                v["plan_id"], v["region_code"],
                json.dumps({"total_cost": total, "package_cost": eff, "fx": fx_micro, "source": used, "mode": mode}))
            return True
        return False

    if eff is None:
        return False
    price = pricing.compute_price(total, mode, v["markup_basis_points"], v["fixed_profit_cny"], v["rounding"])
    conn.execute("UPDATE plan_variants SET effective_package_cost_cny=?, total_cost_cny=?, sale_price_cny=?, fx_rate_snapshot=?,"
                 " price_calculated_at=?, updated_at=? WHERE id=?", (eff, total, price, fx_micro if used == "upstream" else None, now, now, v["id"]))
    if price != v["sale_price_cny"]:
        audit(conn, core, actor, "price_change", "%s %s 售价 %s → %s（%s）" % (
            v["plan_label"], v["region_label"], pricing.fmt_cny(v["sale_price_cny"]), pricing.fmt_cny(price), reason),
            v["plan_id"], v["region_code"], json.dumps({"total_cost": total, "package_cost": eff, "fx": fx_micro, "source": used}))
    return price != v["sale_price_cny"]


def _recalc_all(conn, core, actor, reason):
    s = get_settings(conn)
    n = 0
    for r in conn.execute("SELECT * FROM plan_variants").fetchall():
        if _recalc_variant(conn, core, dict(r), s, actor, reason):
            n += 1
    return n


def recalc_all(core, actor):
    with tx(core.conn) as c:
        n = _recalc_all(c, core, actor, "手动重新计算")
        audit(c, core, actor, "recalc", "手动重新计算全部售价，%d 个价格有变化" % n)
    return n


# ---- upstream cost sync --------------------------------------------------------
def parse_costs(data):
    """{plan_id: {REGION: usd_cents|None}} for the four allowed plans only.
    Regions come from planRegions; cost from wallet.prices (number or null); regionalDisplayPrices is ignored on purpose."""
    if not isinstance(data, dict) or not isinstance(data.get("planRegions"), dict):
        raise ValueError("planRegions missing")
    prices = ((data.get("wallet") or {}).get("prices")) if isinstance(data.get("wallet"), dict) else None
    prices = prices if isinstance(prices, dict) else {}
    out = {}
    for plan_id in ALLOWED:
        regions = data["planRegions"].get(plan_id)
        if not isinstance(regions, list):
            continue
        plan_prices = prices.get(plan_id) if isinstance(prices.get(plan_id), dict) else {}
        costs = {}
        for r in regions:
            code = region_code(r)
            if not code:
                continue
            raw = plan_prices.get(code, plan_prices.get(code.lower()))
            costs[code] = (pricing.parse_usd_cents(raw), region_label_from(r))
        out[plan_id] = costs
    return out


def _record_upstream_failure(core, actor, msg, detail=""):
    with tx(core.conn) as c:
        c.execute("UPDATE pricing_settings SET upstream_last_try_at=?, upstream_last_error=? WHERE id=1", (core.now(), msg))
        audit(c, core, actor, "sync_failed", "上游成本同步失败：" + msg, detail=detail)


def sync_costs(core, actor="system"):
    """Returns (ok, plain_message, variant_count). On any failure old data is kept untouched."""
    try:
        client = settings.upstream_client(core, require_enabled=False)
    except settings.Unavailable as e:
        _record_upstream_failure(core, actor, str(e))
        return False, str(e), 0
    try:
        resp = client.config()
    except UpstreamTransportError as e:
        msg = "连不上上游服务，已保留上一次的成本。"
        _record_upstream_failure(core, actor, msg, "network: %s" % e)
        return False, msg, 0
    if not resp.ok:
        msg = humanize.explain(resp.error_code, resp.http_status) + "已保留上一次的成本。"
        _record_upstream_failure(core, actor, msg, "http=%s code=%s" % (resp.http_status, resp.error_code))
        return False, msg, 0
    try:
        costs = parse_costs(resp.data)
    except ValueError:
        msg = "上游返回的内容无法识别，已保留上一次的成本。"
        _record_upstream_failure(core, actor, msg, "planRegions missing")
        return False, msg, 0

    now = core.now()
    count = 0
    with tx(core.conn) as c:
        s = get_settings(c)
        products = ensure_plan_products(c, core, actor)
        seen = set()
        for plan_id, regions in costs.items():
            for code, (new_cents, up_label) in regions.items():
                row = c.execute("SELECT * FROM plan_variants WHERE plan_id=? AND region_code=?", (plan_id, code)).fetchone()
                if row is None:
                    _new_variant(c, core, products[plan_id], plan_id, code, s, up_label)
                    row = c.execute("SELECT * FROM plan_variants WHERE plan_id=? AND region_code=?", (plan_id, code)).fetchone()
                v = dict(row)
                seen.add(v["id"])
                count += 1
                _apply_reading(c, core, v, new_cents, s, actor, now)
        for r in c.execute("SELECT id, plan_label, region_label, plan_id, region_code, upstream_available FROM plan_variants").fetchall():
            if r["id"] not in seen and r["upstream_available"]:
                c.execute("UPDATE plan_variants SET upstream_available=0, updated_at=? WHERE id=?", (now, r["id"]))
                audit(c, core, actor, "region_removed", "%s %s：上游已不再提供这个地区" % (r["plan_label"], r["region_label"]), r["plan_id"], r["region_code"])
        c.execute("UPDATE pricing_settings SET upstream_last_ok_at=?, upstream_last_try_at=?, upstream_last_error='' WHERE id=1", (now, now))
        audit(c, core, actor, "sync_ok", "上游成本同步成功，共 %d 个套餐地区" % count)
    return True, "已同步 %d 个套餐地区的上游成本。" % count, count


def _apply_reading(c, core, v, new_cents, s, actor, now):
    old = v["upstream_cost_usd_cents"]
    if not v["upstream_available"]:
        c.execute("UPDATE plan_variants SET upstream_available=1 WHERE id=?", (v["id"],))
    if new_cents is None:                                   # null != 0: keep the last valid cost
        c.execute("UPDATE plan_variants SET upstream_latest_null=1, upstream_synced_at=?, updated_at=? WHERE id=?", (now, now, v["id"]))
        if not v["upstream_latest_null"]:
            audit(c, core, actor, "cost_null", "%s %s：上游没有提供成本，保留最后一次有效成本" % (v["plan_label"], v["region_label"]), v["plan_id"], v["region_code"])
        return
    if old is not None and old > 0 and pricing.change_bp(old, new_cents) > s["anomaly_bp"]:
        bp = pricing.change_bp(old, new_cents)
        detail = "上游成本从 $%s 变为 $%s，变化 %.1f%%，超过阈值 %.1f%%，已暂停自动调价，等待确认。" % (
            pricing.fmt_usd(old), pricing.fmt_usd(new_cents), bp / 100, s["anomaly_bp"] / 100)
        c.execute("UPDATE plan_variants SET pending_upstream_cost_usd_cents=?, anomaly='成本异常，等待确认', anomaly_detail=?,"
                  " upstream_latest_null=0, upstream_synced_at=?, updated_at=? WHERE id=?", (new_cents, detail, now, now, v["id"]))
        audit(c, core, actor, "anomaly", "%s %s：%s" % (v["plan_label"], v["region_label"], detail), v["plan_id"], v["region_code"])
        return
    c.execute("UPDATE plan_variants SET upstream_cost_usd_cents=?, pending_upstream_cost_usd_cents=NULL, anomaly='', anomaly_detail='',"
              " upstream_latest_null=0, upstream_synced_at=?, updated_at=? WHERE id=?", (new_cents, now, now, v["id"]))
    if old != new_cents:
        audit(c, core, actor, "cost_sync", "%s %s：上游参考成本 $%s → $%s" % (v["plan_label"], v["region_label"], pricing.fmt_usd(old), pricing.fmt_usd(new_cents)),
              v["plan_id"], v["region_code"])
    fresh = dict(c.execute("SELECT * FROM plan_variants WHERE id=?", (v["id"],)).fetchone())
    _recalc_variant(c, core, fresh, s, actor, "上游成本同步")


# ---- FX sync -----------------------------------------------------------------------
def sync_fx(core, actor="system"):
    """Returns (ok, plain_message). Failure never touches the stored rate."""
    now = core.now()
    try:
        micro, source = (core.fx_provider or fx.fetch_rate)()
        if not micro or micro <= 0:
            raise fx.FxError("empty rate")
    except fx.FxError as e:
        msg = "汇率服务暂时不可用，继续使用上一次的汇率。"
        with tx(core.conn) as c:
            c.execute("UPDATE pricing_settings SET fx_last_try_at=?, fx_last_error=? WHERE id=1", (now, msg))
            audit(c, core, actor, "fx_failed", "汇率同步失败：" + msg, detail=str(e))
        return False, msg
    with tx(core.conn) as c:
        s = get_settings(c)
        old = s["fx_auto_micro"]
        if old and pricing.change_bp(old, micro) > s["anomaly_bp"]:
            bp = pricing.change_bp(old, micro)
            detail = "汇率从 %s 变为 %s，变化 %.1f%%，超过阈值 %.1f%%，已暂停自动调价，等待确认。" % (
                pricing.fmt_rate(old), pricing.fmt_rate(micro), bp / 100, s["anomaly_bp"] / 100)
            c.execute("UPDATE pricing_settings SET fx_pending_micro=?, fx_pending_detail=?, fx_auto_at=?, fx_auto_source=?,"
                      " fx_last_try_at=?, fx_last_error='' WHERE id=1", (micro, detail, now, source, now))
            audit(c, core, actor, "anomaly", "汇率异常：" + detail)
            return True, "汇率变化较大，已暂停自动调价，等待确认。"
        c.execute("UPDATE pricing_settings SET fx_auto_micro=?, fx_auto_at=?, fx_auto_source=?, fx_pending_micro=NULL, fx_pending_detail='',"
                  " fx_last_try_at=?, fx_last_error='' WHERE id=1", (micro, now, source, now))
        if old != micro:
            audit(c, core, actor, "fx_sync", "USD/CNY 汇率 %s → %s（%s）" % (pricing.fmt_rate(old), pricing.fmt_rate(micro), source))
            _recalc_all(c, core, actor, "汇率同步")
    return True, "汇率已更新为 %s。" % pricing.fmt_rate(micro)


def run_cycle(core, force=False, actor="system"):
    """One scheduled/manual sync. A DB lock makes sure two processes never run it at the same time.
    Returns None when skipped (not due yet or already running), else (fx_msg, cost_msg)."""
    now = core.now()
    with tx(core.conn) as c:
        r = c.execute("UPDATE pricing_settings SET sync_lock_until=? WHERE id=1 AND sync_lock_until<=? AND (?=1 OR next_sync_at<=?)",
                      (now + 600, now, 1 if force else 0, now))
        if r.rowcount != 1:
            return None
    try:
        fx_ok, fx_msg = sync_fx(core, actor)
        ok, msg, _ = sync_costs(core, actor)
    finally:
        core.conn.execute("UPDATE pricing_settings SET sync_lock_until=0, next_sync_at=? WHERE id=1", (core.now() + SYNC_INTERVAL,))
    return fx_msg, msg


# ---- admin actions --------------------------------------------------------------------
def _variant(c, vid):
    r = c.execute("SELECT * FROM plan_variants WHERE id=?", (vid,)).fetchone()
    if r is None:
        raise PricingError("找不到这个套餐地区")
    return dict(r)


def toggle_variant(core, actor, vid, enabled):
    with tx(core.conn) as c:
        v = _variant(c, vid)
        if enabled and v["sale_price_cny"] is None:
            if v["pricing_mode"] == "manual_retail":
                raise PricingError("「%s %s」还没有零售价，请先填写零售价再上架。" % (v["plan_label"], v["region_label"]))
            raise PricingError("「%s %s」还没有可用成本，请先填写套餐成本再上架。" % (v["plan_label"], v["region_label"]))
        c.execute("UPDATE plan_variants SET enabled=?, updated_at=? WHERE id=?", (1 if enabled else 0, core.now(), vid))
        audit(c, core, actor, "toggle", "%s %s：%s" % (v["plan_label"], v["region_label"], "上架销售" if enabled else "关闭销售"), v["plan_id"], v["region_code"])


def _apply_fields(v, f):
    """Validate admin input and return the column updates."""
    upd = {}
    if "cost_source" in f and f["cost_source"] != "":
        if f["cost_source"] not in ("upstream", "manual"):
            raise PricingError("成本来源不对")
        upd["cost_source"] = f["cost_source"]
    try:
        if f.get("manual_package_cost") is not None:
            upd["manual_package_cost_cny"] = pricing.parse_money_fen(f["manual_package_cost"]) if str(f["manual_package_cost"]).strip() else None
        if f.get("other_cost") not in (None, ""):
            upd["other_cost_cny"] = pricing.parse_money_fen(f["other_cost"])
        if f.get("markup_percent") not in (None, ""):
            upd["markup_basis_points"] = pricing.parse_percent_bp(f["markup_percent"])
        if f.get("fixed_profit") not in (None, ""):
            upd["fixed_profit_cny"] = pricing.parse_money_fen(f["fixed_profit"])
        if f.get("manual_sale_price") not in (None, ""):
            fen = pricing.parse_money_fen(f["manual_sale_price"])
            if fen <= 0:
                raise PricingError("零售价必须大于 0")
            upd["manual_sale_price_cny"] = fen
    except ValueError as e:
        raise PricingError(str(e))
    if f.get("pricing_mode"):
        if f["pricing_mode"] not in pricing.MODES:
            raise PricingError("加价方式不对")
        upd["pricing_mode"] = f["pricing_mode"]
    if f.get("rounding"):
        if f["rounding"] not in pricing.ROUNDINGS:
            raise PricingError("取整方式不对")
        upd["rounding"] = f["rounding"]
    if upd.get("markup_basis_points", 0) > 1_000_000:
        raise PricingError("加价比例过大")
    mode = upd.get("pricing_mode", v["pricing_mode"])
    if mode == "manual_retail":
        manual = upd.get("manual_sale_price_cny", v.get("manual_sale_price_cny"))
        if manual is None or manual <= 0:
            raise PricingError("请填写大于 0 的人民币零售价")
    return upd


def update_variant(core, actor, vid, fields, c=None):
    def run(c):
        v = _variant(c, vid)
        old_mode, old_price = v["pricing_mode"], v["sale_price_cny"]
        upd = _apply_fields(v, fields)
        if upd:
            cols = ", ".join("%s=?" % k for k in upd)
            c.execute("UPDATE plan_variants SET %s, updated_at=? WHERE id=?" % cols, list(upd.values()) + [core.now(), vid])
        fresh = _variant(c, vid)
        _recalc_variant(c, core, fresh, get_settings(c), actor, "管理员修改")
        fresh = _variant(c, vid)
        if fresh["enabled"] and fresh["sale_price_cny"] is None:
            raise PricingError("这个地区正在销售，不能清除售价；请先关闭销售。")
        detail = {"old_mode": old_mode, "old_sale_price_cny": old_price,
                  "new_mode": fresh["pricing_mode"], "new_sale_price_cny": fresh["sale_price_cny"],
                  "fields": {k: fields[k] for k in fields if k != "_csrf"}}
        audit(c, core, actor, "variant_update",
              "%s %s：%s ¥%s → %s ¥%s" % (
                  v["plan_label"], v["region_label"],
                  pricing.MODE_TEXT.get(old_mode, old_mode), pricing.fmt_cny(old_price),
                  pricing.MODE_TEXT.get(fresh["pricing_mode"], fresh["pricing_mode"]), pricing.fmt_cny(fresh["sale_price_cny"])),
              v["plan_id"], v["region_code"], json.dumps(detail, ensure_ascii=False))
    if c is not None:
        return run(c)
    with tx(core.conn) as c:
        return run(c)


def bulk(core, actor, plan_id, action, variant_ids, params):
    """Batch operations on one plan. Returns a plain-language summary. Invalid input rolls the whole transaction back."""
    if plan_id not in ALLOWED:
        raise PricingError("套餐不存在")
    with tx(core.conn) as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM plan_variants WHERE plan_id=? ORDER BY id", (plan_id,)).fetchall()]
        chosen = [r for r in rows if not variant_ids or r["id"] in set(variant_ids)]
        if action in ("enable", "disable"):
            if not variant_ids:
                raise PricingError("请先勾选要操作的地区。")
            done = skipped = 0
            for r in chosen:
                if action == "enable" and r["sale_price_cny"] is None:
                    skipped += 1
                    continue
                c.execute("UPDATE plan_variants SET enabled=?, updated_at=? WHERE id=?", (1 if action == "enable" else 0, core.now(), r["id"]))
                done += 1
            audit(c, core, actor, "bulk", "%s：批量%s %d 个地区" % (PLAN_LABEL[plan_id], "上架" if action == "enable" else "关闭", done), plan_id)
            return "已%s %d 个地区。" % ("上架" if action == "enable" else "关闭", done) + (" 另有 %d 个地区还没有成本，已跳过。" % skipped if skipped else "")
        if action == "retail":
            if not variant_ids:
                raise PricingError("请先勾选要设置零售价的地区。")
            if str(params.get("value", "")).strip() == "":
                raise PricingError("请填写零售价。")
            try:
                fen = pricing.parse_money_fen(params.get("value", ""))
            except ValueError as e:
                raise PricingError(str(e))
            if fen <= 0:
                raise PricingError("零售价必须大于 0")
            labels = []
            for r in chosen:
                before = _variant(c, r["id"])
                update_variant(core, actor, r["id"],
                               {"pricing_mode": "manual_retail", "manual_sale_price": params.get("value", "")}, c=c)
                after = _variant(c, r["id"])
                labels.append(after["region_label"])
                # Setting retail price must NOT auto-enable the region.
                if before["enabled"] == 0 and after["enabled"] != 0:
                    raise PricingError("批量设置零售价不能自动上架")
            audit(c, core, actor, "bulk",
                  "%s：批量直接零售价 ¥%s，地区：%s" % (PLAN_LABEL[plan_id], pricing.fmt_cny(fen), "、".join(labels)),
                  plan_id, detail=json.dumps({"variant_ids": variant_ids, "manual_sale_price_cny": fen, "regions": labels},
                                             ensure_ascii=False))
            return "已为 %d 个地区设置零售价 ¥%s（未自动上架）：%s。" % (len(chosen), pricing.fmt_cny(fen), "、".join(labels))
        field_map = {"markup": {"pricing_mode": "percentage", "markup_percent": params.get("value", ""), "rounding": params.get("rounding", "")},
                     "profit": {"pricing_mode": "fixed", "fixed_profit": params.get("value", ""), "rounding": params.get("rounding", "")},
                     "other_cost": {"other_cost": params.get("value", "")}}
        if action not in field_map:
            raise PricingError("不支持的批量操作")
        if str(params.get("value", "")).strip() == "":
            raise PricingError("请填写数值。")
        for r in chosen:
            update_variant(core, actor, r["id"], field_map[action], c=c)
        audit(c, core, actor, "bulk", "%s：批量设置（%s）共 %d 个地区" % (PLAN_LABEL[plan_id], action, len(chosen)), plan_id)
        return "已更新 %d 个地区并重新计算售价。" % len(chosen)


def confirm_variant(core, actor, vid):
    with tx(core.conn) as c:
        v = _variant(c, vid)
        if v["pending_upstream_cost_usd_cents"] is None and not v["anomaly"]:
            return False
        if v["pending_upstream_cost_usd_cents"] is not None:
            c.execute("UPDATE plan_variants SET upstream_cost_usd_cents=pending_upstream_cost_usd_cents WHERE id=?", (vid,))
        c.execute("UPDATE plan_variants SET pending_upstream_cost_usd_cents=NULL, anomaly='', anomaly_detail='', updated_at=? WHERE id=?", (core.now(), vid))
        _recalc_variant(c, core, _variant(c, vid), get_settings(c), actor, "管理员确认异常价格")
        audit(c, core, actor, "confirm", "%s %s：管理员确认了异常价格" % (v["plan_label"], v["region_label"]), v["plan_id"], v["region_code"])
        return True


def confirm_fx(core, actor):
    with tx(core.conn) as c:
        s = get_settings(c)
        if not s["fx_pending_micro"]:
            return False
        c.execute("UPDATE pricing_settings SET fx_auto_micro=fx_pending_micro, fx_pending_micro=NULL, fx_pending_detail='' WHERE id=1")
        audit(c, core, actor, "confirm", "管理员确认了新汇率 %s" % pricing.fmt_rate(s["fx_pending_micro"]))
        _recalc_all(c, core, actor, "确认新汇率")
        return True


# ---- read models for the UI ---------------------------------------------------------------
def staleness(core):
    s = get_settings(core.conn)
    now = core.now()
    out = {}
    for key, ts, what in (("upstream", s["upstream_last_ok_at"], "上游成本"), ("fx", s["fx_auto_at"], "汇率")):
        if ts is None:
            out[key] = {"level": "none", "text": "%s还没有同步过。" % what}
            continue
        age = now - ts
        level = "err" if age > STALE_ERR else ("warn" if age > STALE_WARN else "ok")
        out[key] = {"level": level, "age": age, "text": ("%s已超过 %d 小时没有同步成功，请检查。" % (what, age // 3600)) if level != "ok" else "%s同步正常。" % what}
    return out


def fx_view(core):
    s = get_settings(core.conn)
    eff = effective_fx(s)
    return {"auto": s["fx_auto_micro"], "auto_at": s["fx_auto_at"], "source": s["fx_auto_source"], "effective": eff,
            "manual_on": s["fx_mode"] == "manual" and bool(s["fx_manual_micro"]), "manual": s["fx_manual_micro"], "safety_bp": s["fx_safety_bp"],
            "pending": s["fx_pending_micro"], "pending_detail": s["fx_pending_detail"], "error": s["fx_last_error"],
            "anomaly_bp": s["anomaly_bp"], "default_markup_bp": s["default_markup_bp"], "raw": s}


def strategy_text(v):
    if v["pricing_mode"] == "manual_retail":
        return "直接零售价 ¥%s" % pricing.fmt_cny(v["manual_sale_price_cny"] or v["sale_price_cny"])
    if v["pricing_mode"] == "percentage":
        return "成本加价 %s%%" % ("%g" % (v["markup_basis_points"] / 100))
    return "固定利润 ¥%s" % pricing.fmt_cny(v["fixed_profit_cny"])


def cost_note(v):
    """Plain-language status of where this variant's cost stands."""
    if v["cost_source"] == "manual":
        if v["manual_package_cost_cny"] is None:
            if v["upstream_cost_usd_cents"] is not None:
                return "上游参考 $%s，点「采用上游参考成本」后才会用来定价" % pricing.fmt_usd(v["upstream_cost_usd_cents"])
            return "上游未提供成本，需要人工填写"
        return "使用人工套餐成本"
    if v["upstream_cost_usd_cents"] is None:
        return "上游未提供成本，需要人工填写" if v["manual_package_cost_cny"] is None else "上游未提供成本，暂用人工成本"
    return "使用上游参考成本" + ("（上游最近一次没有返回成本，沿用上一次的值）" if v["upstream_latest_null"] else "")


def plan_cards(core):
    c = core.conn
    s = get_settings(c)
    fx_micro = effective_fx(s)
    cards = []
    for plan_id, label, desc in PLANS:
        prod = c.execute("SELECT * FROM products WHERE plan_id=? AND type='api_recharge' ORDER BY id LIMIT 1", (plan_id,)).fetchone()
        rows = [dict(r) for r in c.execute("SELECT * FROM plan_variants WHERE plan_id=? ORDER BY region_code", (plan_id,)).fetchall()]
        for v in rows:
            v["cost_note"] = cost_note(v)
            v["strategy"] = strategy_text(v)
            v["profit"] = None if v["sale_price_cny"] is None or v["total_cost_cny"] is None else v["sale_price_cny"] - v["total_cost_cny"]
            v["loss"] = bool(v["profit"] is not None and v["profit"] < 0)
            v["can_sell"] = v["sale_price_cny"] is not None
            v["upstream_usd_text"] = pricing.fmt_usd(v["upstream_cost_usd_cents"]) if v["upstream_cost_usd_cents"] is not None else None
        live = [v for v in rows if v["enabled"] and v["sale_price_cny"] is not None]
        strategies = {v["strategy"] for v in rows}
        synced = [v["upstream_synced_at"] for v in rows if v["upstream_synced_at"]]
        cards.append({"plan_id": plan_id, "label": label, "desc": desc, "product": prod, "variants": rows, "live": len(live), "total": len(rows),
                      "min_price": min([v["sale_price_cny"] for v in live], default=None), "last_sync": max(synced, default=None),
                      "strategy": (strategies.pop() if len(strategies) == 1 else ("各地区不同" if strategies else "尚未设置")),
                      "anomalies": sum(1 for v in rows if v["anomaly"])})
    return cards


def recent_audit(conn, limit=30):
    return conn.execute("SELECT * FROM pricing_audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

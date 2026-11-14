"""Admin operations: order search, buyer/account summaries, timelines, and period stats.

Money stays in integer fen. Revenue uses paid_at; order volume uses created_at.
Display timezone defaults to Asia/Manila (UTC+8) and never follows the OS local TZ.
Full emails are for admin callers only — this module does not mask; routes must not
expose these structures to public endpoints.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import humanize

DISPLAY_TZ = ZoneInfo("Asia/Manila")
TZ_LABEL = "Asia/Manila (UTC+8)"
PAGE_SIZE_DEFAULT = 50

STATUS_SQL = {
    "unpaid": "o.payment_status='unpaid'",
    "processing": "o.payment_status='paid' AND f.status IN ('pending','processing')",
    "attention": "o.payment_status='paid' AND f.status IN ('blocked','failed','manual')",
    "done": "f.status='delivered'",
    "closed": "o.payment_status='closed'",
}

# Timeline: kind -> (stable step key, Chinese title). Duplicate payment_confirmed collapses to one node.
_TIMELINE_KIND = {
    "order_created": ("created", "创建订单"),
    "payment_confirmed": ("paid", "支付确认"),
    "order_closed": ("closed", "订单关闭"),
    "delivered": ("done", "充值完成"),
    "fulfill_blocked": ("attention", "需要处理"),
    "fulfill_failed": ("failed", "充值失败"),
    "fulfill_manual": ("manual", "转人工核对"),
    "blocked": ("attention", "需要处理"),
    "admin_retry": ("retry", "管理员重试"),
    "admin_mark": ("admin", "管理员手动处理"),
}


class OpsError(Exception):
    pass


def norm_email(value):
    return (value or "").strip().lower()


def format_ts(ts, tz=DISPLAY_TZ):
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=tz).strftime("%Y-%m-%d %H:%M:%S")


def period_bounds(period, now_ts, tz=DISPLAY_TZ):
    """Return [start, end) UTC unix seconds for today|yesterday|7d|30d in the display TZ."""
    now = datetime.fromtimestamp(int(now_ts), tz=tz)
    start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period in ("today", ""):
        start, end = start_today, start_today + timedelta(days=1)
    elif period == "yesterday":
        start, end = start_today - timedelta(days=1), start_today
    elif period in ("7d", "last7"):
        start, end = start_today - timedelta(days=6), start_today + timedelta(days=1)
    elif period in ("30d", "last30"):
        start, end = start_today - timedelta(days=29), start_today + timedelta(days=1)
    else:
        raise OpsError("不支持的时间范围")
    return int(start.timestamp()), int(end.timestamp())


def custom_bounds(date_from, date_to, tz=DISPLAY_TZ):
    """YYYY-MM-DD strings in display TZ -> [start, end) UTC unix. Inclusive calendar days."""
    try:
        start = datetime.strptime((date_from or "").strip(), "%Y-%m-%d").replace(tzinfo=tz)
        end_day = datetime.strptime((date_to or "").strip(), "%Y-%m-%d").replace(tzinfo=tz)
    except ValueError:
        raise OpsError("日期格式不对，请使用 YYYY-MM-DD")
    if end_day < start:
        raise OpsError("结束日期不能早于开始日期")
    end = end_day + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def resolve_range(now_ts, period="", date_from="", date_to=""):
    if (date_from or "").strip() and (date_to or "").strip():
        return custom_bounds(date_from, date_to)
    return period_bounds(period or "today", now_ts)


def _row(r):
    return dict(r) if r is not None else None


def _needs_attention(o, f):
    return o["payment_status"] == "paid" and f["status"] in ("blocked", "failed", "manual")


def _order_rank(conn, lookup_email, order_id):
    email = norm_email(lookup_email)
    if not email or not order_id:
        return None
    rows = conn.execute(
        "SELECT id FROM orders WHERE lower(trim(lookup_email))=? ORDER BY created_at ASC, id ASC",
        (email,)).fetchall()
    for i, r in enumerate(rows, 1):
        if r["id"] == order_id:
            return i
    return None


def search_orders(core, status="all", period="", date_from="", date_to="", plan_id="", region="",
                  order_no="", lookup_email="", recharge_email="", customer="all",
                  page=1, page_size=PAGE_SIZE_DEFAULT, q=""):
    """Admin order list. Returns {rows, total, page, page_size, tz_label}."""
    page = max(1, int(page or 1))
    page_size = max(1, min(100, int(page_size or PAGE_SIZE_DEFAULT)))
    where, args = ["1=1"], []

    if status in STATUS_SQL:
        where.append(STATUS_SQL[status])

    if period or (date_from and date_to):
        start, end = resolve_range(core.now(), period, date_from, date_to)
        where.append("o.created_at>=? AND o.created_at<?")
        args += [start, end]

    plan_id = (plan_id or "").strip()
    if plan_id:
        where.append("o.recharge_plan_id=?")
        args.append(plan_id)

    region = (region or "").strip().upper()
    if region:
        where.append("upper(o.recharge_region)=?")
        args.append(region)

    # One search box: an e-mail matches the contact e-mail OR the full recharge e-mail (exact, case-insensitive);
    # anything else is treated as (part of) an order number.
    q = (q or "").strip()
    if q:
        if "@" in q:
            qe = norm_email(q)
            where.append("(lower(trim(o.lookup_email))=? OR lower(trim(o.recharge_email))=?)")
            args += [qe, qe]
        else:
            where.append("o.order_no LIKE ?")
            args.append("%" + q + "%")

    order_no = (order_no or "").strip()
    if order_no:
        where.append("o.order_no LIKE ?")
        args.append("%" + order_no + "%")

    le = norm_email(lookup_email)
    if le:
        where.append("lower(trim(o.lookup_email))=?")
        args.append(le)

    re_ = norm_email(recharge_email)
    if re_:
        where.append("lower(trim(o.recharge_email))=?")
        args.append(re_)

    if customer in ("first", "repeat"):
        # First order for this lookup_email (by created_at, id).
        first_sql = (
            "o.id=(SELECT o2.id FROM orders o2 WHERE lower(trim(o2.lookup_email))=lower(trim(o.lookup_email))"
            " AND trim(o.lookup_email)!='' ORDER BY o2.created_at ASC, o2.id ASC LIMIT 1)")
        where.append(first_sql if customer == "first" else "NOT (" + first_sql + ")")
        where.append("trim(o.lookup_email)!=''")

    sql_where = " AND ".join(where)
    total = core.conn.execute(
        "SELECT COUNT(*) FROM orders o JOIN fulfillments f ON f.order_id=o.id WHERE " + sql_where, args
    ).fetchone()[0]

    rows = core.conn.execute(
        "SELECT o.*, f.status AS fstatus, f.reason AS freason, f.reason_code AS freason_code,"
        " f.delivered_at AS fdelivered_at, f.upstream_task_id AS fupstream_task_id,"
        " f.updated_at AS fupdated_at"
        " FROM orders o JOIN fulfillments f ON f.order_id=o.id"
        " WHERE %s ORDER BY o.created_at DESC, o.id DESC LIMIT ? OFFSET ?" % sql_where,
        args + [page_size, (page - 1) * page_size]).fetchall()

    out = []
    for r in rows:
        o = dict(r)
        f = {"status": o.pop("fstatus"), "reason": o.pop("freason"), "reason_code": o.pop("freason_code"),
             "delivered_at": o.pop("fdelivered_at"), "upstream_task_id": o.pop("fupstream_task_id"),
             "updated_at": o.pop("fupdated_at")}
        le_n = norm_email(o["lookup_email"])
        lookup_order_count = 0
        if le_n:
            lookup_order_count = core.conn.execute(
                "SELECT COUNT(*) FROM orders WHERE lower(trim(lookup_email))=?", (le_n,)).fetchone()[0]
        out.append({
            "id": o["id"],
            "order_no": o["order_no"],
            "created_at": o["created_at"],
            "created_at_text": format_ts(o["created_at"]),
            "paid_at": o["paid_at"],
            "paid_at_text": format_ts(o["paid_at"]),
            "closed_at": o["closed_at"],
            "closed_at_text": format_ts(o["closed_at"]),
            "completed_at": f["delivered_at"],
            "completed_at_text": format_ts(f["delivered_at"]),
            "plan_id": o["recharge_plan_id"],
            "plan_label": o["snap_plan_label"] or o["product_name"],
            "region": o["recharge_region"],
            "region_label": o["snap_region_label"] or humanize.region_name(o["recharge_region"]),
            "amount_cents": o["amount_cents"],
            "payment_status": o["payment_status"],
            "fulfillment_status": f["status"],
            "lookup_email": o["lookup_email"],
            "recharge_email": o["recharge_email"],
            "contact": o["contact"],
            "lookup_order_count": lookup_order_count,
            "lookup_order_rank": _order_rank(core.conn, o["lookup_email"], o["id"]),
            "needs_attention": _needs_attention(o, f),
            "attention_reason": f["reason"] if _needs_attention(o, f) else "",
        })
    return {"rows": out, "total": total, "page": page, "page_size": page_size,
            "tz_label": TZ_LABEL, "pages": max(1, (total + page_size - 1) // page_size)}


def buyer_summary(core, lookup_email, exclude_order_id=None):
    """Aggregate by contact/lookup email. exclude_order_id avoids double-counting when composing cards."""
    email = norm_email(lookup_email)
    empty = {"email": email, "orders": 0, "paid_orders": 0, "success_orders": 0, "open_orders": 0,
             "paid_amount_cents": 0, "first_order_at": None, "first_order_at_text": None,
             "last_order_at": None, "last_order_at_text": None, "recent_order_ids": []}
    if not email:
        return empty
    extra, args = "", [email]
    if exclude_order_id:
        extra = " AND o.id!=?"
        args.append(exclude_order_id)
    rows = core.conn.execute(
        "SELECT o.id, o.created_at, o.paid_at, o.amount_cents, o.payment_status, f.status AS fstatus"
        " FROM orders o JOIN fulfillments f ON f.order_id=o.id"
        " WHERE lower(trim(o.lookup_email))=?" + extra + " ORDER BY o.created_at DESC, o.id DESC", args).fetchall()
    if not rows:
        return empty
    paid_orders = success = open_ = paid_amount = 0
    first_at = last_at = None
    for r in rows:
        last_at = last_at or r["created_at"]
        first_at = r["created_at"]
        if r["payment_status"] == "paid":
            paid_orders += 1
            paid_amount += r["amount_cents"]
        if r["fstatus"] == "delivered":
            success += 1
        if r["payment_status"] == "paid" and r["fstatus"] in ("pending", "processing", "blocked", "failed", "manual"):
            open_ += 1
    # first_at was last in ASC walk — recompute
    first_at = min(r["created_at"] for r in rows)
    last_at = max(r["created_at"] for r in rows)
    return {
        "email": email,
        "orders": len(rows),
        "paid_orders": paid_orders,
        "success_orders": success,
        "open_orders": open_,
        "paid_amount_cents": paid_amount,
        "first_order_at": first_at,
        "first_order_at_text": format_ts(first_at),
        "last_order_at": last_at,
        "last_order_at_text": format_ts(last_at),
        "recent_order_ids": [r["id"] for r in rows[:20]],
    }


def recharge_account_summary(core, recharge_email, exclude_order_id=None):
    email = norm_email(recharge_email)
    empty = {"email": email, "recharge_orders": 0, "success_count": 0, "last_success_at": None,
             "last_success_at_text": None, "last_plan_label": "", "last_region_label": "", "recent_order_ids": []}
    if not email:
        return empty
    extra, args = "", [email]
    if exclude_order_id:
        extra = " AND o.id!=?"
        args.append(exclude_order_id)
    rows = core.conn.execute(
        "SELECT o.id, o.created_at, o.snap_plan_label, o.product_name, o.snap_region_label, o.recharge_region,"
        " f.status AS fstatus, f.delivered_at"
        " FROM orders o JOIN fulfillments f ON f.order_id=o.id"
        " WHERE o.product_type='api_recharge' AND lower(trim(o.recharge_email))=?" + extra +
        " ORDER BY o.created_at DESC, o.id DESC", args).fetchall()
    if not rows:
        return empty
    success_rows = [r for r in rows if r["fstatus"] == "delivered"]
    last_ok = success_rows[0] if success_rows else None
    latest = rows[0]
    return {
        "email": email,
        "recharge_orders": len(rows),
        "success_count": len(success_rows),
        "last_success_at": last_ok["delivered_at"] if last_ok else None,
        "last_success_at_text": format_ts(last_ok["delivered_at"]) if last_ok else None,
        "last_plan_label": (latest["snap_plan_label"] or latest["product_name"] or ""),
        "last_region_label": (latest["snap_region_label"] or humanize.region_name(latest["recharge_region"])),
        "recent_order_ids": [r["id"] for r in rows[:20]],
    }


def order_timeline(core, order_id):
    """Stable ops timeline. Duplicate payment_confirmed collapses; technical codes stay in detail."""
    o = _row(core.conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone())
    if not o:
        raise OpsError("订单不存在")
    f = _row(core.conn.execute("SELECT * FROM fulfillments WHERE order_id=?", (order_id,)).fetchone())
    events = core.conn.execute(
        "SELECT * FROM events WHERE order_id=? ORDER BY id ASC", (order_id,)).fetchall()

    nodes, seen_paid = [], False
    for e in events:
        kind = e["kind"]
        if kind == "payment_confirmed":
            if seen_paid:
                continue
            seen_paid = True
        meta = _TIMELINE_KIND.get(kind)
        if not meta:
            # Skip noisy transport / signature events from the main ops view.
            if kind.startswith("notify_") or kind in ("payment_unknown_order", "fulfill_exception",
                                                      "payment_amount_mismatch", "payment_rejected"):
                continue
            title = humanize.EVENT_TEXT.get(kind, kind)
            step = kind
        else:
            step, title = meta
        nodes.append({
            "step": step,
            "title": title,
            "at": e["created_at"],
            "at_text": format_ts(e["created_at"]),
            "message": e["message"],
            "detail": e["detail"] or "",
            "kind": kind,
        })

    # Synthesize missing "生成付款码" / "进入充值队列" / "提交上游" when state implies them.
    def _has(step):
        return any(n["step"] == step for n in nodes)

    if o["precreate_attempted"] and not _has("qr"):
        # Place after created: use created_at + 1s synthetic marker only if no richer event exists.
        at = o["created_at"]
        nodes.append({"step": "qr", "title": "生成付款码", "at": at, "at_text": format_ts(at),
                      "message": "已向支付宝申请付款码" if o["qr_code"] else "已尝试生成付款码",
                      "detail": "", "kind": "synthetic_qr"})
    if o["payment_status"] == "paid" and f and f["status"] not in ("none",) and not _has("queued"):
        at = o["paid_at"] or o["created_at"]
        nodes.append({"step": "queued", "title": "进入充值队列", "at": at, "at_text": format_ts(at),
                      "message": "付款确认后进入自动履约队列", "detail": "", "kind": "synthetic_queue"})
    if f and f["upstream_task_id"] and not _has("submitted"):
        # Prefer earliest fulfill/processing-related event time; else fulfillment.updated_at.
        at = f["updated_at"] or o["paid_at"] or o["created_at"]
        nodes.append({"step": "submitted", "title": "提交上游", "at": at, "at_text": format_ts(at),
                      "message": "已提交上游充值任务", "detail": "task=" + f["upstream_task_id"], "kind": "synthetic_submit"})
    if f and f["status"] == "processing" and not any(n["step"] == "upstream" for n in nodes):
        nodes.append({"step": "upstream", "title": "上游处理中", "at": f["updated_at"] or core.now(),
                      "at_text": format_ts(f["updated_at"] or core.now()),
                      "message": "上游正在处理", "detail": f.get("upstream_status") or "", "kind": "synthetic_upstream"})

    nodes.sort(key=lambda n: (n["at"] or 0, n["kind"]))
    return {"tz_label": TZ_LABEL, "nodes": nodes}


def _sum_snapshots(conn, start, end):
    """Revenue-period snapshot totals. Missing snapshots are excluded (not treated as 0)."""
    rows = conn.execute(
        "SELECT snap_total_cost_cny, snap_expected_profit_cny, amount_cents FROM orders"
        " WHERE payment_status='paid' AND paid_at>=? AND paid_at<?", (start, end)).fetchall()
    cost = profit = None
    cost_n = profit_n = 0
    for r in rows:
        if r["snap_total_cost_cny"] is not None:
            cost = (cost or 0) + r["snap_total_cost_cny"]
            cost_n += 1
        if r["snap_expected_profit_cny"] is not None:
            profit = (profit or 0) + r["snap_expected_profit_cny"]
            profit_n += 1
    return {"cost_cents": cost, "cost_orders": cost_n, "profit_cents": profit, "profit_orders": profit_n,
            "paid_rows": len(rows)}


def overview(core, period="today", date_from="", date_to=""):
    start, end = resolve_range(core.now(), period, date_from, date_to)
    c = core.conn

    new_orders = c.execute("SELECT COUNT(*) FROM orders WHERE created_at>=? AND created_at<?", (start, end)).fetchone()[0]
    paid_by_paid_at = c.execute(
        "SELECT COUNT(*), COALESCE(SUM(amount_cents),0) FROM orders"
        " WHERE payment_status='paid' AND paid_at>=? AND paid_at<?", (start, end)).fetchone()
    paid_orders_revenue, revenue_cents = paid_by_paid_at[0], paid_by_paid_at[1]

    paid_among_created = c.execute(
        "SELECT COUNT(*) FROM orders WHERE created_at>=? AND created_at<? AND payment_status='paid'",
        (start, end)).fetchone()[0]
    completed = c.execute(
        "SELECT COUNT(*) FROM orders o JOIN fulfillments f ON f.order_id=o.id"
        " WHERE o.created_at>=? AND o.created_at<? AND f.status='delivered'", (start, end)).fetchone()[0]
    unpaid = c.execute(
        "SELECT COUNT(*) FROM orders WHERE created_at>=? AND created_at<? AND payment_status='unpaid'",
        (start, end)).fetchone()[0]
    processing = c.execute(
        "SELECT COUNT(*) FROM orders o JOIN fulfillments f ON f.order_id=o.id"
        " WHERE o.created_at>=? AND o.created_at<? AND o.payment_status='paid' AND f.status IN ('pending','processing')",
        (start, end)).fetchone()[0]
    attention = c.execute(
        "SELECT COUNT(*) FROM orders o JOIN fulfillments f ON f.order_id=o.id"
        " WHERE o.created_at>=? AND o.created_at<? AND o.payment_status='paid' AND f.status IN ('blocked','failed','manual')",
        (start, end)).fetchone()[0]
    closed = c.execute(
        "SELECT COUNT(*) FROM orders WHERE created_at>=? AND created_at<? AND payment_status='closed'",
        (start, end)).fetchone()[0]

    snaps = _sum_snapshots(c, start, end)
    aov = (revenue_cents // paid_orders_revenue) if paid_orders_revenue else None

    # New vs returning buyers by lookup_email (volume window = created_at).
    emails = [norm_email(r[0]) for r in c.execute(
        "SELECT DISTINCT lookup_email FROM orders WHERE created_at>=? AND created_at<? AND trim(lookup_email)!=''",
        (start, end)).fetchall()]
    new_customers = returning = 0
    for email in emails:
        first = c.execute(
            "SELECT MIN(created_at) FROM orders WHERE lower(trim(lookup_email))=?", (email,)).fetchone()[0]
        if first is not None and start <= first < end:
            new_customers += 1
        else:
            returning += 1

    ranking_plan = c.execute(
        "SELECT COALESCE(NULLIF(snap_plan_label,''), product_name) AS label, recharge_plan_id AS plan_id,"
        " COUNT(*) AS orders, SUM(amount_cents) AS revenue_cents,"
        " SUM(CASE WHEN snap_expected_profit_cny IS NULL THEN 0 ELSE snap_expected_profit_cny END) AS profit_cents,"
        " SUM(CASE WHEN snap_expected_profit_cny IS NULL THEN 0 ELSE 1 END) AS profit_orders"
        " FROM orders WHERE payment_status='paid' AND paid_at>=? AND paid_at<?"
        " GROUP BY label, plan_id ORDER BY revenue_cents DESC, orders DESC LIMIT 20",
        (start, end)).fetchall()
    ranking_region = c.execute(
        "SELECT COALESCE(NULLIF(snap_region_label,''), recharge_region) AS label, recharge_region AS region,"
        " COUNT(*) AS orders, SUM(amount_cents) AS revenue_cents,"
        " SUM(CASE WHEN snap_expected_profit_cny IS NULL THEN 0 ELSE snap_expected_profit_cny END) AS profit_cents,"
        " SUM(CASE WHEN snap_expected_profit_cny IS NULL THEN 0 ELSE 1 END) AS profit_orders"
        " FROM orders WHERE payment_status='paid' AND paid_at>=? AND paid_at<? AND product_type='api_recharge'"
        " GROUP BY label, region ORDER BY revenue_cents DESC, orders DESC LIMIT 20",
        (start, end)).fetchall()

    return {
        "period": period or "custom",
        "tz_label": TZ_LABEL,
        "start": start,
        "end": end,
        "start_text": format_ts(start),
        "end_text": format_ts(end - 1) if end else None,
        "new_orders": new_orders,
        "paid_orders": paid_orders_revenue,          # by paid_at (revenue window)
        "paid_among_created": paid_among_created,    # by created_at (conversion numerator)
        "completed_orders": completed,
        "unpaid_orders": unpaid,
        "processing_orders": processing,
        "attention_orders": attention,
        "closed_orders": closed,
        "conversion": {
            "rate": (paid_among_created / new_orders) if new_orders else None,
            "numerator": paid_among_created,
            "denominator": new_orders,
        },
        "revenue_cents": revenue_cents,
        "cost_cents": snaps["cost_cents"],           # None => 暂无数据
        "cost_orders_with_snapshot": snaps["cost_orders"],
        "profit_cents": snaps["profit_cents"],
        "profit_orders_with_snapshot": snaps["profit_orders"],
        "aov_cents": aov,
        "new_customers": new_customers,
        "returning_customers": returning,
        "rank_by_plan": [dict(r) for r in ranking_plan],
        "rank_by_region": [dict(r) for r in ranking_region],
    }


def index_names(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}

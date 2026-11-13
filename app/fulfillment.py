"""Fulfillment, recorded separately from payment (fulfillments table).

status: none -> pending -> processing -> delivered | failed | blocked | manual
  blocked : paid but cannot ship right now (out of stock, upstream key missing, upstream
            account/card-platform problem...). reason is shown in the admin; admin retries.
  failed  : upstream definitively refused/failed the recharge; admin may retry.
  manual  : outcome uncertain -> a human must check before anything is repeated.
"""
import json

from . import humanize, settings
from .db import tx
from .upstream import (TASK_FAILED, TASK_MANUAL, TASK_PROGRESS, TASK_SUCCESS, UpstreamTransportError,
                       parse_task, parse_task_list)

BACKOFF = [10, 30, 120, 300, 600]
MAX_AUTO_ATTEMPTS = 6
# upstream error codes that need an operator (top up card platform / points, bind card platform...)
OPERATOR_CODES = {
    "card_platform_disabled", "card_platform_not_bound", "card_platform_no_eligible_card",
    "card_platform_insufficient_balance", "wallet_card_pool_unavailable", "automate_points_insufficient",
    "automate_payment_source_invalid", "automate_payment_source_unsupported",
}
RETRY_LATER_CODES = {"automate_account_busy", "automate_account_recent_success", "automate_rate_limit_exceeded",
                     "automate_account_check_busy"}


def _set(c, core, order_id, **kw):
    kw["updated_at"] = core.now()
    cols = ", ".join("%s=?" % k for k in kw)
    c.execute("UPDATE fulfillments SET %s WHERE order_id=?" % cols, list(kw.values()) + [order_id])


# ---- card delivery --------------------------------------------------------
def deliver_cards(core, order_id):
    with tx(core.conn) as c:
        f = c.execute("SELECT * FROM fulfillments WHERE order_id=?", (order_id,)).fetchone()
        o = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if f["status"] == "delivered" or o["payment_status"] != "paid":
            return f["status"]
        need = o["quantity"]
        held = c.execute("SELECT COUNT(*) n FROM card_keys WHERE order_id=? AND status IN ('reserved','sold')",
                         (order_id,)).fetchone()["n"]
        if held < need:   # reservation was released (late payment) -> try to take cards now
            extra = need - held
            r = c.execute(
                "UPDATE card_keys SET status='reserved', order_id=?, reserved_at=? WHERE id IN ("
                " SELECT id FROM card_keys WHERE product_id=? AND status='available' ORDER BY id LIMIT ?)",
                (order_id, core.now(), o["product_id"], extra))
            if r.rowcount != extra:
                raise _Short(extra - r.rowcount)   # rolls the whole transaction back: nothing half-reserved
        c.execute("UPDATE card_keys SET status='sold', sold_at=? WHERE order_id=? AND status='reserved'",
                  (core.now(), order_id))
        sold = c.execute("SELECT COUNT(*) n FROM card_keys WHERE order_id=? AND status='sold'", (order_id,)).fetchone()["n"]
        if sold != need:
            raise RuntimeError("card count mismatch")
        _set(c, core, order_id, status="delivered", reason_code="", reason="", delivered_at=core.now())
        core.event(c, order_id, "delivered", "已发放 %d 张卡密" % need)
        return "delivered"


class _Short(Exception):
    def __init__(self, missing):
        self.missing = missing


def run_cards(core, order_id, force=False):
    c = core.conn
    f = c.execute("SELECT * FROM fulfillments WHERE order_id=?", (order_id,)).fetchone()
    if f["status"] in ("delivered", "none"):
        return f["status"]
    if f["status"] in ("failed", "manual") and not force:
        return f["status"]
    try:
        return deliver_cards(core, order_id)
    except _Short as s:
        # the whole transaction rolled back: nothing partially reserved
        with tx(c) as cc:
            _set(cc, core, order_id, status="blocked", reason_code="out_of_stock",
                 reason="已付款但库存不足（缺 %d 张）。补货后会自动发货，或人工退款" % s.missing, attempts=f["attempts"] + 1)
            cc.execute("UPDATE orders SET attention='已付款但库存不足，待补货或退款' WHERE id=?", (order_id,))
            core.event(cc, order_id, "blocked", "已付款但库存不足（缺 %d 张）" % s.missing)
        return "blocked"


# ---- API recharge (upstream, paymentSource=auto_card) ---------------------
def _claim(core, order_id, force):
    now = core.now()
    with tx(core.conn) as c:
        f = c.execute("SELECT * FROM fulfillments WHERE order_id=?", (order_id,)).fetchone()
        o = c.execute("SELECT payment_status FROM orders WHERE id=?", (order_id,)).fetchone()
        if o["payment_status"] != "paid" or f["status"] in ("delivered", "none"):
            return None
        if f["status"] == "processing" and f["locked_until"] > now:
            return None
        if f["status"] in ("failed", "blocked", "manual") and not force:
            return None
        if f["status"] == "pending" and f["next_attempt_at"] > now and not force:
            return None
        _set(c, core, order_id, status="processing", locked_until=now + 120, attempts=f["attempts"] + 1)
        return c.execute("SELECT * FROM fulfillments WHERE order_id=?", (order_id,)).fetchone()


def _hint(code, message="", http=None):
    return humanize.explain(code, http, message)


def _schedule_retry(core, order_id, f, why):
    n = f["attempts"]
    if n >= MAX_AUTO_ATTEMPTS:
        _mark(core, order_id, "manual", "retries_exhausted", "多次重试后仍无法确认上游结果（%s），请到上游核对订单后处理" % why)
        return
    delay = BACKOFF[min(n - 1, len(BACKOFF) - 1)]
    with tx(core.conn) as c:
        _set(c, core, order_id, status="pending", next_attempt_at=core.now() + delay, reason_code="retrying",
             reason="%s；%d 秒后自动重试" % (why, delay))


def _mark(core, order_id, status, code, reason, detail=""):
    with tx(core.conn) as c:
        _set(c, core, order_id, status=status, reason_code=code, reason=reason)
        if status in ("failed", "blocked", "manual"):
            c.execute("UPDATE orders SET attention=? WHERE id=?", (reason[:300], order_id))
        core.event(c, order_id, "fulfil_" + status, reason, ("code=%s %s" % (code, detail)).strip())


def _deliver_recharge(core, order_id, task_id):
    with tx(core.conn) as c:
        _set(c, core, order_id, status="delivered", reason_code="", reason="", delivered_at=core.now(),
             upstream_status="succeeded")
        c.execute("UPDATE orders SET recharge_input_enc='', attention='' WHERE id=?", (order_id,))
        core.event(c, order_id, "delivered", "上游充值成功（任务 %s）" % task_id)


def _adopt(core, order_id, f, t):
    """Apply a parsed upstream task to the fulfillment record."""
    if not t or not t["status"] or not (t["id"] or f["upstream_task_id"]):
        _mark(core, order_id, "manual", "upstream_unparseable",
              "上游返回的信息无法识别，请到上游核对这笔订单。", "response shape not recognized")
        return
    tid = t["id"] or f["upstream_task_id"]
    with tx(core.conn) as c:
        _set(c, core, order_id, upstream_task_id=tid, upstream_status=t["status"], upstream_error_code=t["error_code"])
    st = t["status"]
    if st in TASK_SUCCESS:
        _deliver_recharge(core, order_id, tid)
    elif st in TASK_FAILED:
        _mark(core, order_id, "failed", t["error_code"] or "upstream_" + st,
              "上游充值%s：%s" % ("已取消" if st != "failed" else "失败", _hint(t["error_code"], t["error_message"])),
              "upstream_status=%s task=%s" % (st, tid))
    elif st in TASK_MANUAL:
        _mark(core, order_id, "manual", "upstream_manual_review", "上游把这笔订单标记为「待人工核实」，请到上游核对后再处理。", "status=manual_review")
    elif st in TASK_PROGRESS:
        fresh = core.conn.execute("SELECT * FROM fulfillments WHERE order_id=?", (order_id,)).fetchone()
        if core.now() > fresh["deadline"]:
            _mark(core, order_id, "manual", "upstream_timeout", "上游任务长时间没有完成，请到上游核对。", "status=%s" % st)
        else:
            with tx(core.conn) as c:
                _set(c, core, order_id, status="pending", next_attempt_at=core.now() + 5, reason_code="upstream_" + st,
                     reason="上游处理中（%s）" % st)
    else:
        _mark(core, order_id, "manual", "upstream_unknown_status", "上游返回了无法识别的状态，请人工核对。", "status=%r" % st)


def _build_body(core, o, coid):
    body = {
        "clientOrderId": coid,
        "planId": o["recharge_plan_id"],
        "checkoutCountry": o["recharge_region"],
        "paymentSource": "auto_card",
        "authSessionJson": json.loads(core.crypto.decrypt(o["recharge_input_enc"])),
    }
    cp = settings.get_upstream(core)["card_platform_id"]
    if cp:
        body["cardPlatformId"] = cp
    return body


def _resolve_unknown(core, order_id, f, client, why):
    """Submit outcome unknown. Look the order up by clientOrderId (documented exact query)."""
    try:
        resp = client.find_by_client_order_id(f["upstream_client_order_id"])
    except UpstreamTransportError:
        return _schedule_retry(core, order_id, f, why + "，且查单失败")
    if not resp.ok:
        return _schedule_retry(core, order_id, f, why + "，且查单时上游服务出错")
    tasks = parse_task_list(resp.data)
    if tasks is None:
        return _mark(core, order_id, "manual", "lookup_unparseable",
                     "提交结果未知，而且无法确认上游有没有收到这笔订单，请到上游核对后再处理。",
                     "clientOrderId=%s" % f["upstream_client_order_id"])
    if len(tasks) == 1:
        return _adopt(core, order_id, f, tasks[0])
    if len(tasks) == 0:   # confirmed: upstream never created it -> resubmitting the same clientOrderId is safe
        return _schedule_retry(core, order_id, f, why + "，查单确认上游未创建任务")
    _mark(core, order_id, "manual", "lookup_ambiguous", "上游查到多条对应的任务，请人工核对。", "clientOrderId=%s" % f["upstream_client_order_id"])


def _payment_guard(core, order_id):
    """The last line of defence before anything is sent upstream. Returns None when the order is really paid, else a
    plain-language reason. It looks at the stored payment record (written only by payments.confirm_payment after the
    signature / amount / merchant-order-number checks), not just at a status flag."""
    row = core.conn.execute(
        "SELECT o.order_no, o.amount_cents, o.payment_status, p.out_trade_no, p.amount_cents AS paid_cents, p.order_id AS pay_order"
        " FROM orders o LEFT JOIN payments p ON p.order_id=o.id WHERE o.id=?", (order_id,)).fetchone()
    if row is None or row["payment_status"] != "paid":
        return "订单还没有付款"
    if row["out_trade_no"] is None:
        return "没有找到这笔订单的有效付款记录"
    if row["out_trade_no"] != row["order_no"] or row["pay_order"] != order_id:
        return "付款记录与订单号不一致"
    if row["paid_cents"] != row["amount_cents"]:
        return "实际付款金额与订单金额不一致"
    return None


def run_recharge(core, order_id, force=False):
    """Submits the recharge upstream. ONLY the worker may reach this (see run()); the payment guard runs twice."""
    f = _claim(core, order_id, force)
    if f is None:
        return
    bad = _payment_guard(core, order_id)
    if bad:
        return _mark(core, order_id, "blocked", "payment_not_confirmed", "没有确认到有效付款，已停止充值：%s。" % bad)
    o = core.conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    try:
        client = settings.upstream_client(core)
    except settings.Unavailable as e:
        return _mark(core, order_id, "blocked", "upstream_unconfigured", "%s，请到后台「上游充值」配置后重试" % e)
    try:
        if f["upstream_task_id"]:
            resp = client.get_task(f["upstream_task_id"])
            if resp.ok:
                return _adopt(core, order_id, f, parse_task(resp.data))
            if resp.http_status >= 500 or resp.http_status == 429:
                return _schedule_retry(core, order_id, f, "查询上游任务时服务暂时出错")
            return _mark(core, order_id, "manual", "poll_error",
                         _hint(resp.error_code, resp.error_message, resp.http_status) + " 请人工核对。",
                         "http=%s code=%s" % (resp.http_status, resp.error_code))
        coid = f["upstream_client_order_id"] or "%s-%d" % (o["order_no"], f["upstream_attempt"])
        if coid != f["upstream_client_order_id"]:
            with tx(core.conn) as c:
                _set(c, core, order_id, upstream_client_order_id=coid)
            f = dict(f, upstream_client_order_id=coid)
        if not o["recharge_input_enc"]:
            return _mark(core, order_id, "manual", "input_missing", "充值账号资料已被清除，无法提交，请人工处理")
        bad = _payment_guard(core, order_id)          # re-checked immediately before the only place that submits upstream
        if bad or f["upstream_task_id"]:
            return _mark(core, order_id, "blocked", "payment_not_confirmed", "没有确认到有效付款，已停止充值：%s。" % (bad or "这笔订单已经提交过"))
        try:
            resp = client.submit(_build_body(core, o, coid))
        except UpstreamTransportError:
            return _resolve_unknown(core, order_id, f, client, "提交上游超时/连接失败")
        if resp.ok:
            t = parse_task(resp.data)
            if not t or not t["id"]:
                return _resolve_unknown(core, order_id, f, client, "上游已响应但无法识别任务号")
            return _adopt(core, order_id, f, t)
        code = resp.error_code
        if resp.http_status >= 500 or resp.data is None:
            return _resolve_unknown(core, order_id, f, client, "上游服务暂时出错")
        if resp.http_status == 429 or code in RETRY_LATER_CODES:
            return _schedule_retry(core, order_id, f, _hint(code, resp.error_message) or "上游限流")
        if resp.http_status in (401, 403):
            return _mark(core, order_id, "blocked", code or "upstream_auth", _hint(code, resp.error_message, resp.http_status), "http=%s" % resp.http_status)
        if code in OPERATOR_CODES:
            return _mark(core, order_id, "blocked", code, _hint(code, resp.error_message, resp.http_status))
        return _mark(core, order_id, "failed", code or "http_%s" % resp.http_status,
                     _hint(code, resp.error_message, resp.http_status), "http=%s" % resp.http_status)
    except UpstreamTransportError:
        return _schedule_retry(core, order_id, f, "查询上游任务超时")


# ---- dispatch / admin -----------------------------------------------------
def run(core, order_id, force=False, worker=False):
    """Cards are delivered inline (no upstream involved). Recharges are submitted upstream ONLY by the worker:
    web requests, payment callbacks and admin buttons just queue the order (status pending) and return."""
    f = core.conn.execute("SELECT type FROM fulfillments WHERE order_id=?", (order_id,)).fetchone()
    if f["type"] == "card":
        return run_cards(core, order_id, force)
    if not worker:
        return None
    return run_recharge(core, order_id, force)


def admin_retry(core, order_id, actor="admin"):
    """Safe retry: card -> re-run delivery; recharge -> re-poll the known upstream task, or
    resubmit with the SAME clientOrderId (upstream returns the original task), or - only when
    the previous upstream task definitively failed/was cancelled - a NEW clientOrderId."""
    with tx(core.conn) as c:
        f = c.execute("SELECT * FROM fulfillments WHERE order_id=?", (order_id,)).fetchone()
        if f["status"] in ("none", "delivered", "processing"):
            return False
        if f["type"] == "api_recharge" and f["upstream_task_id"] and f["upstream_status"] in TASK_FAILED:
            _set(c, core, order_id, upstream_attempt=f["upstream_attempt"] + 1, upstream_client_order_id="",
                 upstream_task_id="", upstream_status="", upstream_error_code="")
        _set(c, core, order_id, status="pending", next_attempt_at=0, attempts=0)
        core.event(c, order_id, "admin_retry", "%s 触发重试履约" % actor)
    run(core, order_id, force=True)
    return True


def admin_mark(core, order_id, success, actor="admin"):
    """Manual resolution for recharge orders after the operator verified the upstream order."""
    with tx(core.conn) as c:
        f = c.execute("SELECT * FROM fulfillments WHERE order_id=?", (order_id,)).fetchone()
        if f["type"] != "api_recharge" or f["status"] in ("delivered", "none"):
            return False
        if success:
            _set(c, core, order_id, status="delivered", reason_code="admin_confirmed", reason="管理员人工确认已充值",
                 delivered_at=core.now())
            c.execute("UPDATE orders SET recharge_input_enc='', attention='' WHERE id=?", (order_id,))
        else:
            _set(c, core, order_id, status="failed", reason_code="admin_failed", reason="管理员人工确认充值失败（需退款/处理）")
        core.event(c, order_id, "admin_mark", "%s 人工标记为%s" % (actor, "已充值" if success else "充值失败"))
        return True


def sweep(core, limit=50):
    now = core.now()
    rows = core.conn.execute(
        "SELECT order_id FROM fulfillments WHERE (status='pending' AND next_attempt_at<=?)"
        " OR (status='processing' AND locked_until<?) OR (status='blocked' AND reason_code='out_of_stock')"
        " ORDER BY id LIMIT ?", (now, now, limit)).fetchall()
    for r in rows:
        try:
            run(core, r["order_id"], worker=True)
        except Exception as e:
            core.event(core.conn, r["order_id"], "fulfil_exception", "履约异常：%s" % type(e).__name__)

"""Payment confirmation.  confirm_payment() is the ONLY place an order becomes paid; the
async notification and the alipay.trade.query reconcile both funnel into it."""
import json

from . import alipay as ali
from . import fulfillment, settings
from .db import tx

SUCCESS = ("TRADE_SUCCESS", "TRADE_FINISHED")


def confirm_payment(core, out_trade_no, trade_no, total_amount, source, raw=""):
    """Returns 'confirmed' | 'duplicate' | 'rejected' | 'unknown_order'."""
    now = core.now()
    with tx(core.conn) as c:
        o = c.execute("SELECT * FROM orders WHERE order_no=?", (out_trade_no,)).fetchone()
        if o is None:
            core.event(c, None, "payment_unknown_order", "未知商户订单号 %s（来源 %s）" % (out_trade_no[:40], source))
            return "unknown_order"
        try:
            cents = ali.to_cents(total_amount)
        except ValueError:
            cents = None
        if cents != o["amount_cents"]:
            msg = "金额不符：订单 %s，支付宝 %s（来源 %s）" % (ali.format_cents(o["amount_cents"]), total_amount, source)
            core.event(c, o["id"], "payment_amount_mismatch", msg)
            c.execute("UPDATE orders SET attention=? WHERE id=?", ("收到金额不符的支付通知，已拒绝确认，请人工核对", o["id"]))
            return "rejected"
        if not trade_no:
            core.event(c, o["id"], "payment_rejected", "缺少支付宝交易号")
            return "rejected"
        if o["payment_status"] == "paid":
            return "duplicate"
        other = c.execute("SELECT 1 FROM payments WHERE trade_no=?", (trade_no,)).fetchone()
        if other:
            core.event(c, o["id"], "payment_rejected", "交易号已被其他订单使用")
            return "rejected"
        late = 1 if o["payment_status"] == "closed" else 0
        c.execute("INSERT INTO payments(order_id, out_trade_no, trade_no, amount_cents, source, late, raw, created_at)"
                  " VALUES (?,?,?,?,?,?,?,?)", (o["id"], out_trade_no, trade_no, cents, source, late, raw[:4000], now))
        c.execute("UPDATE orders SET payment_status='paid', paid_at=?, late_payment=?, attention=? WHERE id=?",
                  (now, late, "订单关闭后才收到付款（迟到付款），已自动尝试履约" if late else o["attention"], o["id"]))
        deadline = now + 6 * 3600
        c.execute("UPDATE fulfillments SET status='pending', next_attempt_at=0, deadline=?, updated_at=?"
                  " WHERE order_id=? AND status='none'", (deadline, now, o["id"]))
        core.event(c, o["id"], "payment_confirmed",
                   "支付确认（%s）：交易号 %s，金额 %s%s" % (source, trade_no, ali.format_cents(cents), "，迟到付款" if late else ""))
        return "confirmed"


def _fulfil(core, order_no):
    row = core.conn.execute("SELECT id FROM orders WHERE order_no=?", (order_no,)).fetchone()
    if row:
        try:
            fulfillment.run(core, row["id"])
        except Exception as e:  # payment is already stored; worker will retry
            core.event(core.conn, row["id"], "fulfil_exception", "履约异常：%s" % type(e).__name__)


def handle_notify(core, form):
    """Returns (body, http_status). Body must be exactly 'success' for Alipay to stop retrying."""
    try:
        client = settings.alipay_client(core, require_enabled=False)
    except settings.Unavailable:
        return "failure", 503
    cfg = settings.get_alipay(core)
    if not client.verify_notify(form):
        core.event(core.conn, None, "notify_bad_signature", "异步通知验签失败（out_trade_no=%s）" % form.get("out_trade_no", "")[:40])
        return "failure", 400
    if form.get("app_id") != cfg["app_id"] or form.get("seller_id") != cfg["merchant_pid"]:
        core.event(core.conn, None, "notify_wrong_merchant", "通知 app_id/seller_id 与配置不符（out_trade_no=%s）" % form.get("out_trade_no", "")[:40])
        return "failure", 400
    out_trade_no = form.get("out_trade_no", "")
    if form.get("trade_status") not in SUCCESS:
        return "success", 200
    raw = json.dumps({k: v for k, v in form.items() if k != "sign"}, ensure_ascii=False)
    res = confirm_payment(core, out_trade_no, form.get("trade_no", ""), form.get("total_amount", ""), "notify", raw)
    if res in ("confirmed", "duplicate"):
        _fulfil(core, out_trade_no)
    return "success", 200


def check_payment(core, order_no):
    """alipay.trade.query reconcile. Returns paid|waiting|closed|not_exist|unknown."""
    o = core.conn.execute("SELECT * FROM orders WHERE order_no=?", (order_no,)).fetchone()
    if o is None:
        return "unknown"
    if o["payment_status"] == "paid":
        return "paid"
    if not o["precreate_attempted"]:
        return "not_exist"
    try:
        client = settings.alipay_client(core, require_enabled=False)
    except settings.Unavailable:
        return "unknown"
    core.conn.execute("UPDATE orders SET last_query_at=? WHERE id=?", (core.now(), o["id"]))
    try:
        q = client.query(order_no)
    except ali.AlipayError as e:
        if e.sub_code == "ACQ.TRADE_NOT_EXIST" and e.signed:
            return "not_exist"
        return "unknown"
    except (ali.AlipayTransportError, ali.AlipaySignatureError):
        return "unknown"
    if q["trade_status"] in SUCCESS:
        res = confirm_payment(core, order_no, q["trade_no"], q["total_amount"], "query", json.dumps(q))
        if res in ("confirmed", "duplicate"):
            _fulfil(core, order_no)
            return "paid"
        return "unknown"
    if q["trade_status"] == "TRADE_CLOSED":
        return "closed"
    return "waiting"


def close_local(core, order_no, alipay_confirmed):
    with tx(core.conn) as c:
        o = c.execute("SELECT * FROM orders WHERE order_no=?", (order_no,)).fetchone()
        r = c.execute("UPDATE orders SET payment_status='closed', closed_at=?, recharge_input_enc=CASE WHEN ?=1 THEN '' ELSE recharge_input_enc END"
                      " WHERE id=? AND payment_status='unpaid'", (core.now(), 1 if alipay_confirmed else 0, o["id"]))
        if r.rowcount != 1:
            return False
        c.execute("UPDATE card_keys SET status='available', order_id=NULL, reserved_at=NULL"
                  " WHERE order_id=? AND status='reserved'", (o["id"],))
        core.event(c, o["id"], "order_closed", "订单超时关闭，库存已释放%s" % ("" if alipay_confirmed else "（未经支付宝确认）"))
        return True


def expire_order(core, order_no):
    o = core.conn.execute("SELECT * FROM orders WHERE order_no=?", (order_no,)).fetchone()
    if o is None or o["payment_status"] != "unpaid":
        return
    if not o["precreate_attempted"]:
        close_local(core, order_no, alipay_confirmed=False)
        return
    r = check_payment(core, order_no)
    if r == "paid":
        return
    if r in ("not_exist", "closed"):
        close_local(core, order_no, alipay_confirmed=True)
        return
    if r == "waiting":
        try:
            client = settings.alipay_client(core, require_enabled=False)
            client.close(order_no)
        except ali.AlipayError:
            r2 = check_payment(core, order_no)          # close refused: maybe it was just paid
            if r2 in ("not_exist", "closed"):
                close_local(core, order_no, alipay_confirmed=True)
            return
        except (ali.AlipayTransportError, ali.AlipaySignatureError, settings.Unavailable):
            return
        close_local(core, order_no, alipay_confirmed=True)
    # r == 'unknown': leave reserved; retried by the next sweep


def sweep(core, limit=50):
    now = core.now()
    for r in core.conn.execute(
            "SELECT order_no FROM orders WHERE payment_status='unpaid' AND expires_at<=? ORDER BY id LIMIT ?", (now, limit)).fetchall():
        expire_order(core, r["order_no"])
    for r in core.conn.execute(
            "SELECT order_no FROM orders WHERE payment_status='unpaid' AND precreate_attempted=1 AND expires_at>?"
            " AND created_at<=? AND last_query_at<=? ORDER BY id LIMIT ?", (now, now - 20, now - 30, limit)).fetchall():
        check_payment(core, r["order_no"])

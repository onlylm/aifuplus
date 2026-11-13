"""Alipay Open API client (RSA2, public-key mode) for face-to-face payment (当面付).

Only the three methods we need: alipay.trade.precreate / query / close, plus async
notification signature verification.  Certificate mode is NOT supported."""
import base64
import json
import re
import time as _time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

GATEWAYS = {
    "production": "https://openapi.alipay.com/gateway.do",
    "sandbox": "https://openapi-sandbox.dl.alipaydev.com/gateway.do",
}
CST = timezone(timedelta(hours=8))


class AlipayError(Exception):
    """Business-level error returned by Alipay (code != 10000)."""

    def __init__(self, code, sub_code="", msg="", signed=False):
        super().__init__("%s %s %s" % (code, sub_code, msg))
        self.code, self.sub_code, self.msg, self.signed = code, sub_code, msg, signed


class AlipayTransportError(Exception):
    """Network / HTTP failure: outcome unknown."""


class AlipaySignatureError(Exception):
    """Response signature missing/invalid."""


def _pem_wrap(body, label):
    body = re.sub(r"\s+", "", body)
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return "-----BEGIN %s-----\n%s\n-----END %s-----\n" % (label, "\n".join(lines), label)


def load_private_key(text):
    text = (text or "").strip()
    if not text:
        raise ValueError("应用私钥为空")
    candidates = [text] if "BEGIN" in text else [_pem_wrap(text, "PRIVATE KEY"), _pem_wrap(text, "RSA PRIVATE KEY")]
    for c in candidates:
        try:
            key = serialization.load_pem_private_key(c.encode(), password=None)
            if key.key_size < 2048:
                raise ValueError("RSA 私钥长度必须 >= 2048 位")
            return key
        except ValueError as e:
            if "2048" in str(e):
                raise
        except Exception:
            pass
    raise ValueError("无法解析应用私钥（需要 RSA PKCS#8 / PKCS#1，PEM 或纯 Base64）")


def load_public_key(text):
    text = (text or "").strip()
    if not text:
        raise ValueError("支付宝公钥为空")
    c = text if "BEGIN" in text else _pem_wrap(text, "PUBLIC KEY")
    try:
        return serialization.load_pem_public_key(c.encode())
    except Exception:
        raise ValueError("无法解析支付宝公钥（需要 X.509 公钥，PEM 或纯 Base64）")


def public_key_pem(private_key):
    return private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def private_key_fingerprint(private_key):
    import hashlib
    der = private_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()[:16]


def sign_string(params, exclude=("sign",)):
    return "&".join("%s=%s" % (k, params[k]) for k in sorted(params)
                    if k not in exclude and params[k] not in (None, ""))


def rsa2_sign(private_key, content):
    return base64.b64encode(private_key.sign(content.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())).decode()


def rsa2_verify(public_key, content, signature):
    try:
        public_key.verify(base64.b64decode(signature), content.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:
        return False


def extract_node(text, key):
    """Raw substring of the JSON object stored under `key` (needed to verify the response sign)."""
    m = re.search(r'"%s"\s*:\s*' % re.escape(key), text)
    if not m:
        return None
    i = m.end()
    if i >= len(text) or text[i] != "{":
        return None
    depth, in_str, esc = 0, False, False
    for j in range(i, len(text)):
        ch = text[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
    return None


def to_cents(value):
    """'12.30' -> 1230 ; rejects more than 2 decimals / garbage."""
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError):
        raise ValueError("bad amount: %r" % (value,))
    cents = d * 100
    if cents != cents.to_integral_value() or d < 0:
        raise ValueError("bad amount: %r" % (value,))
    return int(cents)


def format_cents(cents):
    return "%d.%02d" % (cents // 100, cents % 100)


def cst_time(ts):
    return datetime.fromtimestamp(ts, CST).strftime("%Y-%m-%d %H:%M:%S")


def default_transport(url, form):
    import requests
    try:
        r = requests.post(url, data=form, timeout=(5, 20),
                          headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"})
    except requests.RequestException as e:
        raise AlipayTransportError(str(e))
    if r.status_code != 200:
        raise AlipayTransportError("HTTP %s" % r.status_code)
    return r.content.decode("utf-8")


class AlipayClient:
    def __init__(self, app_id, private_key_pem, alipay_public_key, mode="sandbox", notify_url="",
                 transport=None, clock=None):
        self.app_id = app_id
        self.private_key = load_private_key(private_key_pem)
        self.public_key = load_public_key(alipay_public_key)
        self.gateway = GATEWAYS[mode]
        self.notify_url = notify_url
        self.transport = transport or default_transport
        self.clock = clock or _time.time

    # ---- signing -----------------------------------------------------------
    def verify_notify(self, params):
        sig = params.get("sign")
        if not sig:
            return False
        return rsa2_verify(self.public_key, sign_string(params, exclude=("sign", "sign_type")), sig)

    def _call(self, method, biz, with_notify=False):
        form = {
            "app_id": self.app_id,
            "method": method,
            "format": "JSON",
            "charset": "utf-8",
            "sign_type": "RSA2",
            "timestamp": cst_time(self.clock()),
            "version": "1.0",
            "biz_content": json.dumps(biz, ensure_ascii=False, separators=(",", ":")),
        }
        if with_notify and self.notify_url:
            form["notify_url"] = self.notify_url
        form["sign"] = rsa2_sign(self.private_key, sign_string(form))
        text = self.transport(self.gateway, form)
        key = method.replace(".", "_") + "_response"
        node = extract_node(text, key)
        err = False
        if node is None:
            node = extract_node(text, "error_response")
            err = True
        if node is None:
            raise AlipayTransportError("unparseable response")
        try:
            data = json.loads(node)
            envelope = json.loads(text)
        except ValueError:
            raise AlipayTransportError("unparseable response")
        sig = envelope.get("sign")
        signed = bool(sig) and rsa2_verify(self.public_key, node, sig)
        if sig and not signed:
            raise AlipaySignatureError("response signature invalid")
        if err or data.get("code") != "10000":
            raise AlipayError(data.get("code", ""), data.get("sub_code", ""),
                              data.get("sub_msg") or data.get("msg", ""), signed=signed)
        if not signed:
            raise AlipaySignatureError("successful response is not signed")
        return data

    # ---- API ---------------------------------------------------------------
    def precreate(self, out_trade_no, amount_cents, subject, expire_ts):
        data = self._call("alipay.trade.precreate", {
            "out_trade_no": out_trade_no,
            "total_amount": format_cents(amount_cents),
            "subject": subject[:120],
            "time_expire": cst_time(expire_ts),
        }, with_notify=True)
        if data.get("out_trade_no") != out_trade_no or not data.get("qr_code"):
            raise AlipaySignatureError("precreate response mismatch")
        return data["qr_code"]

    def query(self, out_trade_no):
        data = self._call("alipay.trade.query", {"out_trade_no": out_trade_no})
        if data.get("out_trade_no") != out_trade_no:
            raise AlipaySignatureError("query response out_trade_no mismatch")
        return {
            "trade_status": data.get("trade_status", ""),
            "trade_no": data.get("trade_no", ""),
            "total_amount": data.get("total_amount", ""),
            "out_trade_no": out_trade_no,
        }

    def close(self, out_trade_no):
        return self._call("alipay.trade.close", {"out_trade_no": out_trade_no})

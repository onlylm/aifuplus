"""Normalize buyer ChatGPT Session JSON before order creation.

Fills a missing top-level account_id from known nested shapes, JWT account claims
(candidate discovery only), or a bounded upstream /account-check poll.
Never logs the raw Session. Account resolution is pre-payment validation only —
it does not create upstream recharge tasks.
"""
import base64
import json
import time

from . import settings
from .upstream import UpstreamTransportError, parse_account_check_job

MAX_AUTH_JSON = 20000
CHECK_TIMEOUT_SEC = 8.0
CHECK_POLL_SEC = 0.35

# Plain-language buyer messages (no account_id / JWT jargon).
MSG_MISSING = "请粘贴充值凭证"
MSG_TOO_LONG = "充值凭证过长，请确认复制的是正确内容"
MSG_BAD_JSON = "充值凭证格式不对，请按说明重新复制完整内容"
MSG_NO_TOKEN = "充值凭证里缺少账号授权信息，请重新提交有效的充值凭证"
MSG_NO_EMAIL = "没有识别到充值账号，请重新提交有效的充值凭证"
MSG_CHECKING = "正在识别充值账号"
MSG_OK = "已识别充值账号"
MSG_FAIL = "无法确认要充值的账号，请重新获取充值凭证"
MSG_AMBIGUOUS = "这个登录状态包含多个账号，暂时无法确定充值目标。"


class SessionError(Exception):
    """Buyer-fixable Session problem. status is valid|invalid|ambiguous|checking."""

    def __init__(self, message, status="invalid", field="session"):
        super().__init__(message)
        self.status = status
        self.field = field


def _as_id(value):
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        s = str(int(value)) if float(value).is_integer() else ""
        return s if s else ""
    return ""


def _top_level_id(data):
    return _as_id(data.get("account_id")) or _as_id(data.get("accountId"))


def _nested_ids(data):
    """Project-supported nested shapes that commonly carry the ChatGPT account id."""
    found = []
    account = data.get("account") if isinstance(data.get("account"), dict) else {}
    for key in ("account_id", "accountId", "id"):
        got = _as_id(account.get(key))
        if got:
            found.append(got)
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    for key in ("account_id", "accountId"):
        got = _as_id(user.get(key))
        if got:
            found.append(got)
    accounts = data.get("accounts")
    if isinstance(accounts, list):
        ids = []
        for item in accounts:
            if not isinstance(item, dict):
                continue
            got = _as_id(item.get("accountId") or item.get("account_id") or item.get("id"))
            if got:
                ids.append(got)
        if len(ids) == 1:
            found.append(ids[0])
        elif len(set(ids)) > 1:
            return None  # explicit multi-account ambiguity at Session level
    # de-dupe preserving order
    out, seen = [], set()
    for i in found:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _b64url_json(segment):
    try:
        pad = "=" * (-len(segment) % 4)
        return json.loads(base64.urlsafe_b64decode(segment + pad).decode("utf-8"))
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _jwt_candidate_ids(access_token):
    """Read ChatGPT account claims from an unverified JWT payload (discovery only)."""
    if not isinstance(access_token, str) or access_token.count(".") != 2:
        return []
    payload = _b64url_json(access_token.split(".")[1])
    if not isinstance(payload, dict):
        return []
    found = []
    auth = payload.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        got = _as_id(auth.get("chatgpt_account_id") or auth.get("account_id"))
        if got:
            found.append(got)
    flat = payload.get("https://api.openai.com/auth.chatgpt_account_id")
    got = _as_id(flat)
    if got:
        found.append(got)
    nested = payload.get("auth") if isinstance(payload.get("auth"), dict) else {}
    got = _as_id(nested.get("chatgpt_account_id") or nested.get("account_id"))
    if got:
        found.append(got)
    out, seen = [], set()
    for i in found:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _unique_or_ambiguous(ids):
    ids = [i for i in ids if i]
    if not ids:
        return None, False
    uniq = list(dict.fromkeys(ids))
    if len(uniq) == 1:
        return uniq[0], False
    return None, True


def _extract_selected_account(result):
    if not isinstance(result, dict):
        return None, True
    selected = _as_id(result.get("selectedAccountId") or result.get("selected_account_id"))
    accounts = result.get("accounts") if isinstance(result.get("accounts"), list) else []
    account_ids = []
    for item in accounts:
        if isinstance(item, dict):
            got = _as_id(item.get("accountId") or item.get("account_id") or item.get("id"))
            if got:
                account_ids.append(got)
    if selected:
        return selected, False
    if len(account_ids) == 1:
        return account_ids[0], False
    count = result.get("accountCount")
    if count == 1 and account_ids:
        return account_ids[0], False
    if (isinstance(count, int) and count > 1) or len(set(account_ids)) > 1:
        return None, True
    return None, False


def _poll_account_check(core, session_obj):
    """POST /account-check then poll job until success/fail/timeout. Returns account id or raises SessionError."""
    try:
        client = settings.upstream_client(core)
    except settings.Unavailable:
        raise SessionError(MSG_FAIL, "invalid")
    try:
        resp = client.account_check({"authSessionJson": session_obj})
    except UpstreamTransportError:
        raise SessionError(MSG_FAIL, "invalid")
    if not resp.ok:
        raise SessionError(MSG_FAIL, "invalid")
    job = parse_account_check_job(resp.data)
    if job is None:
        raise SessionError(MSG_FAIL, "invalid")
    deadline = time.monotonic() + CHECK_TIMEOUT_SEC
    while True:
        status = (job.get("status") or "").lower()
        if status in ("succeeded", "success"):
            account_id, ambiguous = _extract_selected_account(job.get("result") or {})
            if ambiguous:
                raise SessionError(MSG_AMBIGUOUS, "ambiguous")
            if not account_id:
                raise SessionError(MSG_FAIL, "invalid")
            return account_id
        if status in ("failed", "cancelled", "canceled", "error"):
            raise SessionError(MSG_FAIL, "invalid")
        if time.monotonic() >= deadline:
            raise SessionError(MSG_FAIL, "invalid")
        time.sleep(CHECK_POLL_SEC)
        jid = job.get("id")
        if not jid:
            raise SessionError(MSG_FAIL, "invalid")
        try:
            resp = client.get_account_check_job(jid)
        except UpstreamTransportError:
            raise SessionError(MSG_FAIL, "invalid")
        if not resp.ok:
            raise SessionError(MSG_FAIL, "invalid")
        job = parse_account_check_job(resp.data)
        if job is None:
            raise SessionError(MSG_FAIL, "invalid")


def normalize_session(auth_text, core=None, use_upstream=True):
    """Return (normalized_session_dict, email, status).

    status is always 'valid' on success. Raises SessionError otherwise.
    On success the returned dict always has a non-empty top-level account_id.
    """
    auth_text = (auth_text or "").strip()
    if not auth_text:
        raise SessionError(MSG_MISSING, "invalid")
    if len(auth_text) > MAX_AUTH_JSON:
        raise SessionError(MSG_TOO_LONG, "invalid")
    try:
        data = json.loads(auth_text)
    except ValueError:
        raise SessionError(MSG_BAD_JSON, "invalid")
    if not isinstance(data, dict):
        raise SessionError(MSG_BAD_JSON, "invalid")

    token = data.get("accessToken")
    if not (isinstance(token, str) and token.strip()):
        raise SessionError(MSG_NO_TOKEN, "invalid")

    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    email = user.get("email")
    if not (isinstance(email, str) and "@" in email and email.strip()):
        raise SessionError(MSG_NO_EMAIL, "invalid")
    email = email.strip()

    account_id = _top_level_id(data)
    if not account_id:
        nested = _nested_ids(data)
        if nested is None:
            raise SessionError(MSG_AMBIGUOUS, "ambiguous")
        account_id, ambiguous = _unique_or_ambiguous(nested)
        if ambiguous:
            raise SessionError(MSG_AMBIGUOUS, "ambiguous")

    if not account_id:
        jwt_ids = _jwt_candidate_ids(token)
        account_id, ambiguous = _unique_or_ambiguous(jwt_ids)
        if ambiguous:
            raise SessionError(MSG_AMBIGUOUS, "ambiguous")

    if not account_id:
        if not use_upstream or core is None:
            raise SessionError(MSG_FAIL, "invalid")
        account_id = _poll_account_check(core, data)

    if not account_id:
        raise SessionError(MSG_FAIL, "invalid")

    normalized = dict(data)
    normalized["account_id"] = account_id
    # Keep accountId in sync when callers/upstream prefer camelCase.
    if "accountId" in normalized or "account_id" in data:
        normalized["accountId"] = account_id
    return normalized, email, "valid"

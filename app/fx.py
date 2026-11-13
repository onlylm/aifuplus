"""USD/CNY exchange-rate providers (HTTPS JSON APIs, no key, no page scraping). Swap/add providers here only."""
import requests

from .pricing import parse_rate_micro

MIN_RATE, MAX_RATE = 3_000_000, 15_000_000            # sanity window for USD/CNY; anything outside is rejected


class FxError(Exception):
    pass


def _get_json(url, timeout=(4, 8)):
    try:
        r = requests.get(url, timeout=timeout, headers={"Accept": "application/json"})
    except requests.RequestException as e:
        raise FxError("network: %s" % type(e).__name__)
    if r.status_code != 200:
        raise FxError("http %s" % r.status_code)
    try:
        return r.json()
    except ValueError:
        raise FxError("invalid json")


def frankfurter():
    d = _get_json("https://api.frankfurter.dev/v1/latest?base=USD&symbols=CNY")
    return parse_rate_micro((d.get("rates") or {}).get("CNY"))


def open_er_api():
    d = _get_json("https://open.er-api.com/v6/latest/USD")
    if d.get("result") != "success":
        raise FxError("provider reported failure")
    return parse_rate_micro((d.get("rates") or {}).get("CNY"))


PROVIDERS = [("frankfurter.dev", frankfurter), ("open.er-api.com", open_er_api)]


def fetch_rate(providers=None):
    """Try providers in order. Returns (rate_micro, source). Never returns 0/None: raises FxError instead."""
    errors = []
    for name, fn in (providers or PROVIDERS):
        try:
            micro = fn()
        except FxError as e:
            errors.append("%s: %s" % (name, e))
            continue
        if micro and MIN_RATE <= micro <= MAX_RATE:
            return micro, name
        errors.append("%s: rate out of range" % name)
    raise FxError("; ".join(errors) or "no provider")

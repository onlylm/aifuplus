"""Upstream connection test. (Plan / cost synchronisation lives in pricing_service.)"""
from . import humanize, pricing_service, settings
from .upstream import UpstreamTransportError


NAME_KEYS = ("name", "label", "title", "displayName", "display_name")


def _plan_items(data):
    plans = data.get("plans")
    out = []
    if isinstance(plans, list):
        for p in plans:
            if isinstance(p, str):
                out.append((p, {}))
            elif isinstance(p, dict):
                pid = p.get("id") or p.get("planId") or p.get("plan_id")
                if isinstance(pid, str) and pid:
                    out.append((pid, p))
    elif isinstance(plans, dict):
        for pid, p in plans.items():
            out.append((str(pid), p if isinstance(p, dict) else {}))
    return out


def parse_config(data):
    """Upstream /config -> (plans, auto_card_open). Only the four GPT direct-recharge plans are kept; regions may be
    plain codes or objects. Raises ValueError when no plan list can be found."""
    if not isinstance(data, dict) or "plans" not in data:
        raise ValueError("no plans")
    sources = data.get("paymentSources")
    auto_open = any((s.get("id") if isinstance(s, dict) else s) == "auto_card" for s in (sources or []))
    region_map = data.get("planRegions") if isinstance(data.get("planRegions"), dict) else {}
    plans = []
    for pid, obj in _plan_items(data):
        if pid not in pricing_service.ALLOWED:
            continue
        raw = region_map.get(pid)
        if raw is None:
            raw = obj.get("regions") or []
        regions = [c for c in (pricing_service.region_code(r) for r in raw) if c]
        name = next((obj[k].strip() for k in NAME_KEYS if isinstance(obj.get(k), str) and obj[k].strip()), pricing_service.PLAN_LABEL[pid])
        disabled = obj.get("enabled") is False or obj.get("disabled") is True or str(obj.get("status", "")).lower() in ("disabled", "closed", "off")
        plans.append({"plan_id": pid, "name": name, "regions": regions, "usable": bool(auto_open and not disabled and regions),
                      "note": "" if auto_open else "上游暂未开放「自动选卡」"})
    return plans, auto_open


def _fail_text(resp=None, exc=None):
    if exc is not None:
        return "连不上上游服务，请检查网络或上游地址。"
    return humanize.explain(resp.error_code, resp.http_status)


def _record_test(core, ok, msg, detail=""):
    core.conn.execute("UPDATE upstream_config SET last_test_at=?, last_test_ok=?, last_test_msg=?, last_test_detail=? WHERE id=1",
                      (core.now(), 1 if ok else 0, msg, detail[:2000]))


def test_connection(core):
    """Returns (ok, plain_message)."""
    try:
        client = settings.upstream_client(core, require_enabled=False)
    except settings.Unavailable as e:
        _record_test(core, False, str(e))
        return False, str(e)
    try:
        resp = client.config()
    except UpstreamTransportError as e:
        _record_test(core, False, _fail_text(exc=e), "network error: %s" % e)
        return False, _fail_text(exc=e)
    if not resp.ok:
        msg = _fail_text(resp)
        _record_test(core, False, msg, "http=%s code=%s" % (resp.http_status, resp.error_code))
        return False, msg
    _record_test(core, True, "已连接上游，密钥可用。")
    return True, "已连接上游，密钥可用。"

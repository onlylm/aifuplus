"""All pricing arithmetic lives here (integers only; no floats touch money).

Units:  USD cents, CNY fen, FX rate x 1,000,000 ("micro"), percentages in basis points (1% = 100).
Half-up rounding is used wherever an integer division loses a remainder."""
from decimal import Decimal, InvalidOperation

ROUNDINGS = ("cent", "yuan_up", "tail_90", "tail_99")
ROUNDING_TEXT = {"cent": "保留两位小数", "yuan_up": "向上取整到整数元", "tail_90": "尾数调整为 .90", "tail_99": "尾数调整为 .99"}
MODES = ("percentage", "fixed", "manual_retail")
MODE_TEXT = {"percentage": "按成本百分比加价", "fixed": "固定利润", "manual_retail": "直接设置零售价"}


def _div_half_up(n, d):
    return (n * 2 + d) // (d * 2) if n >= 0 else -((-n * 2 + d) // (d * 2))


def parse_usd_cents(value):
    """Upstream wallet price (JSON number/str) -> USD cents. None for null / non-numeric / negative / bool."""
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError):
        return None
    if not d.is_finite() or d < 0:
        return None
    return int((d * 100).to_integral_value(rounding="ROUND_HALF_UP"))


def parse_rate_micro(value):
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError):
        return None
    if not d.is_finite() or d <= 0:
        return None
    return int((d * 1_000_000).to_integral_value(rounding="ROUND_HALF_UP"))


def parse_money_fen(text):
    """'12.34' -> 1234 (CNY fen). Raises ValueError."""
    try:
        d = Decimal(str(text).strip())
    except (InvalidOperation, AttributeError):
        raise ValueError("金额格式不对")
    c = d * 100
    if not d.is_finite() or d < 0 or c != c.to_integral_value():
        raise ValueError("金额格式不对（最多两位小数，不能为负）")
    return int(c)


def parse_percent_bp(text):
    """'20' or '12.5' (percent) -> basis points."""
    try:
        d = Decimal(str(text).strip())
    except (InvalidOperation, AttributeError):
        raise ValueError("百分比格式不对")
    bp = d * 100
    if not d.is_finite() or d < 0 or bp != bp.to_integral_value():
        raise ValueError("百分比格式不对（最多两位小数，不能为负）")
    return int(bp)


def usd_cents_to_cny_fen(usd_cents, fx_micro):
    """1 USD cent x rate = rate fen."""
    return _div_half_up(usd_cents * fx_micro, 1_000_000)


def with_safety(fx_micro, safety_bp):
    """Safety factor: effective rate = rate x (1 + safety%)."""
    return _div_half_up(fx_micro * (10_000 + safety_bp), 10_000)


def effective_package_cost(cost_source, upstream_usd_cents, manual_cny, fx_micro):
    """Returns (fen or None, used_source). 'manual' always wins when chosen; 'upstream' falls back to the manual value
    only when upstream never provided a valid number."""
    if cost_source == "manual":
        return (manual_cny, "manual") if manual_cny is not None else (None, "")
    if upstream_usd_cents is not None and fx_micro:
        return usd_cents_to_cny_fen(upstream_usd_cents, fx_micro), "upstream"
    if upstream_usd_cents is None and manual_cny is not None:
        return manual_cny, "manual"
    return None, ""


def apply_rounding(price_fen, rounding):
    if rounding == "yuan_up":
        return -(-price_fen // 100) * 100
    if rounding in ("tail_90", "tail_99"):
        tail = 90 if rounding == "tail_90" else 99
        base = price_fen - price_fen % 100
        return base + tail if price_fen <= base + tail else base + 100 + tail      # never rounds down
    return price_fen


def compute_price(total_cost_fen, mode, markup_bp, fixed_profit_fen, rounding, manual_sale_fen=None):
    """Markup is on COST (cost x (1 + p)), not a margin on price.

    manual_retail: sale price is exactly the admin-entered fen (no rounding/markup).
    """
    if mode == "manual_retail":
        if manual_sale_fen is None or manual_sale_fen <= 0:
            raise ValueError("请填写大于 0 的零售价")
        return int(manual_sale_fen)
    if mode == "percentage":
        raw = _div_half_up(total_cost_fen * (10_000 + markup_bp), 10_000)
    else:
        raw = total_cost_fen + fixed_profit_fen
    return max(1, apply_rounding(raw, rounding))


def change_bp(old, new):
    """Relative change in basis points (always >= 0). old must be > 0."""
    if not old:
        return 0
    return _div_half_up(abs(new - old) * 10_000, old)


def fmt_cny(fen):
    return "%d.%02d" % (fen // 100, fen % 100) if fen is not None else "-"


def fmt_usd(cents):
    return "%d.%02d" % (cents // 100, cents % 100) if cents is not None else "-"


def fmt_rate(micro):
    return "%d.%04d" % (micro // 1_000_000, (micro % 1_000_000 + 50) // 100) if micro else "-"

"""Products and card-key inventory."""
import re

from .crypto import mask_code
from .db import tx

TYPES = ("card", "api_recharge")
# Documented direct-recharge plans usable with paymentSource=auto_card are only known by
# /config.plans at runtime; the admin enters the planId (see README "限制").


class CatalogError(Exception):
    pass


def parse_price(text):
    m = re.fullmatch(r"\s*(\d{1,7})(?:\.(\d{1,2}))?\s*", str(text))
    if not m:
        raise CatalogError("价格格式无效（最多两位小数）")
    cents = int(m.group(1)) * 100 + int((m.group(2) or "0").ljust(2, "0"))
    if cents <= 0:
        raise CatalogError("价格必须大于 0")
    return cents


def save_product(core, product_id, name, description, type_, price, enabled, max_per_order, plan_id, regions):
    name = name.strip()
    if not name:
        raise CatalogError("商品名称不能为空")
    if type_ not in TYPES:
        raise CatalogError("请选择商品类型")
    cents = parse_price(price)
    try:
        max_per_order = int(max_per_order)
    except ValueError:
        raise CatalogError("单笔最大数量必须是整数")
    if not 1 <= max_per_order <= 100:
        raise CatalogError("单笔最大数量需在 1~100")
    plan_id = plan_id.strip()
    regions = ",".join(r.strip().upper() for r in regions.split(",") if r.strip())
    if type_ == "api_recharge":
        max_per_order = 1
        if not plan_id:
            raise CatalogError("请先选择一个上游套餐")
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", plan_id):
            raise CatalogError("套餐信息无效，请重新同步套餐后再试")
        if not regions:
            raise CatalogError("请至少选择一个可销售地区")
        if not all(re.fullmatch(r"[A-Z]{2}", r) for r in regions.split(",")):
            raise CatalogError("地区信息无效，请重新选择")
    else:
        plan_id, regions = "", ""
    now = core.now()
    with tx(core.conn) as c:
        if product_id:
            row = c.execute("SELECT type FROM products WHERE id=?", (product_id,)).fetchone()
            if not row:
                raise CatalogError("商品不存在")
            if row["type"] != type_:
                raise CatalogError("不能修改已创建商品的类型")
            c.execute("UPDATE products SET name=?, description=?, price_cents=?, enabled=?, max_per_order=?,"
                      " plan_id=?, regions=?, updated_at=? WHERE id=?",
                      (name, description.strip(), cents, 1 if enabled else 0, max_per_order, plan_id, regions, now, product_id))
            return product_id
        cur = c.execute("INSERT INTO products(name, description, type, price_cents, enabled, max_per_order, plan_id,"
                        " regions, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (name, description.strip(), type_, cents, 1 if enabled else 0, max_per_order, plan_id, regions, now, now))
        return cur.lastrowid


def stock_counts(conn, product_id):
    out = {"available": 0, "reserved": 0, "sold": 0, "void": 0}
    for r in conn.execute("SELECT status, COUNT(*) n FROM card_keys WHERE product_id=? GROUP BY status", (product_id,)):
        out[r["status"]] = r["n"]
    return out


def import_cards(core, product_id, raw_text):
    """One code per line. Returns (added, duplicates, invalid)."""
    p = core.conn.execute("SELECT type FROM products WHERE id=?", (product_id,)).fetchone()
    if not p or p["type"] != "card":
        raise CatalogError("只能向「卡密发货」商品导入卡密")
    seen, codes, invalid, in_file_dups = set(), [], 0, 0
    for line in raw_text.splitlines():
        code = line.strip()
        if not code:
            continue
        if len(code) > 500:
            invalid += 1
            continue
        h = core.crypto.hash_code(code)
        if h in seen:
            in_file_dups += 1
            continue
        seen.add(h)
        codes.append((code, h))
    if len(codes) > 20000:
        raise CatalogError("单次最多导入 20000 条")
    added = dups = 0
    now = core.now()
    with tx(core.conn) as c:
        for code, h in codes:
            cur = c.execute("INSERT OR IGNORE INTO card_keys(product_id, code_enc, code_hash, mask, created_at)"
                            " VALUES (?,?,?,?,?)", (product_id, core.crypto.encrypt(code), h, mask_code(code), now))
            if cur.rowcount:
                added += 1
            else:
                dups += 1
    return added, dups + in_file_dups, invalid


def void_card(core, card_id):
    with tx(core.conn) as c:
        cur = c.execute("UPDATE card_keys SET status='void' WHERE id=? AND status='available'", (card_id,))
        return cur.rowcount == 1

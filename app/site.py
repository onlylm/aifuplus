"""Admin-editable storefront content (support contact, FAQ, legal text...)."""
import re

DEFAULTS = {
    "site_name": "aifu.plus",
    "support_wechat": "",
    "support_hours": "",
    "support_qr_url": "",
    "disclaimer": "本站为第三方充值服务，与 OpenAI 无隶属关系。ChatGPT 是 OpenAI 的商标。",
    "refund_policy": "",
    "faq": "",
    "terms_text": "",
    "privacy_text": "",
    "session_guide": "",
    "session_guide_url": "",
}
LIMITS = {"site_name": 40, "support_wechat": 60, "support_hours": 80, "support_qr_url": 300, "disclaimer": 400,
          "refund_policy": 4000, "session_guide": 2000, "session_guide_url": 300, "faq": 6000, "terms_text": 20000, "privacy_text": 20000}

DEFAULT_FAQ = """充值需要提供什么？
需要提供要充值账号的「充值凭证」（账号授权信息），不需要账号密码。信息只用于本次充值，加密保存，充值完成后立即删除。

付款后多久到账？
付款成功后系统自动提交充值，通常几分钟内完成，进度会在订单页实时显示。

支持哪些支付方式？
目前支持支付宝扫码支付。

忘了订单链接怎么办？
到「查询订单」，同时输入订单号和下单邮箱即可查看订单状态。卡密类订单出于安全考虑，只能通过下单时保存的链接或登录下单账号查看，找不回时请联系客服。"""


DEFAULT_GUIDE = """在电脑浏览器里登录要充值的账号。
打开商家提供的充值凭证获取页面。
选中页面里的全部内容并复制，回到这里粘贴到输入框。"""


def get_all(conn):
    d = dict(DEFAULTS)
    for r in conn.execute("SELECT key, value FROM site_settings"):
        if r["key"] in d:
            d[r["key"]] = r["value"]
    return d


def save(core, form):
    for k, default in DEFAULTS.items():
        if k not in form:
            continue
        v = form[k].strip()[:LIMITS[k]]
        if k in ("support_qr_url", "session_guide_url") and v and not re.match(r"^(https?://|/)", v):
            raise ValueError("客服二维码地址必须以 http(s):// 或 / 开头")
        if k == "site_name" and not v:
            v = default
        core.conn.execute("INSERT INTO site_settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (k, v))


def parse_faq(text):
    """Blocks separated by blank lines: first line = question, rest = answer."""
    items = []
    for block in re.split(r"\n\s*\n", (text or "").strip()):
        lines = [l.rstrip() for l in block.strip().splitlines()]
        if len(lines) >= 2 and lines[0].strip():
            items.append((lines[0].strip(), "\n".join(lines[1:]).strip()))
    return items

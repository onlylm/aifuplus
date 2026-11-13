"""Admin-managed configuration: Alipay + upstream (Pro20x Automate)."""
import re

from . import alipay as ali
from .upstream import Pro20xClient


class Unavailable(Exception):
    pass


class ConfigError(Exception):
    pass


def get_alipay(core):
    return dict(core.conn.execute("SELECT * FROM alipay_config WHERE id=1").fetchone())


def base_url(core, cfg=None):
    cfg = cfg or get_alipay(core)
    return (cfg["public_base_url"] or core.public_base_url_env or "").rstrip("/")


def notify_url(core, cfg=None):
    b = base_url(core, cfg)
    return b + "/notify/alipay" if b else ""


def alipay_status(core):
    cfg = get_alipay(core)
    cfg["has_private_key"] = bool(cfg["private_key_enc"])
    cfg["fingerprint"] = ""
    if cfg["has_private_key"]:
        try:
            cfg["fingerprint"] = ali.private_key_fingerprint(
                ali.load_private_key(core.crypto.decrypt(cfg["private_key_enc"])))
        except Exception:
            cfg["fingerprint"] = "（无法解密）"
    cfg["notify_url"] = notify_url(core, cfg)
    cfg.pop("private_key_enc")
    return cfg


def save_alipay(core, enabled, mode, app_id, merchant_pid, private_key, alipay_public_key,
                public_base_url, timeout_minutes):
    cfg = get_alipay(core)
    if mode not in ("sandbox", "production"):
        raise ConfigError("模式无效")
    app_id, merchant_pid = app_id.strip(), merchant_pid.strip()
    if app_id and not re.fullmatch(r"\d{8,20}", app_id):
        raise ConfigError("App ID 应为纯数字")
    if merchant_pid and not re.fullmatch(r"\d{8,20}", merchant_pid):
        raise ConfigError("商户 PID（2088 开头）应为纯数字")
    public_base_url = public_base_url.strip().rstrip("/")
    if public_base_url and not re.match(r"^https?://", public_base_url):
        raise ConfigError("公网访问地址必须以 http:// 或 https:// 开头")
    try:
        timeout_minutes = int(timeout_minutes)
    except ValueError:
        raise ConfigError("订单有效期必须是整数分钟")
    if not 5 <= timeout_minutes <= 120:
        raise ConfigError("订单有效期需在 5~120 分钟")
    priv_enc = cfg["private_key_enc"]
    if private_key.strip():
        try:
            key = ali.load_private_key(private_key)
        except ValueError as e:
            raise ConfigError(str(e))
        priv_enc = core.crypto.encrypt(key.private_bytes(
            ali.serialization.Encoding.PEM, ali.serialization.PrivateFormat.PKCS8,
            ali.serialization.NoEncryption()).decode())
    pub = alipay_public_key.strip() or cfg["alipay_public_key"]
    if alipay_public_key.strip():
        try:
            ali.load_public_key(alipay_public_key)
        except ValueError as e:
            raise ConfigError(str(e))
    if enabled and not (app_id and merchant_pid and priv_enc and pub):
        raise ConfigError("启用前请填写完整：App ID、商户 PID、应用私钥、支付宝公钥")
    core.conn.execute(
        "UPDATE alipay_config SET enabled=?, mode=?, app_id=?, merchant_pid=?, private_key_enc=?,"
        " alipay_public_key=?, public_base_url=?, order_timeout_minutes=?, updated_at=? WHERE id=1",
        (1 if enabled else 0, mode, app_id, merchant_pid, priv_enc, pub, public_base_url,
         timeout_minutes, core.now()))


def alipay_client(core, require_enabled=True):
    cfg = get_alipay(core)
    if require_enabled and not cfg["enabled"]:
        raise Unavailable("支付宝支付未启用")
    if not (cfg["app_id"] and cfg["private_key_enc"] and cfg["alipay_public_key"]):
        raise Unavailable("支付宝配置不完整")
    return ali.AlipayClient(cfg["app_id"], core.crypto.decrypt(cfg["private_key_enc"]),
                            cfg["alipay_public_key"], cfg["mode"], notify_url(core, cfg),
                            transport=core.alipay_transport, clock=core.clock)


# ---- upstream -------------------------------------------------------------
def get_upstream(core):
    return dict(core.conn.execute("SELECT * FROM upstream_config WHERE id=1").fetchone())


def upstream_status(core):
    cfg = get_upstream(core)
    cfg["has_key"] = bool(cfg["api_key_enc"])
    cfg.pop("api_key_enc")
    return cfg


def save_upstream(core, enabled, base_url_, api_key, card_platform_id):
    cfg = get_upstream(core)
    base_url_ = base_url_.strip().rstrip("/")
    if not re.match(r"^https://", base_url_):
        raise ConfigError("上游地址必须以 https:// 开头")
    key_enc = cfg["api_key_enc"]
    if api_key.strip():
        key_enc = core.crypto.encrypt(api_key.strip())
    if enabled and not key_enc:
        raise ConfigError("启用前请填写 Automate Key")
    cp = card_platform_id.strip()
    if cp and not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", cp):
        raise ConfigError("卡台 ID 格式无效")
    core.conn.execute(
        "UPDATE upstream_config SET enabled=?, base_url=?, api_key_enc=?, card_platform_id=?, updated_at=? WHERE id=1",
        (1 if enabled else 0, base_url_, key_enc, cp, core.now()))


def upstream_client(core, require_enabled=True):
    cfg = get_upstream(core)
    if require_enabled and not cfg["enabled"]:
        raise Unavailable("自动充值还没有启用，请到「上游充值」页打开「启用自动充值」。")
    if not cfg["api_key_enc"]:
        raise Unavailable("还没有保存上游密钥，请先在「上游充值」页保存 Automate Key。")
    return Pro20xClient(cfg["base_url"], core.crypto.decrypt(cfg["api_key_enc"]),
                        transport=core.upstream_transport)

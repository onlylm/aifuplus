"""Plain-language layer: turns upstream codes / internal states into text an operator or buyer can read.
Technical codes are only ever shown inside collapsed 'technical details' blocks."""

REGION_NAMES = {
    "PH": "菲律宾", "TZ": "坦桑尼亚", "SE": "瑞典", "EG": "埃及", "US": "美国", "GB": "英国", "JP": "日本",
    "SG": "新加坡", "HK": "中国香港", "TW": "中国台湾", "KR": "韩国", "DE": "德国", "FR": "法国", "CA": "加拿大",
    "AU": "澳大利亚", "IN": "印度", "TR": "土耳其", "NG": "尼日利亚", "BR": "巴西", "MX": "墨西哥",
    "ID": "印度尼西亚", "TH": "泰国", "VN": "越南", "MY": "马来西亚", "PK": "巴基斯坦", "AR": "阿根廷", "CL": "智利",
}


def region_name(code):
    code = (code or "").strip().upper()
    return REGION_NAMES.get(code, code)


ERROR_TEXT = {
    "automate_key_required": "还没有保存上游密钥，请先在「上游充值」页保存 Automate Key。",
    "automate_key_invalid": "上游密钥无效或已被停用，请到上游网页端重新获取并保存。",
    "automate_rate_limit_exceeded": "上游请求过于频繁，系统会稍后自动重试。",
    "automate_json_invalid": "提交给上游的信息格式有误，请联系技术支持。",
    "automate_client_order_required": "提交给上游的订单信息不完整，请联系技术支持。",
    "automate_plan_invalid": "上游找不到这个套餐，请先「同步套餐」，再重新创建商品。",
    "automate_plan_disabled": "上游已经关闭了这个套餐。",
    "automate_plan_region_not_allowed": "该套餐暂不支持用户选择的地区。",
    "automate_card_invalid": "支付卡信息不完整，请检查卡台。",
    "automate_account_recent_success": "该账号 10 分钟内已有成功订单，系统会稍后自动重试。",
    "automate_account_busy": "该账号正在处理另一笔订单，系统会稍后自动重试。",
    "automate_account_failed_limit": "该账号最近 3 小时内失败次数过多，上游暂时禁止再提交，请稍后再试。",
    "automate_points_insufficient": "上游账户积分不足，请先充值积分。",
    "automate_payment_source_invalid": "这个套餐不支持「自动选卡」付款，请换一个套餐。",
    "automate_payment_source_unsupported": "这个套餐不支持「自动选卡」付款，请换一个套餐。",
    "card_platform_disabled": "上游暂未开放「自动选卡」功能。",
    "card_platform_not_bound": "上游账号还没有绑定卡台，请先到上游网页端的「卡台」页绑定。",
    "card_platform_no_eligible_card": "卡台目前没有可用的支付卡，请检查卡台库存。",
    "card_platform_insufficient_balance": "卡台余额不足，请先给卡台充值。",
    "wallet_card_pool_unavailable": "卡池暂时不可用，请稍后再试或联系上游。",
    "protocol_tool_interrupted": "上游服务重启，导致任务中断，可以点「重试」。",
}


def explain(code="", http=None, message=""):
    if code in ERROR_TEXT:
        return ERROR_TEXT[code]
    if http in (401, 403):
        return "上游密钥无效或没有权限，请到「上游充值」页检查 Automate Key。"
    if http == 429:
        return "上游请求过于频繁，系统会稍后自动重试。"
    if http and http >= 500:
        return "上游服务暂时出错，系统会稍后自动重试。"
    return "上游返回了无法识别的错误，请展开「技术详情」后联系技术支持。"


PAY_TEXT = {"unpaid": "待付款", "paid": "已付款", "closed": "已关闭"}
FUL_TEXT = {"none": "未开始", "pending": "排队中", "processing": "处理中", "delivered": "已完成",
            "blocked": "需要处理", "failed": "充值失败", "manual": "需人工核对"}
UP_TEXT = {"queued": "上游排队中", "running": "上游充值中", "captcha_required": "上游等待验证", "succeeded": "上游已成功",
           "success": "上游已成功", "failed": "上游充值失败", "cancelled": "上游已取消", "canceled": "上游已取消",
           "manual_review": "上游待人工核实", "": "尚未提交"}
EVENT_TEXT = {
    "order_created": "创建订单", "payment_confirmed": "确认收款", "payment_amount_mismatch": "收款金额不符",
    "payment_rejected": "拒绝确认收款", "payment_unknown_order": "收到未知订单的通知", "notify_bad_signature": "收到验签失败的通知",
    "notify_wrong_merchant": "收到商户信息不符的通知", "order_closed": "订单关闭", "delivered": "发货 / 充值完成",
    "fulfil_blocked": "无法自动完成", "fulfil_failed": "充值失败", "fulfil_manual": "转人工核对",
    "fulfil_exception": "处理出现异常", "blocked": "无法自动发货", "admin_retry": "管理员重试", "admin_mark": "管理员手动处理",
}
# status -> colour class used by chips
FUL_TONE = {"none": "off", "pending": "wait", "processing": "wait", "delivered": "ok", "blocked": "bad", "failed": "bad", "manual": "warn"}
PAY_TONE = {"unpaid": "wait", "paid": "ok", "closed": "off"}


def mask_email(e):
    e = e or ""
    if "@" not in e:
        return e
    a, b = e.split("@", 1)
    return (a[:1] + "•••••" if a else "•••••") + "@" + b


def register(app):
    from . import pricing
    app.jinja_env.filters.update(
        usd=lambda c: pricing.fmt_usd(c), rate=lambda m: pricing.fmt_rate(m), cny=lambda f: pricing.fmt_cny(f),
        region_name=region_name, pay_text=lambda s: PAY_TEXT.get(s, s), ful_text=lambda s: FUL_TEXT.get(s, s),
        up_text=lambda s: UP_TEXT.get((s or "").lower(), s), event_text=lambda s: EVENT_TEXT.get(s, s),
        ful_tone=lambda s: FUL_TONE.get(s, "off"), pay_tone=lambda s: PAY_TONE.get(s, "off"), mask_email=mask_email,
        regions_list=lambda s: [r for r in (s or "").split(",") if r])

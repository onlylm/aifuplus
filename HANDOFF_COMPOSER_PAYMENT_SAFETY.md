# 交接给 Composer：付款安全与履约顺序（业务逻辑）

界面与文案负责人（Claude）在收到角色分工前，已按「用户要求」做过一版下面的业务改动，随后**按分工撤回**，
没有保留在代码里。这里留下完整做法，供 Composer 决定是否采用并补测试。

## 用户要求的业务顺序（原文意思）
1. 用户填写并确认 → 2. 后端创建 unpaid 订单 → 3. 弹窗显示支付宝二维码 → 4. 回调/主动查询确认到账 →
5. 订单 paid → 6. 履约进入 pending → 7. **只有 Worker 才能提交上游** → 8. 充值中 / 结果。

提交上游之前必须再次确认：支付已可靠确认、实付金额 = 订单金额、商户订单号一致、该订单尚未提交过上游、
fulfillment 状态允许。重复回调 / Worker 重启 / 刷新页面 / 回调与查单并发，都只能充值一次。

## 之前的实现思路（已撤回）
`app/fulfillment.py`
```python
def _payment_guard(core, order_id):
    """None = really paid, else a plain reason. Reads the stored payment record, not just a status flag."""
    row = core.conn.execute(
        "SELECT o.order_no, o.amount_cents, o.payment_status, p.out_trade_no, p.amount_cents AS paid_cents, p.order_id AS pay_order"
        " FROM orders o LEFT JOIN payments p ON p.order_id=o.id WHERE o.id=?", (order_id,)).fetchone()
    if row is None or row["payment_status"] != "paid": return "订单还没有付款"
    if row["out_trade_no"] is None: return "没有找到这笔订单的有效付款记录"
    if row["out_trade_no"] != row["order_no"] or row["pay_order"] != order_id: return "付款记录与订单号不一致"
    if row["paid_cents"] != row["amount_cents"]: return "实际付款金额与订单金额不一致"
    return None
```
- `run_recharge()`：`_claim()` 之后调用一次；在 `client.submit(...)` 之前再调用一次（并要求 `upstream_task_id` 为空）；
  不通过 → `_mark(..., "blocked", "payment_not_confirmed", "没有确认到有效付款，已停止充值：…")`，**不**调用上游。
- `run(core, order_id, force=False, worker=False)`：卡密仍然内联发放；`api_recharge` 只有 `worker=True` 才会进入 `run_recharge`。
  `sweep()` 调用 `run(..., worker=True)`。于是支付回调、状态接口、后台「重试」按钮都只把订单排进 pending，由 Worker 提交。
- 受影响的旧测试需要在付款后手动跑一次 `worker.sweep_once(core)`（模拟 Worker 下一轮）：
  `tests/test_recharge.py` 的 `pay()` 和 4 处 `admin_retry` 之后，`tests/test_pricing.py` 的访客购买用例。

## 建议新增的测试（上游 mock 计数）
- 支付前 `POST /tasks` 次数 = 0；确认付款并跑 Worker 后 = 1；重复回调后仍 = 1。
- 验签失败 / 金额不符 / 订单号不一致 → 0。
- 直接把 `orders.payment_status` 改成 paid 但没有 `payments` 记录 → Worker 也不得提交。
- Worker 重启（claim 后崩溃，锁过期）→ 同一个 clientOrderId，仍只有一个上游任务。
- 回调与主动查单并发 → 1。

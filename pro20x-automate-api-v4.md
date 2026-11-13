# Pro20x Automate 开放 API V4

Base URL: https://pro20x.com/api/v3/automate

## 1. 快速接入

文档版本 V4，接口地址保持不变（`/api/v1`、`/api/v2`、`/api/v3` 等价）。

### 请求头

```http
X-Automate-Key: AUTOMATE_KEY
Content-Type: application/json
X-Request-Id: CLIENT_REQUEST_ID
```

- `X-Automate-Key`：在网页端获取；重新生成后旧 Key 立即失效。
- `X-Request-Id`：可选，用于调用方追踪请求。创建类接口的幂等编号使用请求体中的 `clientOrderId`，相同编号重复提交返回原任务。

### 响应格式

成功返回 `"success": true` 和业务字段；失败返回统一错误结构（见第 9 节），按 `code` 判断，不要匹配 `message` 文案。每个响应都带 `requestId`。

### 异步任务规则

查询订阅、升级查价、取消续费、开启续费都是异步任务：

1. 提交后返回 `202`，包含 `job` 和轮询地址 `poll`。
2. 每 2～3 秒轮询 `poll`：`job.status` 为 `queued`（排队中）或 `running` 时继续；`success` 时读取 `job.result`；`failed` 时读取 `job.errorCode`。
3. 任务记录持久保存，只能由提交它的 Key 查询；其他 Key 查询返回 `404`。
4. 任务被服务重启中断时返回 `failed` + `protocol_tool_interrupted`，可重新提交。

## 2. 配置、积分与钱包

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /config | 套餐、区域、升级路线、支付方式、积分消耗与钱包价格 |
| GET | /points?limit=50 | 积分余额与最近流水 |
| GET | /wallet?limit=20 | 钱包余额（美元）、钱包价格表与最近流水 |

```bash
curl "https://pro20x.com/api/v3/automate/config" \
  -H "X-Automate-Key: AUTOMATE_KEY"
```

| /config 字段 | 说明 |
| --- | --- |
| `plans` / `planRegions` | 兼容旧对接的套餐列表，以及每个套餐允许的区域 |
| `catalog` | 一级分类和二级选项；每个组的 `credentialFields` 标出账号资料字段，每个二级项的 `request` 是提交任务时要合并的静态参数，不发送一级分类 ID |
| `upgradePaths` / `upgradePaymentModes` | 升级路线与升级支付方式 |
| `existingGoUpgradePaths` | “Go 升级”套餐的四条独立路线，受该套餐自己的路线开关控制 |
| `paymentSources` | 当前开放的支付方式，**对象数组** `{ id, label }`，`id` 取值 `card`、`auto_card`、`wallet`（见下方示例） |
| `pointsBilling.taskCosts` | 各套餐提交任务消耗的积分（提交冻结，成功扣除，失败或取消退回） |
| `wallet.prices[planId][region]` | 钱包支付价（美元），`null` 表示该地区不支持钱包支付 |
| `subscriptionRenewPointsCost` | 开启续费消耗的积分 |

例如选“已有 Go 升级 → Go → Plus”时，从 `catalog` 读取的二级项包含 `request: { "planId": "go-existing-upgrade", "upgradePath": "go_to_plus" }`；另选 `paymentMode`，再填写账号和支付资料。GPT 直充的二级项只需 `planId`。旧客户端可继续使用 `plans` 等原有字段；路径和请求地址不变。

`paymentSources` 每一项是对象，判断是否支持某种支付方式请比较 `id`，不要直接对数组做 `includes('auto_card')`：

```json
"paymentSources": [
  { "id": "card", "label": "填卡支付" },
  { "id": "auto_card", "label": "自动选卡" },
  { "id": "wallet", "label": "钱包支付" }
]
```

```js
const supportsAutoCard = (config.paymentSources || []).some((item) => item?.id === 'auto_card');
```

### 区域与货币

| 区域代码 | 区域 | 货币代码 |
| --- | --- | --- |
| PH | 菲律宾 | PHP |
| TZ | 坦桑尼亚 | TZS |
| SE | 瑞典 | SEK |
| EG | 埃及 | EGP |

实际可用区域和货币以 `/config` 返回为准。升级和 Credits 的提交区域必须与账号当前账单币种一致。

### GET /wallet

钱包只能在网页端用 USDT 充值，只能消费，不能提现。

```json
{
  "success": true,
  "wallet": { "currency": "USD", "balance": 120.0, "reserved": 20.0, "available": 100.0 },
  "prices": { "plus-monthly": { "PH": 16.0, "US": 20.35 } },
  "transactions": [
    { "type": "task_reserve", "amount": -20.0, "taskId": "AUTO-XXXXXXXX", "createdAt": "2026-10-17T08:00:00.000Z" }
  ]
}
```

## 3. 查询账号订阅

查询账号下各空间的套餐、订阅来源、币种、续费状态及 Pro 20x 权限。只读，不消耗积分。

### POST /account-check

```json
{
  "authSessionJson": {
    "accessToken": "ACCESS_TOKEN",
    "sessionToken": "SESSION_TOKEN",
    "account_id": "ACCOUNT_ID",
    "user": { "email": "account@example.com" }
  }
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `authSessionJson` | 是 | 账号会话 JSON（字段见第 5 节），可传对象或 JSON 字符串 |

### GET /account-check/jobs/{jobId}

```json
{
  "success": true,
  "job": {
    "id": "ptj_xxxxxxxx",
    "type": "account_check",
    "status": "success",
    "errorCode": "",
    "result": {
      "email": "account@example.com",
      "selectedAccountId": "ACCOUNT_ID",
      "accountCount": 1,
      "plusPromoEligible": true,
      "eligiblePromoCampaignId": "plus-1-month-50-pct-off",
      "accounts": [
        {
          "accountId": "ACCOUNT_ID",
          "planLabel": "Free",
          "tier": "free",
          "hasActiveSubscription": false,
          "subscriptionStatus": "inactive",
          "purchaseOriginPlatform": "",
          "billingCurrency": "USD",
          "willRenew": null,
          "expirationTime": "",
          "pro20xEligible": false,
          "plusPromoEligible": true,
          "eligiblePromoCampaignId": "plus-1-month-50-pct-off"
        }
      ]
    }
  }
}
```

| 结果字段 | 说明 |
| --- | --- |
| `selectedAccountId` | 本次识别的账号空间；在 `accounts` 中找同一 `accountId` 的项 |
| `accounts[].planLabel` / `tier` | 套餐名称 / 套餐档位（`free`、`go`、`plus`、`pro5x`、`pro20x`、`promax`） |
| `accounts[].hasActiveSubscription` | 是否有有效订阅 |
| `accounts[].purchaseOriginPlatform` | 订阅来源（网页端、App Store 等） |
| `accounts[].billingCurrency` | 账单币种 |
| `accounts[].willRenew` | 是否自动续费 |
| `accounts[].expirationTime` | 当前周期到期时间 |
| `accounts[].pro20xEligible` | 是否具备 Pro 20x 权限 |
| `plusPromoEligible` / `accounts[].plusPromoEligible` | 是否有 Plus 优惠（半价）资格：`true` 有资格；`false` 无资格；`null` 查询未返回资格信息，无法确认。外层是 `selectedAccountId` 对应账号空间的结果 |
| `eligiblePromoCampaignId` / `accounts[].eligiblePromoCampaignId` | 可用的 Plus 优惠活动 ID（如 `plus-1-month-50-pct-off`），无资格时为空字符串 |

`plusPromoEligible`、`pro20xEligible` 都以 ChatGPT 实际返回为准，不按套餐名推断。判断是否有优惠请用 `plusPromoEligible === true`，`null` 表示未确认，不等于无资格。提交 Plus 月付任务时不需要回传优惠 ID：`detectPlusPromo` 省略或为 `true` 时服务端按验活结果自动识别，有资格才应用优惠。

#### 错误码

- `automate_auth_invalid` / `protocol_tool_json_invalid`（400）：`authSessionJson` 格式不正确
- `protocol_tool_access_token_missing`（400）：缺少 `accessToken`
- `automate_account_check_busy`（429）：排队任务较多，稍后重试
- `automate_account_check_job_not_found`（404）：任务不存在或不属于当前 Key

## 4. 升级查价

查询当前付费套餐升级到目标套餐需要补的官方差价（官方升级预览）。只读：不提交升级、不扣款、不消耗积分。

### POST /upgrade-quote

```json
{
  "targetPlan": "pro20x",
  "authSessionJson": {
    "accessToken": "ACCESS_TOKEN",
    "sessionToken": "SESSION_TOKEN",
    "account_id": "ACCOUNT_ID",
    "user": { "email": "account@example.com" }
  }
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `targetPlan` | 是 | 目标套餐：`plus`（Plus）、`pro5x`（Pro Lite）、`pro20x`（Pro）、`promax`（Pro Max） |
| `authSessionJson` | 是 | 账号会话 JSON |

仅支持网页端购买的有效付费订阅。

### GET /upgrade-quote/jobs/{jobId}

```json
{
  "success": true,
  "job": {
    "id": "ptj_xxxxxxxx",
    "type": "subscription_upgrade_quote",
    "status": "success",
    "errorCode": "",
    "result": {
      "currentPlan": "plus",
      "currentPlanLabel": "Plus",
      "targetPlan": "pro20x",
      "targetPlanLabel": "Pro",
      "currency": "USD",
      "amountDue": 18000,
      "amountDueDisplay": "USD 180.00",
      "taxAmount": 0,
      "renewalDate": "2026-11-15T00:00:00Z",
      "defaultPaymentMethod": { "cardBrand": "visa", "cardLast4": "4242" }
    }
  }
}
```

| 结果字段 | 说明 |
| --- | --- |
| `amountDue` | 应付差价，货币最小单位（如 USD 的分） |
| `amountDueDisplay` | 格式化后的差价，例如 `USD 180.00` |
| `currency` | 差价币种 |
| `currentPlan` / `targetPlan` | 当前套餐 / 目标套餐 |
| `renewalDate` | 下次续费时间 |
| `defaultPaymentMethod` | 账号默认支付卡品牌与后四位 |

#### 错误码

- `automate_upgrade_quote_target_invalid`（400）：`targetPlan` 无效
- `automate_upgrade_quote_not_active`：账号没有可升级的有效付费订阅
- `automate_upgrade_quote_unsupported_origin`：订阅不是网页端购买，无法查价
- `automate_upgrade_quote_same_plan`：账号已是目标套餐
- `automate_upgrade_quote_oauth_invalid`：会话已失效，请更新 `authSessionJson`
- `automate_upgrade_quote_preview_failed`：官方预览接口返回错误
- `automate_upgrade_quote_busy`（429）：查价任务较多，稍后重试
- `automate_upgrade_quote_job_not_found`（404）：任务不存在或不属于当前 Key

## 5. 提交任务

所有提交都用 `POST /tasks`，按 `planId` 区分任务类型。成功返回任务详情，之后按第 6 节查询进度。

### 通用字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `clientOrderId` | 是 | 调用方订单号，幂等：相同编号重复提交返回原任务 |
| `planId` | 是 | `/config.plans` 中的套餐 ID |
| `checkoutCountry` | 是 | 充值区域，必须在 `/config.planRegions[planId]` 内 |
| `authSessionJson` | GPT 套餐必填 | ChatGPT 账号会话 JSON，字段见下表；Grok XPASS 使用 `grokXpassSsoCookie` |
| `paymentSource` | 否 | 支付方式，默认 `card`，见下文 |
| `card` | 条件必填 | `paymentSource=card` 时必填 |
| `cancelAutoRenew` | 否 | 支付成功后是否取消自动续费，默认按后台配置 |
| `deletePaymentCard` | 否 | GPT 订阅直充支付成功且订阅生效后，尝试删除本单实际扣款卡；省略或传 `true` 为开启，传 `false` 为关闭。也接受 `delete_payment_card`。升级、Credits、Team、Grok、Claude 不适用。 |
| `detectPlusPromo` | 否 | 仅适用于 Plus 月付；省略或传 `true` 时按验活结果识别 `plus-1-month-50-pct-off`，有资格才应用优惠；传 `false` 时跳过优惠识别。 |

GPT 普通直充提交前默认检查当前套餐：若有效网页订阅与目标套餐相同，自动检查并关闭现有自动续费，再重新查询。仅查到 Free 才建单；不同套餐升级沿用原流程。此步骤与支付成功后的 `cancelAutoRenew` 独立，无需传入额外开关。

#### authSessionJson 字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `accessToken` | 是 | 账号登录态 accessToken |
| `sessionToken` | 建议 | 缺少时无法在充值后取消续费 |
| `account_id` | 是 | 要充值的 ChatGPT 账号 ID（也可写 `accountId`）。登录态下有多个账号 / 工作区时用它指定目标账号 |
| `user.email` | 是 | 账号邮箱 |

#### 支付方式 paymentSource

| 值 | 说明 | 积分 | 钱包 |
| --- | --- | --- | --- |
| `card`（默认） | 使用请求中的 `card` | 按规则冻结 / 扣除 | 不扣 |
| `auto_card` | 不传 `card`；从网页端「卡台」页绑定的卡台按用卡策略选卡、补余额（等待到账，超时或失败按设置换卡，仍失败则任务失败），付款被拒按策略换卡；按用卡策略的卡台优先级（最多 3 个）依次使用卡台，无可用卡、余额不足或换卡次数用完时换下一个卡台（换卡次数每个卡台单独计算）；任务结束后卡内余额自动退回卡台；可选 `cardPlatformId` 只使用指定卡台 | 按规则冻结 / 扣除 | 优先级内卡台都「没有可用卡」或「卡台余额不足」且开启「改用钱包支付」时改为钱包支付 |
| `wallet` | 不传 `card`；平台用后台卡充值，按 `/config.wallet.prices` 从钱包扣款 | 不扣 | 提交冻结，成功扣除，失败退回 |

`auto_card` 和 `wallet` 只支持 GPT 直充套餐（Go / Plus / Pro 5x / Pro 20x 直付 `pro20x-direct-monthly` / Pro Max），不支持 `pro20x-monthly`（Plus → Pro 升级流程）、升级、Credits 和 Team。任务详情返回 `paymentSource`；钱包支付任务另含 `wallet.amount` 和 `wallet.status`（`reserved` / `charged` / `released`）。

### 普通充值

```json
{
  "clientOrderId": "CLIENT_ORDER_ID",
  "planId": "plus-monthly",
  "checkoutCountry": "PH",
  "paymentSource": "card",
  "authSessionJson": {
    "accessToken": "ACCESS_TOKEN",
    "sessionToken": "SESSION_TOKEN",
    "account_id": "ACCOUNT_ID",
    "user": { "email": "account@example.com" }
  },
  "card": {
    "number": "CARD_NUMBER",
    "cvc": "CVC",
    "expMonth": "12",
    "expYear": "2030"
  },
  "cancelAutoRenew": true,
  "deletePaymentCard": true,
  "detectPlusPromo": true
}
```

### 钱包支付

```json
{
  "clientOrderId": "CLIENT_ORDER_ID",
  "planId": "plus-monthly",
  "checkoutCountry": "PH",
  "paymentSource": "wallet",
  "authSessionJson": {
    "accessToken": "ACCESS_TOKEN",
    "sessionToken": "SESSION_TOKEN",
    "account_id": "ACCOUNT_ID",
    "user": { "email": "account@example.com" }
  }
}
```

### Team / Business

```json
{
  "clientOrderId": "CLIENT_TEAM_ORDER_ID",
  "planId": "team-subscription",
  "checkoutCountry": "PH",
  "authSessionJson": {
    "accessToken": "ACCESS_TOKEN",
    "sessionToken": "SESSION_TOKEN",
    "account_id": "ACCOUNT_ID",
    "user": { "email": "account@example.com" }
  },
  "card": {
    "number": "CARD_NUMBER",
    "cvc": "CVC",
    "expMonth": "12",
    "expYear": "2030"
  },
  "teamPlanData": {
    "workspaceName": "WORKSPACE_NAME",
    "priceInterval": "month",
    "defaultSeats": 2,
    "proliteSeats": 0,
    "promoCode": "PROMO_CODE"
  },
  "cancelAutoRenew": true
}
```

席位总数为 2～200；`priceInterval` 支持 `month` 或 `year`；`promoCode` 可留空。

### 升级

```json
{
  "clientOrderId": "CLIENT_UPGRADE_ORDER_ID",
  "planId": "subscription-upgrade",
  "upgradePath": "plus_to_pro5x",
  "paymentMode": "new_card",
  "checkoutCountry": "PH",
  "authSessionJson": {
    "accessToken": "ACCESS_TOKEN",
    "sessionToken": "SESSION_TOKEN",
    "account_id": "ACCOUNT_ID",
    "user": { "email": "account@example.com" }
  },
  "card": {
    "number": "CARD_NUMBER",
    "cvc": "CVC",
    "expMonth": "12",
    "expYear": "2030"
  },
  "cancelAutoRenew": true
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `planId` | 是 | Plus/Pro 升级传 `subscription-upgrade`；Go 升级传 `go-existing-upgrade` |
| `upgradePath` | 是 | Plus/Pro 套餐从 `/config.upgradePaths` 选非 Go 路线；Go 升级套餐从 `/config.existingGoUpgradePaths` 选 `go_to_plus`、`go_to_pro5x`、`go_to_pro20x`、`go_to_promax`（Pro Max / 50x） |
| `paymentMode` | 是 | `new_card` 新卡升级；`original_card` 原卡升级（不传 `card`） |

接口不会根据账号套餐自动判断直充或升级；提交升级必须显式传 `planId`、`upgradePath` 和 `paymentMode`。Go 升级直接从当前 Go 订阅升级，不执行 Free → Go 的链式购买；`checkoutCountry` 的币种必须与当前 Go 账单币种一致。

Free → Go → 升级使用 `planId=go-upgrade`，二级目标仍选 `go_to_plus` 等 Go 来源路线；提交 `card` 支付 Go 阶段，不传 `paymentMode`。

### Grok XPASS

```json
{
  "clientOrderId": "CLIENT_XPASS_ORDER_ID",
  "planId": "grok-xpass-monthly",
  "grokXpassProduct": "xpass-plus",
  "grokXpassInterval": "month",
  "grokXpassSsoCookie": "GROK_SSO_COOKIE",
  "checkoutCountry": "US",
  "card": {
    "number": "CARD_NUMBER",
    "cvc": "CVC",
    "expMonth": "12",
    "expYear": "2030"
  }
}
```

二级档位从 `/config.catalog` 的 `grok-xpass` 组选择，可选 `xpass-plus`、`xpass-premium`、`xpass-super`、`xpass-ultra`；账期使用 `grokXpassInterval`（`month` 或 `year`）。Grok 使用自己的 SSO Cookie 验活，不能提交 ChatGPT 的 `authSessionJson`。同一个 `clientOrderId` 重复提交只返回原任务；任务详情的 `grokXpass` 显示档位、账期及收尾状态，不回传 Cookie。

### Credits 点数充值

```json
{
  "clientOrderId": "CLIENT_CREDITS_ORDER_ID",
  "planId": "credits",
  "creditsQuantity": 2500,
  "checkoutCountry": "PH",
  "authSessionJson": {
    "accessToken": "ACCESS_TOKEN",
    "sessionToken": "SESSION_TOKEN",
    "account_id": "ACCOUNT_ID",
    "user": { "email": "account@example.com" }
  },
  "card": {
    "number": "CARD_NUMBER",
    "cvc": "CVC",
    "expMonth": "12",
    "expYear": "2030"
  }
}
```

`creditsQuantity` 必须是 50 的整数倍，并在 `/config.credits` 返回的范围内；最小为 50。Free 账号不能提交，账号账单币种必须与提交区域一致。

#### 错误码

- `automate_client_order_required`（400）：缺少 `clientOrderId`
- `automate_plan_invalid` / `automate_plan_disabled`：套餐无效或已关闭
- `automate_plan_region_not_allowed`（400）：套餐不支持该区域
- `automate_card_invalid`（400）：卡信息不完整
- `automate_account_recent_success` / `automate_account_busy`（409）：该账号 10 分钟内已有成功订单，或已有进行中的任务（升级任务不受 10 分钟限制）
- `automate_account_failed_limit`（422）：同一账号最近 3 小时内已有 5 条失败任务时暂时禁止提交；包含所有失败原因与套餐
- `automate_points_insufficient`（402）：可用积分不足
- `automate_payment_source_invalid` / `automate_payment_source_unsupported`（400）：支付方式无效或该任务不支持
- `wallet_payment_disabled` / `wallet_price_unavailable`（409）：钱包支付未开放或该地区不支持
- `wallet_insufficient`（402）：钱包可用余额不足
- `card_platform_disabled` / `card_platform_not_bound`（409）：自动选卡未开放或未绑定卡台
- 选卡阶段失败时，任务 `error.code` 可能为 `card_platform_no_eligible_card`、`card_platform_insufficient_balance`、`wallet_card_pool_unavailable`

## 6. 任务查询

建议每 3～5 秒轮询，终态后停止。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /tasks?status=all&page=1&pageSize=20 | 任务列表，支持 `clientOrderId` 精确查询 |
| GET | /tasks/{taskId} | 任务详情和完整进度 |
| GET | /tasks/{taskId}/invoice | 成功充值的账单地址 |

GPT 订阅直充的任务详情包含 `deletePaymentCard`（本单是否启用删卡）。完成后如执行了删卡收尾，`gptPaymentMethodCleanup` 返回 `status`、`verified`、`reason`：`deleted_verified` 表示本单卡已删除并核验；`skipped_*` 表示因条件不满足而跳过；`failed` 或 `failed_delete_unverified` 表示删卡失败或无法核验。未返回该对象表示尚未执行或未取得收尾结果。删卡状态与充值任务的 `status` 分开判断，删卡失败不代表支付失败。

### POST /tasks/{taskId}/force-terminate

仅允许在支付提交前取消任务。取消后会释放冻结积分或钱包金额，任务不会继续执行；如果任务已经进入支付提交或支付确认阶段，接口会返回 `409`，请等待最终结果。

```json
{ "note": "用户确认在支付提交前取消" }
```

常见错误码：

- `automate_force_terminate_payment_submission_started`（409）：已开始提交支付，不能取消
- `automate_force_terminate_payment_confirmed`（409）：支付已确认，不能取消
- `automate_force_terminate_task_not_active`（409）：任务当前状态不能取消

## 7. 开启续费

为官方 Web 订阅绑定支付卡并恢复自动续费。按 `/config.subscriptionRenewPointsCost` 冻结积分：成功扣除，失败退回；结果需核实时保持冻结。

### POST /subscription/renew

```json
{
  "clientOrderId": "CLIENT_RENEW_ORDER_ID",
  "authSessionJson": {
    "accessToken": "ACCESS_TOKEN",
    "sessionToken": "SESSION_TOKEN",
    "account_id": "ACCOUNT_ID",
    "user": { "email": "account@example.com" }
  },
  "cards": [{
    "number": "CARD_NUMBER",
    "cvc": "CVC",
    "expMonth": "12",
    "expYear": "2030"
  }]
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `clientOrderId` | 是 | 幂等订单号 |
| `authSessionJson` | 是 | 账号会话 JSON |
| `cards` | 是 | 1～3 张完整卡片，按顺序尝试 |

### 查询结果

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /subscription/jobs/{jobId} | 按任务 ID 查询 |
| GET | /subscription/renew?clientOrderId=CLIENT_RENEW_ORDER_ID | 按幂等订单号查询 |

除异步任务通用状态外，开启续费还可能处于 `manual_review`（结果待核实，请勿重复提交）。

#### 错误码

- `automate_subscription_renew_disabled`（409）：开启续费已由后台关闭
- `automate_subscription_renew_cards_invalid`（400）：卡片数量或卡信息无效
- `automate_renewal_account_busy`（409）：该账号已有处理中或待核实的开启续费任务
- `automate_points_insufficient`（402）：可用积分不足
- `automate_subscription_job_not_found`（404）：任务不存在或不属于当前 Key

## 8. 取消续费

关闭账号下一周期的自动续费。不退款，当前已付费周期内权益保留，不消耗积分。

### POST /subscription/cancel

```json
{
  "clientOrderId": "CLIENT_CANCEL_ORDER_ID",
  "confirm": true,
  "authSessionJson": {
    "accessToken": "ACCESS_TOKEN",
    "sessionToken": "SESSION_TOKEN",
    "account_id": "ACCOUNT_ID",
    "user": { "email": "account@example.com" }
  }
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `confirm` | 是 | 必须为 `true`，确认关闭自动续费 |
| `authSessionJson` | 是 | 账号会话 JSON |
| `clientOrderId` | 否（建议传） | 幂等订单号；相同订单号重复提交返回原任务（`reused: true`） |

同一账号已有排队或处理中的取消任务时，直接返回该任务（HTTP 200，`reused: true`）。

### 查询结果

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /subscription/cancel/jobs/{jobId} | 按任务 ID 查询 |
| GET | /subscription/cancel?clientOrderId=CLIENT_CANCEL_ORDER_ID | 按幂等订单号查询 |

```json
{
  "success": true,
  "job": {
    "id": "ptj_xxxxxxxx",
    "type": "subscription_cancel",
    "clientOrderId": "CLIENT_CANCEL_ORDER_ID",
    "status": "success",
    "errorCode": "",
    "result": {
      "cancelledAt": "2026-10-17T08:00:00.000Z",
      "alreadyCancelled": false,
      "verified": true,
      "account": { "planLabel": "Plus", "expirationTime": "2026-11-15T00:00:00Z" },
      "renewal": { "willRenew": false, "cancellationOutcome": "deactivate" }
    }
  }
}
```

| 结果字段 | 说明 |
| --- | --- |
| `alreadyCancelled` | `true` 表示提交前已关闭自动续费，本次未重复操作 |
| `verified` | 已复核 `will_renew=false` |
| `renewal.willRenew` | 取消后的自动续费状态，成功时为 `false` |
| `account` | 账号套餐与到期时间 |

#### 错误码

- `automate_subscription_cancel_confirmation_required`（400）：未传 `confirm: true`
- `automate_subscription_cancel_not_active`：账号没有可取消的有效付费订阅
- `automate_subscription_oauth_invalid`：会话已失效，请更新 `authSessionJson`
- `automate_subscription_renewal_check_failed`：续费状态查询失败
- `automate_subscription_cancel_unconfirmed`：取消请求已返回但未复核到 `will_renew=false`，可重新提交
- `automate_subscription_tool_busy`（429）：排队任务较多，稍后重试
- `automate_subscription_cancel_job_not_found`（404）：任务不存在或不属于当前 Key

## 9. 状态与错误

### 任务状态

- `queued`：已排队
- `running`：执行中
- `captcha_required`：等待 hCaptcha
- `succeeded`：充值或升级成功
- `failed`：任务失败
- `manual_review`：结果需要人工核实
- `cancelled`：已取消

### 错误响应

```json
{
  "success": false,
  "code": "ERROR_CODE",
  "message": "错误说明",
  "requestId": "REQ-EXAMPLE"
}
```

### 通用错误码

| HTTP | code | 说明 |
| --- | --- | --- |
| 400 | `automate_json_invalid` | 请求体不是合法 JSON |
| 401 | `automate_key_required` | 缺少 `X-Automate-Key` |
| 401 | `automate_key_invalid` | Key 无效或已停用 |
| 429 | `automate_rate_limit_exceeded` | 请求过于频繁，请退避后重试 |

### 升级账单错误

- `upgrade_target_invoice_not_found`：未找到目标套餐待付账单，未扣款
- `upgrade_invoice_target_mismatch`：待付账单与目标套餐不一致，未扣款
- `upgrade_invoice_unreadable`：账单详情暂时无法核验，未提交升级且未扣款
- `automate_upgrade_currency_mismatch`：账单币种与提交区域不一致，未扣款
- `automate_upgrade_currency_unverified`：无法确认升级账单币种，未扣款
- `latest_invoice_confirm_payment`：升级账单支付失败，读取返回中的 Stripe 错误码

# step2api

**StepFun Step Plan 多账号聚合网关** —— 把多个 Step API Key 收进一个本地服务，统一查看每个账号的套餐档位、Plan 时长与剩余额度，对外只暴露一个 Base URL，自动做粘性路由、故障转移、独立代理与代理轮转。

> 只支持**国外站**（[account.stepfun.ai](https://account.stepfun.ai/)）。导入国内站（`stepfun.com`）的 Key 会被直接拒绝。

---

## 它解决什么问题

一个人同时持有多张 Step Plan 订阅（或按量 API Key）时，日常会撞上这几件事：

- 不知道哪张卡还有多少 Credit，哪张快到期了
- 在 Claude Code / Cursor / OpenAI SDK 里来回换 Key，改一次配置重启一次
- 某个 Key 突然 429 或额度耗尽，正在跑的 agent 直接断掉
- 想给不同的 Key 走不同的出口 IP，或者希望请求在多个代理间轮换

step2api 把这些问题收在一个进程里解决：一个 SQLite 库管账号与代理，一个 HTTP 服务对外提供 OpenAI 兼容与 Anthropic 兼容的入口，一个零依赖的 Web 控制台看额度、配代理、查日志。

---

## 特性

| 能力 | 说明 |
| --- | --- |
| **多账号聚合** | 一个 Base URL 对接 N 个 Key，下游客户端不需要知道账号存在 |
| **Plan 额度监控** | 真实套餐档位、Credit 余量、剩余百分比、到期时间（经控制台接口获取） |
| **窗口限额** | 5 小时滚动窗口与周窗口的剩余比例展示（套餐适用时） |
| **Plan 时长** | 计算并展示距离额度重置 / 订阅到期的剩余时间，即将过期高亮告警 |
| **金额余额** | 按量计费通道余额（`balance` / 现金 / 赠送），与订阅额度分开展示 |
| **分级体系** | Step Plan 订阅通道与按量计费通道互不干扰，路径自动映射到各自上游 |
| **粘性路由** | 同一会话固定同一账号，保住 prompt cache 与上下文一致性 |
| **故障转移** | 上游 429/5xx/超时自动换账号重试，客户端无感 |
| **额度感知调度** | 额度耗尽的账号自动摘除；余额未知的账号不会被误杀 |
| **权重与优先级** | 账号可设 weight / priority / 分组，支持四种附加调度模式 |
| **独立代理** | 每个账号可选直连 / 全局代理 / 专属代理 / 代理池，四种模式 |
| **代理轮转** | 池内四种轮转策略；会话级粘性或每次轮转可选；代理健康检查与延迟排序 |
| **凭据加密** | 所有 API Key 与代理密码以 Fernet 密文落库，前端只显示指纹 |
| **流式透传** | SSE 边收边发，网关侧不缓冲整段响应 |
| **请求日志** | 每次转发记录实际命中的账号、代理、重试次数、耗时、token 用量 |
| **Web 控制台** | 零依赖单页应用，覆盖账号、代理、代理池、会话、日志、设置 |

---

## 快速开始

### 依赖

Python 3.10+。推荐用 [uv](https://docs.astral.sh/uv/)：

```bash
git clone https://github.com/huanxueshengmou/step2api.git
cd step2api
uv venv
uv pip install -e .
```

或者用 pip：

```bash
python -m venv .venv
.venv/Scripts/activate      # Windows
# source .venv/bin/activate  # macOS / Linux
pip install -e .
```

### 启动

```bash
step2api serve
# 或者
uv run python -m step2api serve
```

打开 <http://127.0.0.1:8787> 进入控制台。

### 导入账号

控制台「账号 → 导入账号」，粘贴内容即可。支持三种格式：

```
# 每行一个 Key
sk-aaaaaaaaaaaaaaaaaaaaaaaa
sk-bbbbbbbbbbbbbbbbbbbbbbbb

# Key 后面跟专属代理，用 | 分隔（制表符、逗号、分号也可以）
sk-cccccccccccccccccccccccc|http://user:pass@1.2.3.4:8080
sk-dddddddddddddddddddddddd|socks5://5.6.7.8:1080
```

也可以直接粘 JSON：

```json
[
  {"api_key": "sk-aaaa...", "name": "主号", "proxy": "http://1.2.3.4:8080"},
  {"api_key": "sk-bbbb...", "name": "备用"}
]
```

导入时可以选择代理分配方式：不分配、每个 Key 绑定自己的代理、或者全部塞进一个新建的代理池轮转。

命令行同样可以导入：

```bash
step2api import keys.txt --prefix acct- --verify
```

### 接入客户端

**Claude Code**（走 Step Plan 订阅通道，消耗 Credit）：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
export ANTHROPIC_AUTH_TOKEN=<网关令牌，未配置则随便填>
export ANTHROPIC_MODEL=step-3.5-flash
```

**OpenAI SDK**（走按量计费通道）：

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="<网关令牌>")
resp = client.chat.completions.create(
    model="step-3.5-flash",
    messages=[{"role": "user", "content": "hello"}],
)
```

控制台「设置 → 客户端配置」会直接生成可复制的片段。

---

## 路径映射

下游看到的路径会自动映射到上游对应通道：

| 下游请求 | 判定通道 | 上游目标 |
| --- | --- | --- |
| `/step_plan/v1/messages` | Step Plan 订阅 | `https://api.stepfun.ai/step_plan/v1/messages` |
| `/step_plan/v1/chat/completions` | Step Plan 订阅 | `https://api.stepfun.ai/step_plan/v1/chat/completions` |
| `/step_plan/*` | Step Plan 订阅 | `https://api.stepfun.ai/step_plan/*` |
| `/v1/messages` | 按量计费 | `https://api.stepfun.ai/v1/messages` |
| `/v1/chat/completions` | 按量计费 | `https://api.stepfun.ai/v1/chat/completions` |

两条通道是**分级体系**：Step Plan 通道消耗订阅 Credit，按量通道消耗账户余额，二者互不影响。同一个账号可能同时有订阅额度和余额，界面会把两个维度分列展示。

### 关于额度查询：两条路

**API Key 通道查不到套餐额度。** 这是实测结论：

```
GET https://api.stepfun.ai/v1/accounts              → 200  {"balance": 0.00, ...}  按量余额
GET https://api.stepfun.ai/step_plan/v1/usage       → 404
GET https://api.stepfun.ai/step_plan/v1/credits     → 404
GET https://api.stepfun.ai/step_plan/v1/subscription → 404
```

`/v1/accounts` 只给按量计费的账户余额，不含套餐 Credit 余量与到期时间。所以 step2api 提供两条额度路径，**优先走控制台那条**。

#### 路径一：控制台会话（推荐，能拿到真实套餐额度）

Step Plan 的套餐档位、Credit 余量、5 小时窗口与周窗口限额，由控制台后端提供：

```
POST https://account.stepfun.ai/api/step.openapi.devcenter.Dashboard/GetStepPlanStatus
POST https://account.stepfun.ai/api/step.openapi.devcenter.Dashboard/QueryStepPlanRateLimit
POST https://account.stepfun.ai/api/step.openapi.devcenter.Dashboard/QueryStepPlanUsages
```

接口是 gRPC-Web 风格，但**接受 JSON 请求体**，无需 protobuf 编码。需要三个请求头：

| 头 | 从哪里取 |
| --- | --- |
| `Oasis-Token` | 浏览器 Cookie 里的 `Oasis-Token` |
| `Oasis-Webid` | 浏览器 **localStorage 的 `web_id`**（注意不是 Cookie） |
| `Oasis-appID` | 固定 `20700`（Step Plan 应用；`10300` 是基础平台，用它会返回 `auth failed: oasis-token is embezzled`） |

取值步骤：

1. 登录 <https://account.stepfun.ai/>
2. F12 → Application → Cookies → 复制 `Oasis-Token` 的值
3. F12 → Console → 执行 `localStorage.getItem("web_id")`，复制结果
4. 控制台「账号 → 编辑」填入这两个值，保存即自动校验并同步

也可以随导入一起提供，或走 API：

```bash
curl -X POST http://127.0.0.1:8787/api/accounts/1/console   -H 'Content-Type: application/json'   -d '{"token":"<Oasis-Token>","webid":"<web_id>","verify":true}'
```

返回的额度长这样：

```json
{
  "plan_name": "Plus",
  "plan_status": "active",
  "credits_remaining": 1600000000,
  "credits_total": 1600000000,
  "percent_remaining": 1.0,
  "quota_expires_at": "2026-10-05T08:55:44+00:00",
  "five_hour_left_rate": null,
  "weekly_left_rate": null,
  "auto_renew": false
}
```

> 控制台凭据是**会话级**的，退出登录或改密会失效，与 API Key 相互独立。
> 转发流量始终用 API Key，只有查额度才用这套凭据。
> 5h / 周窗口为 `null` 表示该套餐不按这两种窗口限流（上游两者都返回 0），
> 而不是"剩余 0%"。

#### 路径二：API Key 通道（无控制台凭据时的兜底）

1. 按候选路径列表顺序探测 `/usage`、`/credits`、`/quota`、`/subscription`、`/plan` 等端点；
2. 第一个返回 2xx 且能解析出额度语义的路径会被记住，后续刷新复用；
3. 全部失败则回落到按量通道余额，至少保证账号可用性可见。

点账号行的「探测」可查看每个候选的实际返回。若确认了自定义端点：

```bash
export STEP2API_PLAN_QUOTA_PATHS=/usage,/credits
export STEP2API_PLAN_BASE=https://api.stepfun.ai/step_plan/v1
```

额度解析器对字段名做宽松匹配（`remaining_credits` / `credits_remaining` / `remaining` / `left` / `available` / `balance` 等），`total` 与 `used` 缺一个时可相互推导，时间戳支持秒、毫秒与 ISO 字符串。结构特殊时改 `step2api/quota.py` 里的 `_CREDIT_KEYS` / `_TOTAL_KEYS` / `_RESET_KEYS` 即可。

---

## 路由

### 会话粘性

网关从请求里按优先级挖出会话标识：

1. `X-Session-Id`、`X-Conversation-Id`、`Session-Id`、`X-Request-Id` 等请求头
2. Anthropic 格式的 `metadata.user_id` / `metadata.session_id`
3. OpenAI 格式的 `user` 字段
4. `prompt_cache_key` / `safety_identifier`
5. 兜底：`system` + 首条 user 消息的 SHA-256 指纹

命中过的会话会被记进 `sessions` 表，默认保留 6 小时（`STEP2API_AFFINITY_TTL`）。粘住的账号只要还健康、还有额度，就一直用它 —— 这对 prompt cache 命中和多轮上下文稳定性影响很大。

粘住的账号如果返回了可重试的错误，网关会把这个会话从绑定里摘掉，换账号并在新账号上重新建立粘性。

### 调度模式

`STEP2API_ROUTING_MODE`，也可以在控制台切换：

| 模式 | 行为 |
| --- | --- |
| `sticky`（默认） | 会话粘性优先，未命中时按额度充足度 × 权重 × 在途数 × 可靠性打分排序 |
| `round_robin` | 可用账号顺序轮转 |
| `least_used` | 优先选当前在途请求最少的账号 |
| `priority` | 严格按 `priority` 升序 |
| `random` | 随机打散，权重高的更容易靠前 |

### 故障转移

上游返回 `401 / 402 / 403 / 408 / 409 / 425 / 429 / 500 / 502 / 503 / 504 / 529` 时，只要还没向客户端吐出任何字节，就换下一个候选账号重试。单次请求最多尝试 `STEP2API_MAX_RETRIES` 个账号（默认 3）。

连续失败达到 `STEP2API_FAIL_THRESHOLD`（默认 5）次的账号进入冷却，冷却时长 `STEP2API_COOLDOWN`（默认 60 秒），期间不参与调度。冷却到期或被后台额度刷新确认恢复后自动复位。

### 额度感知

额度**明确为 0 或负**的账号会被摘出候选；额度**未知**（从未成功查询过）的账号保持参选 —— 不会因为查不到额度就把账号废掉。

### 手动指定

```bash
# 强制走某个账号（该请求内不轮转）
curl -H "X-Step2api-Account: 3" ...

# 单个请求走指定代理
curl -H "X-Step2api-Proxy: socks5://1.2.3.4:1080" ...

# 禁用本次请求的重试
curl -H "X-Step2api-No-Retry: 1" ...
```

响应头会带回本次实际路由结果，方便排查：

```
X-Step2api-Account: 3
X-Step2api-Proxy: us-1
X-Step2api-Attempt: 1
```

---

## 代理

### 账号级代理模式

每个账号独立配置 `proxy_mode`：

| 模式 | 行为 |
| --- | --- |
| `inherit` | 未配全局代理则直连，配了就用全局代理 |
| `direct` | 永远直连，忽略全局代理与池 |
| `dedicated` | 固定绑一个代理，专属代理不可用时回退全局或直连 |
| `pool` | 绑定一个代理池，按池策略轮转 |

### 代理池

代理池是一个可复用的代理集合。账号通过 `proxy_mode=pool` + `pool_id` 绑定池，绑上之后有**两层开关**决定出口怎么选。

第一层是池的 `affinity`：

| affinity | 行为 |
| --- | --- |
| `sticky`（默认） | 每个账号在池内固定落一个代理。**账号之间会摊开**到池内不同代理 —— 多条订阅的出口 IP 被分散，而每个账号自己的出口稳定不变，不给上游风控制造抖动 |
| `rotate` | 每次解析都重新选，按池策略轮转，同一账号的出口会不断变化 |

第二层是账号自己的 `proxy_rotation` 开关：关掉它就固定使用池内第一个可用代理（多个账号共用同一个出口），绕过 affinity 逻辑。

`rotate` 模式下按池策略轮转：

| 策略 | 行为 |
| --- | --- |
| `round_robin` | 池内顺序轮转，均摊使用 |
| `random` | 随机取一个可用代理 |
| `least_used` | 取当前在途请求最少的代理 |
| `lowest_latency` | 取最近一次健康检查延迟最低的代理 |

在途数与延迟记录在共享的指标登记处，按代理 URL 聚合，因此 `least_used` / `lowest_latency` 看到的是全局负载而不是单个池的局部视图。

池内没有可用代理时，由 `fallback_direct` 决定是直连回退还是直接报错。

「立即轮转」按钮会清空该池所有会话的代理绑定，让下一次请求重新选。

### 出口 IP 应该怎么选

- 怕上游风控 → `affinity=sticky`（默认），每个账号一个固定出口
- 想分散多账号的出口 → `affinity=sticky`，把代理加够，账号会自动摊开
- 想压测或避开单 IP 限速 → `affinity=rotate` + `least_used`
- 只想让所有账号走同一个出口 → 账号 `proxy_rotation=false`

### 协议支持

`http://`、`https://`、`socks5://`、`socks5h://`、`socks4://`、`socks4a://`。

裸露的 `host:port` 会按 `http://host:port` 处理，`user:pass@host:port` 也接受。代理健康检查默认打上游余额接口 —— 因为该接口对无效 key 返回 401/403，能连上就说明链路已经到上游了。

### 代理轮转策略（全局默认）

没有绑定池的账号（`inherit` 模式）使用 `STEP2API_PROXY_STRATEGY` 指定的全局策略；绑定池的账号以池自己的 `strategy` 为准。

---

## 配置

所有配置都可以用 `STEP2API_` 前缀的环境变量覆盖，也可以写在 `.env` 里由你的进程管理器注入。

### 服务

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `STEP2API_HOST` | `127.0.0.1` | 监听地址。要对外暴露请同时设置 `ADMIN_TOKEN` |
| `STEP2API_PORT` | `8787` | 监听端口 |
| `STEP2API_DATA_DIR` | `./data` | SQLite 与密钥文件目录 |
| `STEP2API_CURRENCY` | `USD` | 金额维度展示的币种符号 |

### 安全

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `STEP2API_ADMIN_TOKEN` | 无 | 管理接口令牌。不设置则 `/api/*` 不鉴权 |
| `STEP2API_SECRET` | 自动生成 | 落库加密主密钥。不设置则生成并写入 `data_dir/.secret_key` |
| `STEP2API_GATEWAY_TOKENS` | 无 | 下游调用方令牌，逗号分隔。不设置则不校验 |
| `STEP2API_ALLOW_CN_SITE` | `false` | 是否放开国内站导入限制 |

### 上游

| 变量 | 默认值 |
| --- | --- |
| `STEP2API_UPSTREAM_BASE` | `https://api.stepfun.ai` |
| `STEP2API_PLAN_BASE` | `https://api.stepfun.ai/step_plan/v1` |
| `STEP2API_BALANCE_PATH` | `/v1/accounts` |
| `STEP2API_PLAN_QUOTA_PATHS` | `/usage,/credits,/quota,/subscription,/plan,/me,/account,/accounts,/balance` |
| `STEP2API_REQUEST_TIMEOUT` | `300` 秒 |
| `STEP2API_CONNECT_TIMEOUT` | `15` 秒 |
| `STEP2API_PROBE_TIMEOUT` | `20` 秒 |
| `STEP2API_CONSOLE_BASE` | `https://account.stepfun.ai` | 控制台额度接口基址 |
| `STEP2API_CONSOLE_APP_ID` | `20700` | 控制台应用 ID，Step Plan 必须为 20700 |
| `STEP2API_CONSOLE_SYNC` | `true` | 是否启用控制台额度同步 |

### 路由

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `STEP2API_ROUTING_MODE` | `sticky` | `sticky` / `round_robin` / `least_used` / `priority` / `random` |
| `STEP2API_AFFINITY_TTL` | `21600` | 粘性会话保留秒数（6 小时） |
| `STEP2API_COOLDOWN` | `60` | 失败账号冷却秒数 |
| `STEP2API_MAX_RETRIES` | `3` | 单请求最多尝试账号数 |
| `STEP2API_FAIL_THRESHOLD` | `5` | 连续失败几次后进入冷却 |
| `STEP2API_DEFAULT_MAX_CONCURRENCY` | `8` | 账号默认并发上限 |

### 额度刷新

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `STEP2API_REFRESH_INTERVAL` | `300` | 后台自动刷新间隔秒数 |
| `STEP2API_LOW_QUOTA_RATIO` | `0.1` | 低于该剩余比例触发告警 |

### 代理

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `STEP2API_GLOBAL_PROXY` | 无 | 全局默认代理 |
| `STEP2API_PROXY_STRATEGY` | `round_robin` | 全局轮转策略 |
| `STEP2API_PROXY_CHECK_URL` | 上游余额接口 | 代理健康检查目标 |

### 其它

| 变量 | 默认值 |
| --- | --- |
| `STEP2API_IMPORT_CONCURRENCY` | `6` |
| `STEP2API_LOG_REQUESTS` | `true` |
| `STEP2API_LOG_RETENTION` | `5000` |

---

## 命令行

```bash
step2api serve                       # 启动服务
step2api import keys.txt --verify     # 从文件导入并立即校验额度
step2api import keys.txt --dry-run    # 只解析，不落库
step2api probe-endpoint               # 用库中第一个账号探测额度端点
step2api probe-endpoint --key sk-...  # 直接指定 Key 探测
step2api export --out accounts.json   # 导出账号明细（不含 Key 明文）
```

---

## 管理 API

全部挂在 `/api` 下，配置了 `ADMIN_TOKEN` 时要求 `X-Admin-Token` 请求头或 Bearer 令牌。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/health` | 健康检查（不需要鉴权） |
| `GET` | `/api/stats` | 汇总统计：账号状态、Credit 总量、24 小时用量 |
| `GET` | `/api/accounts` | 账号列表（含额度、Plan 时长、状态） |
| `POST` | `/api/accounts` | 新建账号 |
| `PATCH` | `/api/accounts/{id}` | 修改账号 |
| `DELETE` | `/api/accounts/{id}` | 删除账号 |
| `POST` | `/api/accounts/{id}/refresh` | 刷新该账号额度 |
| `POST` | `/api/accounts/{id}/probe-endpoint` | 探测 Step Plan 额度端点 |
| `POST` | `/api/accounts/{id}/console` | 配置控制台凭据并校验（查真实套餐额度） |
| `DELETE` | `/api/accounts/{id}/console` | 清除控制台凭据 |
| `POST` | `/api/accounts/{id}/reset` | 清除冷却与失败计数 |
| `POST` | `/api/accounts/{id}/toggle` | 启用 / 禁用 |
| `POST` | `/api/import/preview` | 解析预览（不落库） |
| `POST` | `/api/import` | 批量导入 |
| `GET` `POST` `PATCH` `DELETE` | `/api/proxies[/{id}]` | 代理增删改查 |
| `POST` | `/api/proxies/bulk` | 批量导入代理 |
| `POST` | `/api/proxies/check` | 代理健康检查 |
| `GET` `POST` `PATCH` `DELETE` | `/api/pools[/{id}]` | 代理池增删改查 |
| `POST` | `/api/pools/{id}/rotate` | 清空该池的会话代理绑定 |
| `GET` | `/api/sessions` | 粘性会话列表 |
| `DELETE` | `/api/sessions/{key}` | 清除单个会话绑定 |
| `GET` `DELETE` | `/api/logs` | 请求日志 |
| `GET` | `/api/settings` | 当前配置（密钥类字段脱敏） |
| `POST` | `/api/settings/routing` | 修改路由与代理设置 |
| `POST` | `/api/refresh-all` | 刷新全部账号额度 |
| `GET` | `/api/export/claude-code` | 生成客户端配置片段 |

交互式文档在 <http://127.0.0.1:8787/docs>。

---

## 架构

```
step2api/
├── config.py      全局配置（环境变量 + 默认值 + 站点判定）
├── crypto.py      Fernet 加密，API Key 与代理密码落库保护
├── store.py       SQLite 存储层（账号 / 代理 / 池 / 会话 / 日志）
├── proxy.py       代理规范化、轮转池、健康检查
├── quota.py       额度与余额探测、响应解析（分级体系双通道）
├── router.py      账号选择、粘性会话、代理绑定、并发闸门
├── gateway.py     上游转发、流式透传、故障转移
├── scheduler.py   后台额度刷新、会话清理、日志裁剪
├── api.py         REST 管理接口、导入解析、鉴权
├── app.py         FastAPI 装配与路由分发
├── static/        Web 控制台（无构建步骤）
└── __main__.py    命令行入口
```

一条请求的完整路径：

```
客户端
  │  Authorization: Bearer <网关令牌>
  ▼
app.py            路径分发 → 判断通道（plan / api）
  ▼
gateway.py        读 body → 提取会话键与模型 → 询问路由器
  ▼
router.py         粘性命中？→ 排序候选 → 解析每个账号的代理 → 返回尝试序列
  ▼
gateway.py        按序列逐个尝试，带账号自己的 Key 与代理转发，SSE 流式回传
  ▼
store.py          记录结果：额度缓存、冷却、粘性绑定、请求日志
```

---

## 数据与安全

- API Key、代理密码在 SQLite 里是 Fernet 密文（`enc:v1:` 前缀），主密钥来自 `STEP2API_SECRET` 或 `data_dir/.secret_key`
- 前端与 API 响应只返回 key 指纹（`sk-abc…1234`），不返回明文
- 代理 URL 在列表与日志里做账号密码脱敏（`http://user:***@host:port`）
- `.gitignore` 已排除 `data/`、`*.db`、`.secret_key`，不会误提交
- 默认只监听 `127.0.0.1`。要对外提供服务，务必同时设置 `STEP2API_ADMIN_TOKEN` 与 `STEP2API_GATEWAY_TOKENS`
- `step2api export` 导出的明细同样不含 Key 明文

---

## 常见问题

**Q：额度一直显示"未知"？**

先看是否配了控制台凭据。不配的话只能走 API Key 通道，而**该通道没有套餐额度接口**（`/step_plan/v1/*` 下的候选路径全部 404），只能回落到按量余额。按上文「路径一」配好 `Oasis-Token` 与 `web_id` 就能看到真实的套餐档位、Credit 余量与到期时间。

**Q：控制台凭据报 "oasis-token is embezzled"？**

这是 `Oasis-appID` 用错或 `Oasis-Webid` 取错位置。`appID` 必须是 `20700`（Step Plan 应用），`Webid` 要从 `localStorage.getItem("web_id")` 取，**不是** Cookie 里的 `Oasis-Webid`。

**Q：5 小时窗口 / 周窗口显示为空？**

正常。上游对不适用的窗口返回 `0`，本服务将其识别为"不适用"而非"剩余 0%"。目前 Credit 计量型套餐就属于这种，额度以 Credit 桶表示。

**Q：为什么某个账号不接请求了？**

按顺序检查：是否被手动禁用、是否处于冷却（连续失败 5 次触发）、额度是否明确耗尽、绑定的代理池是否为空且禁止直连回退。控制台账号行的状态列会给出具体原因，`last_error` 里是上游返回的原始错误。

**Q：粘性会话没有生效？**

检查请求里有没有携带可识别的会话标识。Claude Code 会带 `metadata.user_id`，一般的 OpenAI SDK 调用不会带任何标识 —— 这种情况网关会用 system + 首条 user 消息做指纹，只有对话开头完全一致才会命中同一个账号。需要强粘性就给请求加 `X-Session-Id` 头。

**Q：代理池轮转没有变化？**

池的 `affinity` 默认是 `sticky`，每个账号在池内的落点是固定的，所以单独盯着一个账号看会以为没轮转。`sticky` 的轮转发生在**账号维度**：同一个池挂 N 个账号，它们会被摊到不同代理上。想让单个账号的出口每次都换，把池改成 `rotate`；想让所有账号走同一个出口，把账号的「池内允许轮转」关掉。

**Q：能同时对接国内站吗？**

不能。本项目设计上只支持国外站，导入含 `stepfun.com` 的内容会被拒绝。`STEP2API_ALLOW_CN_SITE=true` 可以关掉这个限制，但两条通道的路径映射是按国际站的 `step_plan/v1` 结构写的，国内站需要另行调整。

**Q：网关重启后粘性会话还在吗？**

在。会话绑定存在 SQLite 里，重启后继续有效，直到过期或被清除。

---

## 开发

```bash
uv venv
uv pip install -e ".[dev]"
uv run pytest -q          # 68 个单元测试
```

测试覆盖加密、代理解析与轮转、额度响应解析、路由决策、粘性会话、代理绑定、导入解析、SSE 用量提取。

### 端到端冒烟测试

仓库自带一个假上游 + 三个最小 HTTP 代理，可以在不碰真实上游的情况下验证全链路：

```bash
# 终端 1：假上游（含代理）
python scripts/fake_upstream.py

# 终端 2：网关指向假上游
#   Linux / macOS
STEP2API_DATA_DIR=/tmp/s2a \
STEP2API_UPSTREAM_BASE=http://127.0.0.1:8899 \
STEP2API_PLAN_BASE=http://127.0.0.1:8899/step_plan/v1 \
python -m step2api serve --port 8790

# 终端 3：跑 58 项端到端断言
python scripts/smoke_test.py
```

冒烟测试覆盖：批量导入与国内站拒绝、订阅额度解析、余额通道、跨账号故障转移、粘性路由、池内轮转与 affinity 语义、直连/专属代理、SSE 流式透传、请求日志与用量统计、凭据落库加密、冷却与复位、代理健康检查、静态资源、端点探测。脚本开头会清空网关状态，可重复运行。

Windows 下如果控制台报 `UnicodeEncodeError`，先设 `PYTHONUTF8=1`。

---

## License

MIT

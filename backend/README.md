# 后端服务（Gateway）

FastAPI 网关。ASGI 入口：`backend.app.gateway.app:app`。

## 最短路径

必须在**仓库根**启动，不能 `cd backend`。

```bash
cd /path/to/myflow
uv run --env-file .env --with uvicorn \
  uvicorn backend.app.gateway.app:app --host 127.0.0.1 --port 8001
```

- `--env-file` 是 **uv** 的参数，必须写在 `uvicorn` 之前。代码不会自己读 `.env`。
- 依赖里没有 uvicorn，所以用 `--with uvicorn`。要热重载再加 `--reload`。
- 本机绑 `127.0.0.1`。容器里才绑 `0.0.0.0`。
- **Windows + Postgres checkpointer**：uvicorn 默认 `ProactorEventLoop`，psycopg 异步连不上。
  启动时加上
  `--loop backend.app.gateway.selector_loop:selector_loop_factory`
  （这里的 attr 必须是零参可调用对象 / loop 类，不是 `(use_subprocess)->class`）。
  只改 `asyncio.set_event_loop_policy` **无效**（uvicorn 会绕过 policy）。

起来之后：`http://127.0.0.1:8001/docs`。

开发热重载：

```bash
uv run --env-file .env --with uvicorn \
  uvicorn backend.app.gateway.app:app --reload --host 127.0.0.1 --port 8001
```

Windows（Postgres）示例：

```bash
uv run --env-file .env --with uvicorn \
  uvicorn backend.app.gateway.app:app --host 127.0.0.1 --port 8001 \
  --loop backend.app.gateway.selector_loop:selector_loop_factory
```

`--workers` **保持 1**。当前 `StreamBridge` 是进程内内存实现，多 worker 时创建 run 和订阅 SSE 可能不在同一进程。环境变量 `GATEWAY_WORKERS` 必须和真实进程数一致，否则启动回收会把别人的 inflight run 标成 error。

## 配置

`AppConfig.resolve_config_path()` 顺序：

1. 代码显式传入的路径
2. `MYFLOW_CONFIG_PATH`
3. 项目根下的 `config.yaml`（`MYFLOW_PROJECT_ROOT` 或当前工作目录）

`config.yaml` 里以 `$` 开头的值从环境变量展开，缺了会直接报错（例如 `api_key: $DEEPSEEK_API_KEY`）。

`lifespan` 会建数据库引擎、thread_meta / run_store / event_store、checkpointer，并把 `RunServices` 挂到 `app.state`。默认 `database` / `run_events` / `checkpointer` 都可以是 memory：重启即失忆。

沙箱段见 `config.yaml` 的 `sandbox`。本机 Local provider 默认不开宿主 bash；可信环境可设 `allow_host_bash: true`。

### 模型与推理等级（输入框里的两个选择）

**列表来自厂商**：`GET /api/models` 会带着 `models[].api_key` 去调每个 provider 的
`GET {base_url}/models`（OpenAI 兼容，DeepSeek 也支持），把返回的 id 映射回 `models[]`：

- 和 `models[].model` 相同的 id → 沿用该条目的 `name`（`context.model_name` 用它）；
- 厂商新报出来的 id → `<name>@<vendor-id>`，网关复用该条目的 use/base_url/api_key，
  只把 provider 侧模型名换掉（`model_overrides={"model": vendor_id}`）；
- 厂商不可达（没网/没 key/限流）→ 该 provider 回退到配置里的那一条，
  原因写进响应的 `vendor_errors`，输入框不会空掉；结果缓存 5 分钟。

```yaml
models:
  - name: deepseek-v4-flash        # 唯一标识：/api/models 的 name，也是 context.model_name
    display_name: Flash            # 只给人看
    provider: DeepSeek             # 列表上方那行小字；不写就从 use 推导（langchain_deepseek → DeepSeek）
    supports_reasoning_effort: true
    reasoning_efforts:             # 显示名原样用（不改大小写）；不写就从厂商集成自带的 model profile 取 id
      - id: low
        label: Low
      - id: medium
        label: Medium
        default: true              # 标了才会被预选；不标 = 输入框里的 “Off”
```

- 推理等级**没有标准的厂商 HTTP 端点**：先用配置里的 `reasoning_efforts`（只写 id 与显示名，
  不写解释性描述），没写就用厂商集成自带的 model profile（`reasoning_effort_levels`，如
  `gpt-5` 的 `minimal/low/medium/high`，id 原样显示），都没有则这一行不显示；
- 输入框的等级列表第一项固定是 `Off`（= 不下发 `reasoning_effort`，用 provider 默认），
  所以配置里不需要（也不应该）列一个"没有等级"的候选项；
- 响应字段：`models[]`（`name/model/provider/display_name/reasoning_efforts/
  default_reasoning_effort/source/available`）、`default_model`（`agents.default_model`，
  缺省 `models[0]`）、`default_reasoning_effort`、`vendor_errors`；
- 前端把选择塞进 `runs.create` 的 `context.model_name` / `context.reasoning_effort`；
- 网关校验：`<name>` 不在 `models[]` 里、或 `<name>@<vendor-id>` 的 vendor id 非法 →
  400 `model_not_allowed`；等级不是该模型公布的候选 → 422 `invalid_run_request`；
- 装配时 `assemble_agent` 再做一次协商（等级过期的旧客户端回退 provider 默认），
  最终由 `create_chat_model(..., reasoning_effort=…)` 传给模型；模型不支持就整体丢弃；
- `provider` / `reasoning_efforts` 是**元数据**：不会被当成模型构造参数转发出去。

### 持久记忆（M10 §2）

`memory.enabled: true` 后，lifespan 按 `database.backend` 打开 LangGraph `BaseStore`
（memory / sqlite / postgres 同一套选择），并按用户 namespace 存画像与 facts：

| 数据 | namespace | key |
|---|---|---|
| 画像 | `users/<user_id>/profile` | `current` |
| 所有记录 | `users/<user_id>/records` | `kind:id` |
| 维护统计 | `users/<user_id>/stats` | `current` |

画像在 `before_agent` 以隐藏 HumanMessage（id `__user_profile__`）注入；长尾事实只通过
只读工具 `memory_search` 检索。写入走后台 debounce 抽取 + 确定性 proposal 门，模型不能
声明"用户确认了"。HTTP 面是 `/api/memory`（profile / search / items / facts / conflicts /
export / maintenance）。`memory.backend_class` 是启动项，换后端要重启并重置 manager。

### PII / 敏感信息边界

`pii.enabled: true`（默认）时，`start_run` 在 normalize 和持久化之前替换用户
HumanMessage 中的高敏感文本；模型输出写回 state 前也会替换。`state` /
`checkpoint` / `run record` 只保存 run 级 handle。handle 到原值的映射只在进程内存里（`runtime.context`），不写 state、
checkpoint 或日志；进程重启后旧 handle 不可解析，工具必须 fail closed。

- memory 只读取进一步的 memory view：handle 和字面 PII 都变成 `[REDACTED]`，
  不写进 facts，也不作为 evidence；
- 用户画像白名单字段允许持久化，不做通用 PII 替换；
- 所有工具在调用前由 `PIIMiddleware` 还原 handle；无法解析的 handle 会变成
  可恢复的工具错误，不会把 handle 当普通字符串发给外部服务；
- **tool result 不在本策略保证范围内**：`ToolMessage` 中的敏感信息由工具自身负责；
- 多模态、上传文件和 artifact 内容暂不覆盖，由对应工具/上传层负责。

**MCP 与技能走第二份文件**：`extensions_config.json`（模板 `extensions_config.example.json`，已 gitignored）。
它**由 API 读写**，所以整份 `config.yaml` 不能变成 API 可写。路径优先级和 `config.yaml` 一样，环境变量是
`MYFLOW_EXTENSIONS_CONFIG_PATH`：显式路径或环境变量指向的文件不存在会直接报错，
只有"项目根下搜不到"才算没配扩展。所有读-改-写走 `agentkit.config.update_extensions_config()`：
进程内 `threading.Lock` + 跨进程文件咨询锁 + 原子写，技能路由和 MCP 路由共用同一把锁。

### MCP 服务器

`extensions_config.json` 的 `mcpServers` 是 M2 工具列表的**第三源**（配置工具 → 内置 → MCP，按 `tool.name` 去重）。
适配器是可选依赖：`uv sync --extra mcp`（或 `uv pip install langchain-mcp-adapters`），没装时 MCP 一台都不加载，
其余功能照常。工具**懒加载**：第一次 `collect_tools` 时连一次，之后按 `extensions_config.json` 的
内容签名失效；`POST /api/mcp/cache/reset` 可以强制本进程重来。

| 配置键 | 作用 |
|---|---|
| `type` / `transport` | `stdio`（默认）· `sse` · `http`；两个都写时 `type` 赢 |
| `command` / `args` / `env` | stdio 用。**API 启停时只接受 `{npx, uvx}` 这样的裸可执行名**（§5.7，纵深防御） |
| `url` / `headers` | sse / http 用。静态 header 出现两种拼写会被拒 |
| `tool_name_prefix` | 默认 true：工具名加上 `<server>_`，源路由仍然只看 metadata |
| `session_init_timeout` / `tool_call_timeout` | 默认 60s；`session_init_timeout: null` = 不限 |

凭据四层（**先注册的在外层，后注册的赢**）：静态 `headers` < `oauth` < `user_auth` < `headers_from_context`
（`context.secrets` 里的键 → header）。**fail-closed 是默认**：映射里没有当前用户就报错，只点名缺失的键；
想放行缺值要显式写 `on_missing: passthrough`。stdio 上这三层跳过（用 `env`）。
`mcpInterceptors`（运维文件专属，会 import 代码）永远最靠近传输层。

stdio 服务器是**有状态子进程**：会话按 `(server, user_id:thread_id)` 池化，池满 LRU 驱逐；
明确的 `Connection closed` 只驱逐那一个会话，**失败绝不自动重放**。停止/启用某台服务器后，
打一次 `POST /api/mcp/cache/reset` 让本进程立刻换掉旧会话。

技能目录默认是项目根下的 `skills/{public,custom}/<name>/SKILL.md`（`config.yaml` 的 `skills.path` 可改）。
模型看到的是系统提示词尾部的 `<available_skills>` 索引（每技能一行 `name [category]: 截断描述`，
描述截断到 `index_description_max_chars`；根目录 `skills.container_path` 与"目录名即技能名、
SOP 固定 `SKILL.md`"的约定在块前说一次，路径可推），
正文一律按需：`read_file` 读 SKILL.md、`search_and_load_skills` 按任务意图检索加载，
或 `/skill-name` 注入 —— 没有"全量注入"模式开关。
已激活的技能由 `<active_skills>`（在 `<durable_context_data>` 里）投影：名字、描述、目录，
外加**核心 SOP 正文**。正文不进 state，每次投影按 `skill_context` 台账里的 path 现读，
预算见 `skills.active_context`；正文还在消息里时默认不重复写（压缩之后它才是唯一副本）。
`search_and_load_skills` 是两段式（本地 BM25 召回 `skills.search.candidate_k` → 可选精排
`top_k`），默认只有本地通道；`skills.search.embedding` / `rerank` 不填地址就不发请求，
失败也只降级（退回 BM25 / 融合顺序）并在 `retrieval.warnings` 里说明。加载即激活：
结果里的 `path` 和 `read_file` 读 SKILL.md 走同一本 `skill_context` 台账。
技能是**两层开关**：`skills.source: git` 时全局开关写 PostgreSQL 的 `skill_settings`
（按内部 `skill_id`，admin），个人偏好仍写 `skills_prefs.json` 的
`{user_id: {skill_id: bool}}`（任何登录用户），有效状态 = 两者相与。旧的
`extensions_config.json` 技能开关与按名字的偏好用 `backend/scripts/migrate_skill_state.py` 迁移。

## 认证（M9 §2）

身份只有一个权威来源：**AuthMiddleware 写的 `request.state.auth`**。
客户端在 body 里塞的 `user_id` / `owner_id` / `auth_user` / `auth_user_id`
在 `start_run()` 里被**删掉**（不是"校验后使用"），下游一律用
`agentkit.runtime.user_context` 的优先级链解析存储桶。

### 两种模式

| 模式 | 配置 | 行为 |
|---|---|---|
| 单人演示 | `auth.enabled: false` | 固定用户 `default`、全能力，数据继续进 `users/default/`。`GATEWAY_WORKERS>1` 时**拒绝启动** |
| 多用户 | `auth.enabled: true` + `auth.jwt_secret`（≥32 字节） | 白名单外一律 401；缺密钥或缺 admin 时启动/接口会明确报错 |

Demo 模式**故意**沿用 `default` 这个桶名（而不是 `auth-disabled`）：
M4 时代的数据都在 `users/default/`，换名字等于让老用户"数据突然没了"。

### 环境变量

| 变量 | 作用 |
|---|---|
| `AUTH_JWT_SECRET` | HS256 密钥。**改它 = 所有会话立刻失效**（有意为之） |
| `MYFLOW_INTERNAL_AUTH_TOKEN` | 通道 / 调度器代表用户的共享密钥，与 `auth.internal_tokens` 合并；写 `[new, old]` 即可不停机轮换 |
| `GATEWAY_CORS_ORIGINS` | 逗号分隔。`CORSMiddleware` 与 `CSRFMiddleware` **读同一个变量** |
| `GATEWAY_ENABLE_DOCS=false` | 把 `/docs`、`/redoc`、`/openapi.json` 从公开白名单拿掉（生产默认关） |
| `AUTH_TRUSTED_PROXIES` | 只有 peer 落在这里时才信 `X-Real-IP`；**永远不看 `X-Forwarded-For`** |
| `GATEWAY_WORKERS` | 必须等于真实进程数；`>1` 且认证关闭 → 拒绝启动 |

### 错误码：能照着做，而不是"错了"

默认 `auth.local.specific_errors: true`。每个失败都有自己的 `code`，detail 说清**下一步做什么**：

| 情形 | HTTP | `code` | detail 里有什么 |
|---|---|---|---|
| 邮箱形状不对 | 422 | `invalid_email` | 哪条规则没过（缺 @、域名没有点、有空格…），并回显你刚敲的值 |
| 用户名形状不对 | 422 | `invalid_username` | 是哪一条（3-32 位 / 字符集 / 首尾 / 保留字） |
| 用户名被占 | 409 | `username_taken` | 是哪个用户名，让你换一个 |
| 口令太短 | 422 | `password_too_short` | 最少 8 字符 + 实际几位 |
| 口令太长 | 422 | `password_too_long` | 上限 72 **字节**（UTF-8）+ 实际字节数 |
| 邮箱已注册 | 409 | `user_already_exists` | 是哪个邮箱，并提示直接登录 |
| 自助注册关闭 | 403 | `registration_closed` | 让管理员建号 |
| 已有 admin 再走 setup | 409 | `admin_already_exists` | 提示去登录（**不点名**现有 admin，这条路由公开） |
| 没这个账号 | 401 | `account_not_found` | 回显你敲的**标识**（邮箱或用户名），提示先注册。不说是哪一种没注册 |
| 密码不对 | 401 | `wrong_password` | 只说不对，**永不回显口令** |
| 账号被禁用 / 在回收期 | 403 | `account_disabled` / `account_deleted` | 找管理员恢复。**只在口令正确时才给这个答案** |
| 失败次数超限 | 429 | `login_rate_limited` | 剩余秒数 + 响应头 `Retry-After` |
| 改密时当前密码错 | 401 | `invalid_credentials` | 明确指出是 current password 的问题 |
| **服务端**读不出认证配置 | 503 | `auth_unavailable` | 让用户找管理员。**不是**"你没登录"——用户重登一百次也还是这个错 |

两条不变量：**明文口令永不回显**（也不进日志、不进库）；
**账号状态（禁用 / 回收期）只在口令正确之后才透露**——不知道口令的人不该读到"这个账号存在且被禁用了"。

想要"不透露某个邮箱（或用户名）是否注册"就把 `auth.local.specific_errors` 设成 `false`：
登录的四种失败（没这个账号 / 密码错 / 被禁用 / 回收期）会收敛成同一个 401 `invalid_credentials`
和同一句 detail。代价是用户不知道为什么失败。

### 用户名：第二个登录标识

`users.username` 不是"显示名"，它是一个**登录标识**（§2.4）：

- **形状**：3-32 字符，小写字母/数字/下划线/点/连字符，首尾必须是字母或数字；
  `default` / `anonymous` / `auth-disabled` / `system` / `internal` 是保留字。
- **大小写不敏感**：`Alice` 存成 `alice`，登录时敲 `ADMIN` 也进得去。
- **两个标识各自唯一**：邮箱撞车 409 `user_already_exists`，用户名撞车 409
  `username_taken`——要改的字段不同，code 也不同。
- **改用户名不换桶**：桶名只来自 `users.id`（§2.2）。这条比改邮箱更容易写错，
  因为用户名看起来"本来就是 id"。
- **旧库自动回填**：`0003_users_username` 从邮箱 local part 派生（撞名加 `-2`），
  再建唯一索引、置 NOT NULL。升级前不用手工准备数据。

改名的入口是 **`PATCH /api/v1/auth/me`**：body `{"username": "..."}` → `UserResponse`。
仅会话（PAT → 403 `session_required`）、**不动令牌**（别的设备不用重新登录）、
**只改用户名**（多给一个字段就是 422 `invalid_request`）。
`needs_setup` 的用户会被 403 `setup_required` 拦下——他该先去改密码。

**401 ≠ 会话失效。** 登录 / 注册 / 初始化 / 改密上的 401、403 是"这次提交不对"，
前端不该把它们当成"会话过期"去跳登录页（见 `frontend/README.md`）；
"服务端自己读不出配置"则走 503 `auth_unavailable`，不冒充未认证。

**浏览器里剩一枚旧 cookie**（换过 `jwt_secret`、`token_version` 变了、库重建过）时，
公开路由照常工作、受保护路由照常 401 —— 用户不会被永久锁在登录页外面。
这条有回归测试（`test_a_stale_cookie_cannot_lock_the_auth_pages`），改动认证门时别弄丢它。

### 会话与令牌

- JWT 只在 HttpOnly cookie `access_token` 里，**响应体里永远没有 token 字符串**。
  登录响应只有 `expires_in` 与 `needs_setup`。
- 撤销只有两个机制：`users.token_version`（改密 / 重置 / 软删 +1）与
  `disabled` / `deleted_at`（每次鉴权都查库）。**没有 refresh token，登出不是全局登出**——
  它只清当前浏览器，旧 token 重放仍然有效。
- PAT 与 session 共享 `token_version`：改密会把 PAT 一起废掉。
- cookie 只在 HTTPS / 本机 localhost / 显式 `auth.persist_cookies_over_http` 时持久化；
  公网 HTTP 下降级成 session cookie。CSRF cookie 与 access cookie 的 `max_age` 成对更新。

### 内部调用（通道 / 调度器）

```
X-MyFlow-Internal-Token: <共享密钥>
X-MyFlow-Owner-User-Id:  <owner 的存储 ID>
```

三关：令牌相等 → owner 存在且启用 → 该 owner 绑定过这个通道（绑定表属 9D，
未注册时只打一次 warning）。**只有 AuthMiddleware 会读这只 owner 头**，
其它代码路径读到都必须当普通不可信输入丢掉。

> **已知边界**：把 `MYFLOW_INTERNAL_AUTH_TOKEN` 下发到终端（移动 App、浏览器）
> 等于允许伪造"代表某用户"。密钥只应放在 Gateway 本机 / 通道 worker / 调度器。

### 首次初始化与救援

- 没有 admin 时服务**不**自动建号，只提示访问 `/setup`（`GET /api/v1/auth/setup-status`）。
- `POST /api/v1/auth/initialize` 只在零 admin 时可用，并发输家拿 409 `admin_already_exists`。
- 忘记密码：`uv run python -m backend.app.gateway.auth.reset_admin [--email ...] [--credentials-file ...]`
  → 随机口令、`token_version += 1`、`needs_setup=true`，凭据写进 `0600` 文件，
  **stdout 只打路径不打明文**。

### 存储分桶迁移

```bash
# 先看一眼 N 条 thread 归入谁（默认就是 dry-run）
uv run python -m backend.scripts.migrate_user_buckets
# 真跑（要求：Gateway 已停 / 没有活跃 run / 库里已有 admin）
uv run python -m backend.scripts.migrate_user_buckets --apply
```

旧扁平布局 `{base}/threads/{tid}/...` 全部归**第一个 admin**；目标已存在就跳过、
**绝不覆盖**；跨盘时先校验条目数与总字节数再删源。旧共享 `{base}/agents/` 只报告不搬。

### PAT 的现状

PAT 的**校验链路**已经就位（`Authorization: Bearer dfp_...`、固定 scope 菜单、
永不带 admin、到期即 401、`last_used_at` 按分钟节流、与 `token_version` 联动），
但**签发 / 列表 / 吊销的 HTTP 路由按设计推到 W4**（M9 §2.12"不做"）：
打这些路径是 404。服务层的 `AuthService.create_pat / list_pats / revoke_pat` 已经可用。

### 与设计文档的两处偏差

1. `users.system_role` **没有**唯一索引。设计里提过"admin 部分唯一索引"，
   但 `UNIQUE(system_role)` 会禁止第二个 admin，而 §2.13 又要求"不能把最后一个 admin 降级"。
   并发初始化改用条件插入（`WHERE NOT EXISTS (admin)`），降级/删除用条件写。
2. `X-MyFlow-Owner-User-Id` 的第三关（通道绑定）留了
   `register_owner_binding_check()` 这个接缝给 9D，未注册时只打一次 warning。

## 主要路由

都挂在 `/api`。约定：**router 只声明资源段**（`/threads`、`/models`、`/uploads`…），
`/api` 统一在 `create_app()` 的 `include_router(..., prefix="/api")` 里加；
少加一处就是整组接口 404（`test_m3_routes::test_route_inventory_matches_m3` 会当场抓住）。


| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/api/threads` | 建会话 |
| `GET` | `/api/threads/{id}` | 读会话 |
| `POST` | `/api/threads/search` | 列会话 |
| `POST` | `/api/threads/{id}/runs/stream` | 创建 run 并 SSE |
| `POST` | `/api/threads/{id}/runs/wait` | 创建 run 并阻塞到结束 |
| `GET` | `/api/threads/{id}/runs/{run_id}/join` | 旁观已有 run（断开不取消） |
| `POST` | `/api/threads/{id}/runs/{run_id}/cancel` | 取消 |
| `GET` | `/api/models` | 模型目录（`models[]` + `reasoning_efforts` + `default_model`），输入框的模型/推理等级下拉读它 |
| `GET` | `/api/models/{model_name}` | 单个模型（`models:read`） |
| `POST` | `/api/threads/{id}/uploads` | 上传 |
| `GET` | `/api/threads/{id}/artifacts/...` | 产物 |
| `GET`/`PATCH` | `/api/mcp/config` | 读 MCP 配置（打码成 `****`）/ 启停一台已有服务器（`admin`） |
| `POST` | `/api/mcp/cache/reset` | 清本进程的 MCP 工具缓存（`admin`） |
| `GET` | `/api/skills` | 技能目录，`enabled` 是**有效状态**（`skills:read`；`?scope=global` 仅 `admin`） |
| `GET` | `/api/skills/mine` | 当前用户的私有草稿列表（`skills:read`） |
| `POST` | `/api/skills` | 新建私有草稿（`skills:write`） |
| `POST` | `/api/skills/{skill_id}/draft/save` | 保存草稿版本并刷新目录快照（`skills:write`） |
| `POST` | `/api/skills/{skill_id}/submit` / `withdraw` | 提交/撤回审核（`skills:write`） |
| `DELETE` | `/api/skills/{skill_id}` | 删除私有草稿并刷新目录快照（`skills:write`） |
| `PUT` | `/api/skills/{skill_id}/preference` | 开关"我用不用"（任何登录用户，只写 `skills_prefs.json`，按 `skill_id`） |
| `PATCH` | `/api/skills/{skill_id}` | 全局开关（`admin`，`skills.source: git` 时写 PostgreSQL 的 `skill_settings`） |
| `POST` | `/api/skills/reload` | 重建本进程的目录快照与挂载树（`admin`） |
| `GET` | `/api/admin/skills/reviews` | 审核队列（`admin`） |
| `POST` | `/api/admin/skills/reviews/{submission_id}/approve` / `reject` | 批准/驳回提交（`admin`） |
| `POST` | `/api/admin/skills/{skill_id}/unpublish` / `activate` | 下架/切换公共激活版本（`admin`） |
| `GET` | `/api/v1/auth/setup-status` | 库里有没有 admin（公开） |
| `POST` | `/api/v1/auth/initialize` | 建第一个 admin（公开，且只在零 admin 时；要 email + username + password） |
| `POST` | `/api/v1/auth/login/local` | 本地登录（公开，响应体不含 token）。`identifier` = **邮箱或用户名**，旧的 `email` 字段名仍接受 |
| `POST` | `/api/v1/auth/register` | 自助注册（公开，受 `auth.allow_registration` 控制；同样要 username） |
| `POST` | `/api/v1/auth/logout` | 清 cookie（公开，幂等） |
| `GET` | `/api/v1/auth/providers` | 有哪些登录方式（公开；OIDC 不在 M9） |
| `GET` | `/api/v1/auth/me` | 当前用户（含 `username`；**仅会话**，PAT → 403 `session_required`） |
| `PATCH` | `/api/v1/auth/me` | 改**自己**的用户名（body `{"username": ...}` → `UserResponse`；**仅会话**）。不动令牌、不换桶 |
| `POST` | `/api/v1/auth/change-password` | 改密 = 撤销所有会话（**仅会话**） |

MCP **没有创建类路由**：`PUT /api/mcp/config`（整份替换 = 新建 + 删除）返回 404/405。
技能创作与审核路由见上表（`POST /api/skills`、`/api/skills/mine`、`/api/admin/skills/reviews`）。

浏览器应走前端同源 `/api`，不要直接跨源打 8001（开发时 Next rewrite，生产时 nginx）。

## 知识库（MinerU + Qwen3-Embedding-8B + Milvus）

`knowledge_base.enabled: true` 后，lifespan 建立知识库服务。**所有服务地址只来自
config.yaml**，代码里没有默认端点；启用时缺 `parse.local.endpoint`（或云端
`base_url` + `api_key`）、`embedding.base_url`、`vector_store.uri` 会拒绝启动。

MinerU 的**接口路径也是配置**：本地 `parse.local.file_parse_path`（默认 `/v1/file_parse`）、
云端 `parse.cloud.file_urls_path`（默认 `/api/v4/file-urls/batch`）与
`parse.cloud.extract_results_path`（默认 `/api/v4/extract-results/batch/{batch_id}`，必须含
`{batch_id}`）。解析器里没有路径常量，换网关/代理只改配置；路径必须相对（含 `://` 直接
报错），主机写在 `endpoint`/`base_url`。云端响应里的签名上传 URL 和 `full_zip_url` 是
服务端临时地址，不配置。

```bash
# 只在启用知识库时需要；同时保留 postgres/mcp 等其它 extra
uv sync --all-packages --all-extras
```

数据流：上传 → 用户级文件落盘 → 后台任务 → MinerU（PDF/Office）或本地读取
（`.md/.markdown/.txt/.html/.htm/.csv`）→ 清洗 → 按类型 token 分片 →
Qwen3-Embedding-8B（4096 维）→ Milvus。元数据与文档状态共用主数据库；文档数
`document_count` 和当前活跃分片数 `chunk_count` 在知识库行上原子维护。

创建知识库时必须选择类型（类型不可改）：技术文档、运维手册、FAQ、安全合规、API 参考、
法律法规（`legal`）。默认 token 档位：技术/运维/安全/法律 512/64，FAQ 384/0，API 768/0；
硬上界 1024 token。分片和查询都走 Qwen3 tokenizer，不可用时会明确回退近似计数。

`security` 与 `legal` 是两种东西：前者是**内部**安全/合规要求与控制项，后者是**外部**
法规原文；`legal_clause` 策略按 `第X条（之N）/ Article N / § N / 条款 N` 切分，
一条一个分片，条号写进 `section`，引用能直接落到“《XX 办法》第十二条”。

Agent 自动获得只读 `knowledge_search` 工具。工具把 `knowledge_base_type` 作为参数，
由模型按问题语义路由；服务端始终按 `user_id` 过滤，模型不能传身份。首版入库任务
在 Gateway 进程内运行，重启会中断；按单 worker 部署，多实例前需换成持久任务队列。

检索是混合检索（§7.4）：问题拆成 `query`（向量）与 `keyword_query`（BM25 关键词，
可省略、缺省时复用 `query`），两路各取 `retrieval.candidate_k` 条后由 Milvus 
`hybrid_search` + `RRFRanker(k=retrieval.rrf_k)` 融合；`retrieval.rerank.enabled=true`
时再把融合候选送给 rerank 模型精排（`protocol: cohere | tei`，模型默认
`Qwen/Qwen3-Reranker-8B`）。关键词通道或精排不可用时按设计文档降级，并在返回值里
如实标注 `retrieval_mode` 与 `reranked`。

collection 需要 Milvus 2.5+ 的 BM25 能力：`content` 字段带 analyzer（默认 `chinese`）、
`sparse` 字段由 BM25 Function 自动生成、稀疏索引 metric 为 `BM25`。上线前的旧
collection 没有这些字段，关键词通道会报 `keyword_search_unavailable` 并降级为纯向量，
需要新建 collection 后重索引。`retrieval.keyword.analyzer` 是 destructive 参数。

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/api/knowledge/status` | 启用状态、限制、五类分片参数 |
| `GET`/`POST` | `/api/knowledge/bases` | 列 / 建知识库 |
| `GET`/`PATCH`/`DELETE` | `/api/knowledge/bases/{id}` | 详情 / 改名 / 级联删除 |
| `GET`/`POST` | `/api/knowledge/bases/{id}/documents` | 文档列表 / 批量上传（202） |
| `POST` | `/api/knowledge/bases/{id}/documents/{doc}/reindex` | 重新解析 / 索引 |
| `DELETE` | `/api/knowledge/bases/{id}/documents/{doc}` | 删除文档与向量 |
| `GET` | `/api/knowledge/search` | 检索测试（`q` 向量 + `keyword` BM25、`type` / `base_id` 过滤） |

## 测试

```bash
# 必须在仓库根跑：测试 import backend.* 和 agentkit.*
uv run python -m pytest backend/tests
```

## 排障

| 现象 | 处理 |
|---|---|
| `No module named 'backend'` | 在仓库根启动 |
| `No config file found` | `--env-file .env`，或设置 `MYFLOW_CONFIG_PATH` |
| `$DEEPSEEK_API_KEY is not set` | `.env` 里补上，并用 `--env-file` |
| `address already in use` | 换 `--port` 或杀掉占用 8001 的进程 |
| 登录页一直说"登录状态已失效" | 先确认网关进程不是旧的：**改完代码要重启 uvicorn**（README 里的 `--reload` 只在开发时开）。旧 cookie 从 9A 起不会再锁死登录页 |
| SSE 只有一帧 `event: end` | 订阅早于 worker 第一帧 `publish`；见 `MemoryStreamBridge.subscribe` 在 buffer 为 None 时直接 END |
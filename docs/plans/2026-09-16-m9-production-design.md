# M9 生产化：多用户、多实例、调度、通道 详细设计

> 配套文档：[分阶段开发方案](2026-09-16-greenfield-agent-service-roadmap.md) ·
> [M0 地基](2026-09-16-m0-foundation-design.md) · [M1 最小 Agent](2026-09-16-m1-minimal-agent-design.md) ·
> [M2 工具与沙箱](2026-09-16-m2-tools-sandbox-design.md) · [M3 Gateway 与流式](2026-09-16-m3-gateway-streaming-design.md) ·
> [M4 持久化](2026-09-16-m4-persistence-design.md) · [M5 前端](2026-09-16-m5-frontend-design.md) ·
> [M6 中间件体系](2026-09-16-m6-middleware-design.md) · [M7 MCP 与技能](2026-09-16-m7-mcp-skills-design.md) ·
> [M8 子代理委托](2026-09-16-m8-subagents-design.md) ·
> [路由层详细设计](2026-09-16-router-layer-design.md)
>
> 本文细化 M9（3–4 周，四条主线内部可并行，9E 默认跳过）。沿用包名：框架层 `agentkit.*`，应用层 `app.*`。
> 读完应能按文件树、接口、陷阱、测试清单直接开工。

---

## 1. M9 是"能演示"到"能上线"的距离

前面八个里程碑做出来的东西，**在你自己的机器上、你一个人用、只开一个进程**时是完整的。
M9 把这三个前提逐个拆掉。

| 线 | 拆掉的前提 | 核心难点 | 不可恢复错误 |
|---|---|---|---|
| **9A 多用户与鉴权** | "只有你一个人用" | 身份的唯一权威来源 | 图构建和中间件落进不同用户桶 |
| **9B 多 Gateway 实例** | "只开一个进程" | 谁有权写终态 | **丢租约的进程继续写终态** |
| **9C 定时任务调度** | "只有人类会发起对话" | 调度状态机 ≠ run 状态机 | 平行执行栈（两套生命周期） |
| **9D IM 通道** | "只从浏览器进来" | 什么能发出去 | **黑名单外发 → 记忆泄漏到群里** |
| **9E MCP 长时任务**（可选） | "工具调用几秒就返回" | 数据库是真相源 | 句柄丢了远端还在跑 |

9B 和 9D 各对应一个**修不了数据也撤不回**的故障。评审压在这两条上。

### 1.1 依赖：9A 必须先做

| 周 | 内容 |
|---|---|
| W1 | **9A 身份边界 + 认证门 + 存储分桶迁移**。没有这一周，后面四条线会各实现一遍身份 |
| W2 | 9B 租约 / 孤儿回收 / Redis StreamBridge（并行：9D 通道骨架 + 出站白名单） |
| W3 | 9C 调度器（并行：9D 两个平台适配：一个 wait、一个 stream） |
| W4 | 跨线收口：kill -9 接管、IM 开记忆不泄漏、调度走同一条 `start_run`。9E **默认跳过** |

OIDC/SSO、PAT、第三个 IM 平台、细粒度工具 RBAC，都是 W4 有余量再加的增量，不是 W1 阻塞项。

### 1.2 M9 新增 / 改动的文件

```
agentkit/
├── runtime/
│   ├── user_context.py              # M4 已有 AUTO；本阶段补 resolve_runtime_user_id 优先级链
│   ├── stream_bridge/
│   │   └── redis.py                 ★  # 换实现，协议不变
│   └── runs/
│       ├── manager.py               # 心跳、renew、claim_for_takeover、request_cancel
│       └── store/base.py            # 补 renew_lease / claim_for_takeover / request_cancel
├── config/
│   ├── paths.py                     # M2 已有 make_safe_user_id；本阶段全链路强制走它
│   ├── auth_config.py               ★
│   ├── scheduler_config.py          ★  # 若干字段 STARTUP_ONLY
│   ├── channels_config.py           ★
│   └── stream_bridge_config.py      # 加 redis url；backend=redis 时 STARTUP_ONLY
├── authz/
│   ├── principal.py                 ★  # 唯一构造 Principal 的入口
│   └── provider.py                  ★  # Protocol；M9 内置 AllowAll，细粒度推后
├── scheduler/
│   └── schedules.py                 ★  # cron / once / timezone
└── persistence/
    ├── user/                        # 或者由 app.gateway.auth 持有 users 表（推荐）
    ├── personal_access_tokens/      # W4 可选
    ├── scheduled_tasks/             ★
    ├── scheduled_task_runs/         ★
    └── channel_connections/         ★

app/
├── gateway/
│   ├── auth_middleware.py           ★  # fail-closed 全局门
│   ├── csrf_middleware.py           ★
│   ├── internal_auth.py             ★  # 通道 / 调度器服务间认证
│   ├── auth/
│   │   ├── models.py                ★  # User：id / email / password_hash / system_role / token_version
│   │   ├── jwt.py / password.py / session_cookie.py
│   │   └── routers.py               ★  # prefix=/v1/auth，§2.12
│   └── routers/
│       ├── scheduled_tasks.py       ★  # prefix=/scheduled-tasks
│       ├── channels.py              ★  # prefix=/channels；表在 persistence/channel_connections
│       └── feedback.py              ★  # prefix=/runs，/ {run_id}/feedback
├── scheduler/
│   └── service.py                   ★  # 只决定何时；dispatch 调 start_run
└── channels/
    ├── base.py / manager.py / message_bus.py / service.py
    ├── slack.py                     ★  # wait 策略
    └── telegram.py                  ★  # stream 改卡片策略
```

用户表放在 `app.gateway.auth`，**不要**放进 `agentkit.persistence`。
harness 只认识 `CurrentUser` Protocol（有 `.id: str`）。persistence 读 ContextVar，
gateway 写——依赖方向不能反。

---

## 2. 9A 多用户与鉴权（W1）

### 2.1 一开始就要定的：身份只有一个权威来源

**客户端传来的任何身份字段一律剥离。** 不是"校验后使用"，是**删掉**。
写在 `start_run()` / `normalize_input()` 里，所有入口（HTTP、IM、调度、嵌入）共用：

```python
_CLIENT_IDENTITY_KEYS = ("user_id", "owner_id", "auth_user", "auth_user_id")

def strip_client_identity(body) -> None:
    for bag in (body.metadata, body.config.get("configurable"), body.config.get("context"), body.context):
        if isinstance(bag, dict):
            for key in _CLIENT_IDENTITY_KEYS:
                bag.pop(key, None)
```

然后按一条**固定的优先级链**解析真实身份。函数只有一个，名字就叫
`resolve_runtime_user_id`，图构建、中间件、记忆、技能、自定义 agent、提示词装配
**全部调它**：

```
1. 服务端认证注入的身份（runtime.context.user_id，由 AuthMiddleware / internal_auth 写入）
2. 若将来接 Agent Server：server_info.user.identity，先 make_safe_user_id
3. 请求 ContextVar（user_context.get_current_user()）
4. 仅内部/嵌入/无 HTTP 路径：default 桶
```

**图构建和中间件执行落进不同用户桶**是这里最典型的 bug——
它只在多用户环境下出现，单用户测试完全看不出来。
工厂必须把解析结果**显式**传给 skills / skill policy / 静态 prompt，
不要让其中一条回头再读一遍可变配置或 ContextVar。

### 2.2 两个 ID，不要混

认证系统允许身份是邮箱之类的任意非空字符串。直接拿去拼目录会出问题
（`/`、`..`、大小写、超长）。

| ID | 谁用 | 形状 |
|---|---|---|
| **原始 identity** | 授权过滤、JWT `sub`、users 表主键（本地账号用自己的 uuid/email 策略） | 任意非空 |
| **存储 ID** | 文件系统分桶、sandbox 路径、memory 路径 | `make_safe_user_id(raw)` → `[A-Za-z0-9_-]+` |

```python
_UNSAFE = re.compile(r"[^A-Za-z0-9_\-]")

def make_safe_user_id(raw: str) -> str:
    if not raw:
        raise ValueError("user_id must be a non-empty string.")
    sanitized = _UNSAFE.sub("-", raw)
    if sanitized == raw:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{sanitized}-{digest}"
```

已经安全的 id 原样通过，避免无谓改桶。碰撞靠 digest 后缀，不要靠截断。
M2 的 `SandboxProvider.acquire(..., user_id=)` 收的是**存储 ID**。

### 2.3 仓储层的三态 `user_id`

M4 已经把默认值定成 `AUTO`。M9 把语义钉死，**所有**仓库（thread_meta / runs /
run_events / scheduled_tasks / channel_connections / feedback）同一套：

```python
# AUTO：从 ContextVar 解析；没有用户 → RuntimeError，绝不退化成全局查询
# str ：显式指定（测试、通道 worker 传入 owner）
# None：跳过隔离——只允许迁移脚本和管理员 CLI。参数名写成 user_id=None 要出现在调用点，
#       不要包一层"方便"的 bypass=True
async def get(self, thread_id: str, *, user_id: str | None | _AutoSentinel = AUTO) -> dict | None: ...
```

`resolve_user_id(AUTO)` 无 ContextVar 时抛，不要静默返回 `"default"`。
`"default"` 只属于 `get_effective_user_id()` 这条**文件系统**回退，仓库查询不用它。

跨用户读到的行：仓储返回 `None`，路由映射成 **404**，不是 403。
403 泄露"这个 thread id 存在"。thread id 可枚举。

### 2.4 用户表与 JWT

```python
class User(Base):
    __tablename__ = "users"
    id: Mapped[str]                    # JWT sub
    email: Mapped[str]                 # unique
    password_hash: Mapped[str | None]  # bcrypt；OIDC 用户可空
    system_role: Mapped[str]           # "admin" | "user"
    needs_setup: Mapped[bool]          # reset 后强制改密
    token_version: Mapped[int]         # 改密 / reset 时 +1
    created_at / updated_at
```

角色 M9 只承认这两个。细粒度 RBAC 见 §12。
未知 role 在 `build_principal_from_context` 里**不**被改写成 default——缺/空才用 default；
非空但未知交给执行层 fail-closed（M10 护栏），M9 的路由层只认 admin/user。

JWT 声明：`sub`、`tv`（token_version）、`exp`。校验：

1. 签名 + exp
2. 用户存在
3. `tv == user.token_version`——不匹配必须 401。这是改密码后让所有旧 session
   立刻失效的**唯一**机制。不要靠缩短 exp 假装能踢人。

**JWT 只在 HttpOnly cookie `access_token` 里传输，不出现在响应 JSON。**
登录响应体只有 `expires_in` 和 `needs_setup`。测试钉死：响应 body / 响应头
`Authorization` 里都没有 token 字符串。

Cookie 策略（`SessionCookiePolicy`）：

- 只在 HTTPS、受信转发 HTTPS、direct-host localhost HTTP、或操作员显式 opt-in 时持久化
- 公网 HTTP 沙箱 URL 降级成 session cookie（关浏览器即没）
- `remember_me` 只影响 `max_age`；密码从不存
- CSRF cookie 的 `max_age` 必须与 access cookie 一起 stamp，改密重签时成对更新
- logout 清掉所有 auth cookie，且 **logout 响应上禁止再签发 CSRF**

### 2.5 认证门默认拒绝

`AuthMiddleware` 是全局 fail-closed 门：**不在 public 白名单上的路由一律要求认证**。
新加一条路由默认就是受保护的。反过来（默认放行、要保护的加装饰器）
只要有人忘了加装饰器就是一个洞。

Public 白名单（精确 + 前缀，写进模块常量，测试遍历 OpenAPI 断言非白名单都 401）：

```python
_PUBLIC_EXACT_PATHS = frozenset({
    "/health", "/health/ready",
    "/docs", "/redoc", "/openapi.json",
    "/api/v1/auth/login/local",
    "/api/v1/auth/register",
    "/api/v1/auth/logout",
    "/api/v1/auth/setup-status",
    "/api/v1/auth/initialize",
    "/api/v1/auth/providers",
})
_PUBLIC_PATH_PREFIXES = (
    "/api/v1/auth/oauth/",
    "/api/v1/auth/callback/",
)
```

任何非 public 路由**不能只靠"cookie 存在"放行**，必须严格验证 JWT。
存在 cookie 但无效 / 过期 / 用户不存在 / `token_version` 不匹配 → 401，
不要落到业务路由再 500。

`GATEWAY_ENABLE_DOCS=false` 时把 docs 从白名单拿掉（生产默认关）。

登录失败按 **TCP peer IP** 计数。只在 peer 属于 `AUTH_TRUSTED_PROXIES` 时才信
`X-Real-IP`，**不要用 `X-Forwarded-For`**（客户端可伪造）。
`max_login_attempts` 最小为 2（单次失败不得锁 IP）。计数器是进程内 dict——
多 worker 下近似，写进已知边界，M9 不引入 Redis 限速。

### 2.6 CSRF

Double-submit：

- 服务端设 `csrf_token` cookie
- 浏览器 state-changing 请求带同值 `X-CSRF-Token`
- `secrets.compare_digest` 比较

需要 CSRF 的方法：`POST` `PUT` `DELETE` `PATCH`。

auth bootstrap（login / register / initialize / logout）**不**要求 double-submit
（首次调用时浏览器还没有 token），但必须校验 browser `Origin`，拒绝 hostile Origin，
避免 login CSRF / session fixation。

CORS 默认 same-origin（nginx :2026）。拆 origin 的浏览器客户端必须同时配
`GATEWAY_CORS_ORIGINS`——`CORSMiddleware` 和 `CSRFMiddleware` **读同一个变量**。
`Content-Location`（run id）不是 CORS safelist，要进 `CORS_EXPOSED_HEADERS`，
否则前端 SDK 的 `onCreated` 拿不到 run id。

Bearer PAT（W4）跳过 CSRF 的 cookie 对，但 Origin 检查仍跑。
**无效 Bearer 是硬 401，禁止回退到 cookie**——否则 CSRF 中间件的"Bearer 就跳过"
会变成一个洞：攻击页面随便写一个坏的 `Authorization`，请求就改走 cookie 会话。

### 2.7 首次初始化

没有 admin 时服务**不**自动建号，只打日志提示访问 `/setup`。

1. `GET /api/v1/auth/setup-status` → `{needs_setup: true}`
2. `POST /api/v1/auth/initialize` 只在没有 admin 时可用
3. 创建 `system_role=admin`、`needs_setup=false`
4. 设 access_token cookie

并发初始化靠 `users.system_role` 的部分唯一索引（`WHERE system_role='admin'`）
或 email unique + 应用层 "zero admin" 检查；失败者 409。

`python -m app.gateway.auth.reset_admin`：随机密码、`token_version += 1`、
`needs_setup=true`、写入 `0600` 凭据文件。stdout **只打路径，不打明文**。

### 2.8 存储分桶是一次迁移

M2 已定虚拟路径 `/mnt/user-data/{workspace,uploads,outputs}`。
宿主机布局从 M2 的扁平目录升为：

```text
{base}/users/{storage_user_id}/threads/{thread_id}/user-data/{workspace,uploads,outputs}
{base}/users/{storage_user_id}/memory.json          # M10
{base}/users/{storage_user_id}/agents/{name}/       # 自定义 agent
```

**用户分桶对 agent 完全不可见。** 虚拟路径映射发生在每个沙箱实例 acquire 时（M2 §3），
不是全局表。少传一次 `user_id=`，文件就会落到 `default` 桶，agent 读不到——
这是 9D 必须显式传参的原因。

迁移脚本：

- `--dry-run`
- 按用户逐个执行；单个失败继续，最后非零退出
- 旧布局 `{base}/threads/{tid}/...` 迁到 admin 桶（认证启用后的升级路径）
- 旧共享 `{base}/agents/` 只读兼容；更新/删除前必须先迁

`threads_meta.user_id`、`runs.user_id` 与目录名必须是同一个存储 ID。
创建 thread 时客户端 `metadata.user_id` / `owner_id` 已在 §2.1 被剥掉，
仓储 `create(..., user_id=AUTO)` 从 ContextVar 写入。

### 2.9 内部认证：谁能代表某用户

IM 通道和调度器不是浏览器。它们走进程内 HTTP 调自己的 Gateway，
需要一对内部头：

```
X-DeerFlow-Internal-Token: <DEER_FLOW_INTERNAL_AUTH_TOKEN>
X-DeerFlow-Owner-User-Id:  <storage user id>
```

**只有 AuthMiddleware 能把这对头授权成当前用户。** 任何其它代码路径读到
`X-DeerFlow-Owner-User-Id` 都必须当普通不可信输入丢掉。

已知边界，必须写进 `internal_auth.py` 模块 docstring 和部署文档：

> 如果把 `DEER_FLOW_INTERNAL_AUTH_TOKEN` 下发给了终端（移动 App、浏览器），
> 那个"代表某用户"的头就可以被伪造。正确做法是只有 Gateway 本机 / 通道 worker /
> 调度器持有密钥。

内部调用同时带一对匹配的 CSRF cookie/header（通道 SDK 自己注入），
这样 CSRF 中间件不用为内部流量开第二套例外。

`runtime.context.is_internal = True` 由这条路径写入。下面这些键
**只接受 is_internal 调用方**，客户端塞的一律丢：

| 键 | 谁用 |
|---|---|
| `non_interactive` | 9C：去掉 `ask_clarification` |
| `channel_user_id` | 9D：群里的原始发送者，只进 runtime.context，**不进 configurable** |
| `github_token` 一类请求级密钥 | 不进 checkpoint（M7 §6） |

### 2.10 Principal：构造入口只有一个

```python
@dataclass
class Principal:
    user_id: str | None = None
    role: str | None = None
    oauth_provider: str | None = None
    oauth_id: str | None = None
    channel_user_id: str | None = None
    is_internal: bool = False
    attributes: dict[str, Any] = field(default_factory=dict)

def build_principal_from_context(context: Mapping, *, default_role: str) -> Principal:
    role = context.get("user_role")
    if role is None or role == "":
        role = default_role
    return Principal(
        user_id=context.get("user_id"),
        role=role,
        ...,
        is_internal=context.get("is_internal") is True,  # 必须 is True，别的真值不算
        attributes=normalize_authz_attributes(context.get("authz_attributes")),
    )
```

`authz_attributes` 非 Mapping 直接 `TypeError`，不要静默 `{}`。
M8 的 `task` 工具要把 Principal 字段（含 `is_internal` **即使是 False**、
`channel_user_id`）拷进子代理 context，和 lead 同一套归属。

M9 的内置 provider 是 AllowAll（接口在，策略全放行）。工具级 deny 是 M10。
路由级权限本阶段只需要：

- 已登录 → 普通 API
- `system_role=admin` → 用户管理 / 全局技能上传
- PAT（若做）→ 白名单路由 + scope

### 2.11 本地关闭认证

`auth.enabled=false` 仅用于单人演示。此时 AuthMiddleware 注入一个固定的
`auth-disabled` 用户，所有数据进该用户桶。生产启动若 `GATEWAY_WORKERS>1`
且 `auth.enabled=false` → **拒绝启动**（和 9B 的预检放一起）。

### 2.12 路由

路由层 §3.2 把四组划给 M9：`/api/v1/auth/**`、`/api/scheduled-tasks/**`、`/api/channels/**`、`/api/runs/{run_id}/feedback`。
错误体仍是 `{detail, code, trace_id}`。业务路由不 `try/except` 拼状态码。
`create_app` 给这四个 router 加 `/api`；各自的 `prefix` 写在文件里。

白名单是 §2.5 那份。白名单以外先过 AuthMiddleware，再过下面的 `require_scope`。
未登录是 401 `unauthenticated`，函数体不跑。缺 scope 是已有的 403 `permission_denied`。

```python
# app/gateway/deps.py
def caller_from_request(request: Request) -> RunCaller:
    """M3 的匿名实现到这里换成读中间件。body 里的身份仍然不看。"""
    auth = getattr(request.state, "auth", None)
    if auth is None:
        return RunCaller(kind=CallerKind.HTTP, user_id=None)
    kind = CallerKind.INTERNAL if auth.kind == "internal" else CallerKind.HTTP
    scopes = frozenset(auth.scopes) if auth.scopes is not None else None
    return RunCaller(kind=kind, user_id=auth.user_id, scopes=scopes, user_role=auth.role)
```

`auth.scopes is None` 表示会话用户，不按 scope 裁。PAT 带具体集合。内部 token 是 `CallerKind.INTERNAL`，`user_id` 来自 `X-DeerFlow-Owner-User-Id`，而且只有 AuthMiddleware 读这只头（§2.9）。

#### 认证 `app/gateway/auth/routers.py`

`prefix="/v1/auth"`。这些路由在白名单上的，不再套 `require_scope`。

| Method | Path | 请求 | 成功 | 谁能打 |
| --- | --- | --- | --- | --- |
| GET | `/api/v1/auth/setup-status` | 无 | `{"needs_setup": bool}` | 公开 |
| POST | `/api/v1/auth/initialize` | `InitializeRequest` | 201，`UserResponse`，并设 cookie | 公开，且库里还没有 admin |
| POST | `/api/v1/auth/login/local` | `LoginRequest` | 200，`LoginResponse`，并设 cookie | 公开 |
| POST | `/api/v1/auth/register` | `RegisterRequest` | 201，`UserResponse`，并设 cookie | 公开；`auth.allow_registration` 为假则 403 `registration_closed` |
| POST | `/api/v1/auth/logout` | 无 | 200 `{"ok": true}`，清 cookie | 公开。有会话就作废，没有也 200 |
| GET | `/api/v1/auth/providers` | 无 | `{"local": true}` | 公开。OIDC 不在本阶段（§12） |
| GET | `/api/v1/auth/me` | 无 | `UserResponse` | 已登录 |
| POST | `/api/v1/auth/change-password` | `ChangePasswordRequest` | 200 `{"ok": true}`，`token_version += 1`，重签 cookie | **仅会话**。PAT 打这条 → 403 `session_required` |

```python
# app/gateway/auth/models.py  （请求模型 extra="forbid"，放在 auth 包里，不进 run 的 request.py）
class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)
    remember_me: bool = False

class InitializeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)

class RegisterRequest(InitializeRequest):
    pass

class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)

class UserResponse(BaseModel):
    id: str
    email: str
    system_role: Literal["admin", "user"]

class LoginResponse(BaseModel):
    expires_in: int
    needs_setup: bool
```

`UserResponse` 没有 `password_hash`，没有 `token_version`。`LoginResponse` 没有 token 字符串（§2.4）。Set-Cookie 由 `session_cookie.py` 写，路由只调用它。

| 情形 | HTTP | `code` |
| --- | --- | --- |
| 邮箱不存在或密码错 | 401 | `invalid_credentials`。两种情况同一个 detail，不透露哪个错了 |
| 同一 peer IP 超过 `max_login_attempts` | 429 | `login_rate_limited` |
| 已有 admin 再 initialize | 409 | `admin_already_exists` |
| 改密时当前密码错 | 401 | `invalid_credentials` |
| change-password 的调用方是 PAT | 403 | `session_required` |

登录计数用 TCP peer IP。只有 peer 落在 `AUTH_TRUSTED_PROXIES` 里才读 `X-Real-IP`（§2.5）。

`logout` 清 access cookie 和 CSRF cookie，并且这个响应上不再签发新的 CSRF（§2.4）。

#### 定时任务 `app/gateway/routers/scheduled_tasks.py`

`prefix="/scheduled-tasks"`。每条都要同时有 `threads:write` 和 `runs:create`（§4.9）。缺一即 403。

```python
# app/gateway/deps.py
def require_all_scopes(*scopes: str) -> Callable[[Request], RunCaller]:
    def _dep(request: Request) -> RunCaller:
        caller = caller_from_request(request)
        missing = [scope for scope in scopes if not caller.allows(scope)]
        if missing:
            raise RunPermissionDenied(f"missing scope: {', '.join(missing)}")
        return caller
    return _dep
```

| Method | Path | 请求 | 成功 | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/api/scheduled-tasks` | `limit` 1..200，默认 50 | `Page[ScheduledTaskResponse]` | 只列 `user_id=caller.user_id` |
| POST | `/api/scheduled-tasks` | `ScheduledTaskCreateRequest` | 201，`ScheduledTaskResponse` | 非法时区或 cron → 422 `invalid_schedule` |
| GET | `/api/scheduled-tasks/{task_id}` | `TaskId` | `ScheduledTaskResponse` | 不是自己的 → 404 `scheduled_task_not_found` |
| PATCH | `/api/scheduled-tasks/{task_id}` | `ScheduledTaskPatchRequest` | 200 | 有 queued/launching/running 的 occurrence → 409 `task_active`（§4.7） |
| POST | `.../{task_id}/pause` | 无 | 200 | 把 queued 标 cancelled；launching/running 拒绝，409 `task_active` |
| POST | `.../{task_id}/resume` | 无 | 200 | 同样：有活跃 occurrence 则 409 |
| POST | `.../{task_id}/trigger` | 无 | 202 `{"occurrence_id": ...}` | 允许在 paused 上手动跑。已有非终态 occurrence 则复用那一行，不另开 |
| DELETE | `.../{task_id}` | 无 | `{"deleted": task_id}` | 规则同 pause |
| GET | `.../{task_id}/runs` | `limit` | `Page[OccurrenceResponse]` | occurrence，不是 Gateway run |

```python
# app/gateway/models/request.py
TaskId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")]

class ScheduledTaskCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=8000)
    schedule_type: Literal["cron", "once"]
    schedule_spec: dict[str, str]          # cron: {"cron": "0 * * * *"}；once: {"run_at": ISO}
    timezone: str = Field(min_length=1, max_length=64)
    context_mode: Literal["fresh_thread_per_run", "reuse_thread"] = "fresh_thread_per_run"
    thread_id: str | None = None           # 仅 reuse_thread；必须是 caller 自己的 thread
```

`thread_id` 若给出，先 `require_visible_thread`。看不见是 404 `thread_not_found`，不是 403。
`overlap_policy` 不在请求里。服务端固定写 `enqueue`（§4.4）。客户端传这个字段 → 422 `invalid_request`。

路由调用 `app/scheduler/service.py` 的仓储方法，不自己 `create_task`，也不调用 `start_run`。手动 trigger 只插入或复用 occurrence。真正开跑仍是 poller 的 `launching` 状态（§4.3）。

`ScheduledTaskResponse` 含 `id`、`title`、`status`、`schedule_type`、`schedule_spec`、`timezone`、`context_mode`、`thread_id`、`next_run_at`、`last_run_id`、`last_error`。时间是 ISO-8601、`Z` 结尾。没有 `lease_owner`。

#### 通道 `app/gateway/routers/channels.py`

`prefix="/channels"`。`{name}` 只允许本阶段的两个平台：`slack`、`telegram`。其它 → 422 `invalid_request`。

| Method | Path | 能力 | 成功 | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/api/channels` | `channels:read` | `{"channels": [ChannelStatus]}` | 平台是否启用、自己有没有绑定。没有 bot token |
| POST | `/api/channels/{name}/bind` | `channels:write` | 201 `{"code": ..., "expires_in": 600}` | 码只出现在这个响应里（§5.4） |
| DELETE | `/api/channels/{name}/bind` | `channels:write` | `{"deleted": name}` | 删的是这个 caller 的绑定 |
| POST | `/api/channels/{name}/restart` | `admin` | `{"restarted": name}` | 重启本进程里的那个平台连接，不是用户绑定 |

`ChannelStatus` 的字段：`name`、`enabled`、`bound`、`external_account_id`（已绑定才有，是平台账号 id，不是 token）。

消费绑定码发生在通道的入站消息里，不另开 HTTP。码错、过期、用过，入站都当无效，聊天里不回显码。HTTP 这边没有「提交码」的路由。

#### 反馈 `app/gateway/routers/feedback.py`

`prefix="/runs"`。路径是 `/api/runs/{run_id}/feedback`，不把 `thread_id` 再放进 URL。thread 从 run 行读。

| Method | Path | 能力 | 成功 |
| --- | --- | --- | --- |
| PUT | `/api/runs/{run_id}/feedback` | `runs:read` | 200 `FeedbackResponse`。同一 caller + run + `message_id` 再 PUT 是覆盖 |
| GET | `/api/runs/{run_id}/feedback` | `runs:read` | `{"feedback": FeedbackResponse \| null}` |

```python
# app/gateway/models/request.py
class FeedbackPutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rating: Literal[-1, 1]
    comment: str | None = Field(default=None, max_length=2000)
    message_id: str | None = Field(default=None, max_length=64)
```

`message_id` 缺省表示对整次 run。先 `run_manager.get(run_id, user_id=caller.user_id)`，没有或不属于 caller → 404 `run_not_found`。不单开 `feedback_not_found`：GET 没有记录时 `feedback` 是 `null`，不是 404。

`FeedbackResponse` 有 `feedback_id`、`run_id`、`thread_id`、`message_id`、`rating`、`comment`、`created_at`。没有 `user_id`。评论原样返回，不进系统提示词。

#### 错误

| 异常 | HTTP | `code` | 所属文件 |
| --- | --- | --- | --- |
| `Unauthenticated` | 401 | `unauthenticated` | `app/gateway/auth_middleware.py`。白名单外没有有效会话、PAT 或内部 token |
| `InvalidCredentials` | 401 | `invalid_credentials` | `app/gateway/auth/routers.py` |
| `LoginRateLimited` | 429 | `login_rate_limited` | 同上 |
| `AdminAlreadyExists` | 409 | `admin_already_exists` | 同上 |
| `RegistrationClosed` | 403 | `registration_closed` | 同上 |
| `SessionRequired` | 403 | `session_required` | 同上。PAT 打了只允许会话的路由 |
| `InvalidSchedule` | 422 | `invalid_schedule` | `app/gateway/routers/scheduled_tasks.py` |
| `ScheduledTaskNotFound` | 404 | `scheduled_task_not_found` | 同上 |
| `TaskActive` | 409 | `task_active` | 同上 |
| `RunNotFound` | 404 | `run_not_found` | 已有。反馈和别人的 run 都用它 |

这些类带 `status_code` 和 `code`，登记进路由层的 `_handle_domain_error`。`detail` 不含密码、绑定码、bot token、内部 token。

#### 不做

| 不做 | 理由 |
| --- | --- |
| OIDC 的 `/oauth`、`/callback` | §12。白名单前缀先留着，路由不注册，所以打过去是 404 不是 401 |
| PAT 的签发路由 | 同上，W4 可选。`caller_from_request` 已经能读 PAT 的 scopes |
| `POST /api/channels/{name}/bind` 的码从聊天回读 | §5.4。没有第二条 HTTP 把码再取出来 |
| 反馈路由放在 `/api/threads/{id}/runs/{id}/feedback` | 路由层的路径是 `/api/runs/{run_id}/feedback`。thread 从 run 行取，避免两个 id 不一致还得再判一次 |
| 调度路由里 `start_run` | poller 才是唯一会进入 `launching` 的地方（§4.3） |


---

## 3. 9B 多 Gateway 实例（W2）

M3 已经为 `RunRecord` 留了 `owner_worker_id` / `lease_expires_at` / `ownership_lost` /
`stop_reason`。M4 已经建了列，单 worker 启动时把所有 inflight 标 error。
M9 把"本进程挂了"换成"本进程丢了租约，peer 还活着"。

### 3.1 一开始就要定的：丢租约之后不准再写终态

一个进程在跑某个 run，网络抖动导致续约失败，租约过期。
此时另一个进程的孤儿回收把这个 run 认领走了、标成了 `error`、给客户端补了 `end`。

**然后原进程的 agent 跑完了，去写"成功"。**

结果：客户端收到了 `end`（error），数据库里是 `success`，SSE 再也不会有第二个 `end`。
这个状态谁也修不了。

规则，写进 `RunRecord` 和 worker `finally` 的模块 docstring：

> **一旦本进程确认丢失租约（`ownership_lost`），收尾阶段的所有持久化全部跳过。**
> 这个进程不准再写终态、不准再发业务 SSE、不准再更新 token 统计、不准写
> thread_meta、不准写 delivery receipt、不准 `on_run_completed`。
> 由接管的进程标 `error`（`stop_reason=orphan_recovered`）并 `publish_end`。

实现上**不要**写成十个 `if not record.ownership_lost`。抽一个函数：

```python
def should_persist_durable_state(record: RunRecord) -> bool:
    return not record.ownership_lost
```

worker `finally`、journal `update_run_progress` / `update_run_completion`、
`publish` 业务事件，全部问这一句。有人加第四处写入时漏掉 if 是必然的。

配套：**所有终态写入都带 `status IN ('pending','running')` 守卫**，先到的赢。
`update_run_completion` 还拒绝替换一个**不同的**终态（M4 已定）。
这样即使漏了一条路径，数据库层面也不会被覆盖。

`journal.close(flush=not ownership_lost)`：丢租约时连最后一次 flush 都不要。

### 3.2 租约的时间语义

```
lease_expires_at 只在 store 续约成功后才前进
```

它的含义是**"最后一次被确认拥有的截止时间"**，不是"我打算持有到什么时候"。
本地时钟往前推一个租约期然后写进内存是错的——续约失败时你以为还持有着。

```python
LEASE_SECONDS = 30          # 配置项，STARTUP_ONLY
RENEW_EVERY = LEASE_SECONDS / 3    # 允许连续失败两次而不丢
```

心跳循环（每 `RENEW_EVERY`）：

1. 对本进程持有的每条 run 调 `run_store.renew_lease(run_id, owner_worker_id, lease_seconds)`
2. 成功 → 用**返回的** `lease_expires_at` 更新内存（不要自己算）
3. 失败但 `now < record.lease_expires_at` → 记 warning，下次再试
4. 失败且 `now >= record.lease_expires_at` → `_mark_ownership_lost(record)`：
   设标志、`abort_event.set()`、`task.cancel()`、**不写 store**

`renew_lease` 的 WHERE：`run_id=? AND owner_worker_id=? AND status IN ('pending','running')`。
0 行更新 = 不是你的了（被 takeover 或已终态）。

`grace_seconds`（默认 10）加在 peer 回收的过期判断上：`lease_expires_at + grace < now`
才认领。给时钟skew 和"续约 SQL 刚提交、心跳还没写回内存"一个窗口。
**本进程自己的 ownership_lost 判断不加 grace**——那会让你在已经没租约时还继续跑。

`owner_worker_id = f"{hostname}:{uuid4().hex}"`，进程启动时生成一次，关机即废。

### 3.3 孤儿回收

```
1. 扫候选：list inflight 且 lease_expires_at + grace < now（优化，不是权限）
2. 每行 claim_for_takeover(run_id, new_owner)：
     UPDATE runs SET owner_worker_id=?, status='error', stop_reason='orphan_recovered', ...
     WHERE run_id=? AND status IN ('pending','running')
       AND lease_expires_at + grace < now
3. 认领成功 → 回调 Gateway：publish_end + schedule stream cleanup
4. 认领失败 → 别人已经续上或已经终态，跳过
```

第 2 步是关键：**扫描结果不是权限**。两个进程可能同时扫到同一行，
真正的仲裁是那个原子条件更新，只有一个能成功。

**不要**用 `update_status()` 做接管。M4 单 worker 的 `recover_inflight_runs`
在 `GATEWAY_WORKERS>1` 或 `multi_instance=true` 时必须换成这条路径，
否则启动时会把**别的实例正在跑的 run** 标成 error。

触发时机：

- 进程启动、开始接 HTTP **之前**扫一次
- 心跳每 N 个周期再扫一次（建议每 3 次续约扫一次）

周期扫描必须是 **single-flight**：上一轮没跑完，下一轮直接跳过。
否则扫描任务会堆积在唯一的那个续约循环上，把续约本身饿死——
这是一个会导致**所有 run 集体丢租约**的故障。

store 重试有次数/backoff 上限。单条 SQL 仍依赖驱动/池超时，不要在心跳里无限等。

### 3.4 取消在多实例下有三种结果

| 情况 | 结果 | HTTP |
|---|---|---|
| 本进程持有这个 run | 直接 `abort_event` + cancel task | 200 |
| 别的进程持有，租约有效 | `request_cancel(run_id, action)` 写库，等 owner 心跳读到后执行 | 202 |
| 别的进程持有，租约已过期 | `claim_for_takeover` 后按本地取消处理 | 200 |
| 存储层不支持取消请求（memory RunStore） | 拒绝 | 409 + `Retry-After` |

第二种**不转移租约**。转移租约会让两个进程都以为自己在跑。
owner 心跳在续约成功后读 `cancel_action`，对本进程 task 发 abort；
status 写入和 rollback 仍走 worker finalization，心跳自己不写终态。

取消和完成抢同一行：CAS，先到的赢。已经 `success` 的行，取消请求是 no-op。

### 3.5 多 worker 的硬前提要在启动时强制

```
GATEWAY_WORKERS > 1  或  runs.multi_instance=true
    ⇒  database.backend = postgres
    ⇒  run_events.backend = db
    ⇒  stream_bridge.backend = redis
    ⇒  心跳开启（lease_seconds > 0）
    ⇒  auth.enabled = true（见 §2.11）
```

任何一条不满足就 **拒绝启动**，不要降级运行。
SQLite 的并发写、内存事件存储、关掉的心跳，这三样在单 worker 下
都是合理的默认值，在多 worker 下都会静默地产生错误结果。

M4 的 `make_run_event_store` 已经预告了 `GATEWAY_WORKERS` 门。M9 把检查收拢到
一个 `assert_multi_worker_preflight(config)`，lifespan 里引擎初始化之后、
recover 之前调用。

这些字段进 `STARTUP_ONLY_FIELDS`：

```python
STARTUP_ONLY_FIELDS["runs.lease_seconds"] = "心跳循环在启动时按这个值建；热重载不重建。"
STARTUP_ONLY_FIELDS["stream_bridge.backend"] = "Redis vs memory 在启动时选定。"
STARTUP_ONLY_FIELDS["scheduler.max_concurrent_runs"] = "调度 poller 启动时捕获。"
STARTUP_ONLY_FIELDS["scheduler.queue_timeout_seconds"] = "同上。"
STARTUP_ONLY_FIELDS["scheduler.multi_instance"] = "同上。"
```

`scheduler.recursion_limit` **不是** STARTUP_ONLY：每次 dispatch 现读，
YAML 热重载对下一次调度 run 生效，不必重启 poller（根 AGENTS.md 已定）。

### 3.6 StreamBridge 换 Redis

M3 §7 的接口一字不改。`supports_cross_process = True`。

实现要点：

- 每个 `run_id` 一条 Redis stream + 一个 retained 缓冲（有界，超限从左裁，watermark 前进）
- `Last-Event-ID` 语义不变：落后于 watermark → `StreamGap` 然后停，不静默部分重放
- `publish_end` 的保证不变（M3 最重要的不变量）：嵌套 finally，END 一定发出
- heartbeat / END 的 Redis 消息 **不要当续传 id**（空 id）
- retained key 的 TTL（`stream_bridge.stream_ttl_seconds`）在每次 `publish` /
  `publish_end` 刷新——这是泄漏安全网，**不是** run 超时
- 孤儿回收标 `orphan_recovered` 之后，Gateway **补** `publish_end` 并延迟 cleanup

store-only SSE 与 `/wait` 消费者：普通 durable 终态 **不能**当成流已完成
（落库可能早于最后几帧）。**只有** `stop_reason=orphan_recovered` 允许在心跳时
合成 END——此时 producer 已死，可能没写成 END。

checkpoint 的跨进程缓存（如果 M4 做了进程内 cache）同样换成 Redis，
否则两个实例各自缓存会读到过期状态。没有 cache 就不用 Redis 这一层。

### 3.7 关闭顺序

M3 §6.3 + M4 §9 的关机顺序在多实例下多两步：

```
1. 在共享超时预算里优先收用户的 run task（给它们时间正常结束、写完终态）
2. 停心跳
3. 抽干孤儿回收（single-flight 那条）；未执行的延迟 stream cleanup 改立即删除
4. 停扩展服务（反序）→ 停通道 → 停调度 poller
5. 停存储 → 停 checkpointer → close_engine()
```

**先停心跳再收 run 是错的**：心跳停了，租约会在收 run 的过程中过期，
然后本进程自己的 run 会被自己的 `ownership_lost` 逻辑掐掉，
peer 再接管一次——用户看到一次正常对话变成 orphan error。

---

## 4. 9C 定时任务调度器（W3）

### 4.1 一开始就要定的：调度器只决定"何时"

> **调度器可以决定什么时候跑，但必须通过既有的 `start_run()` 派发，
> 绝不能引入一条平行的执行栈。**

违反它非常自然：调度器要跑一个任务，最直接的写法就是自己组装 agent、自己
`astream`、自己存结果。然后你就有了两套 run 生命周期，取消、超时、事件持久化、
token 统计全都要实现两遍，而且会不一致。

所有"启动一次 agent 回合"的路径——HTTP、IM、调度器、将来的 MCP 通知——
都收敛到 M3 的 `start_run()`。调度器是一个带内部认证的 caller，不是一个 runtime。

```python
async def launch_scheduled_thread_run(app, *, thread_id, user_id, prompt, ...) -> RunRecord:
    # 走内部认证 + CSRF 对，或进程内直接调 start_run 并显式传 user_id=
    context = {
        "user_id": user_id,              # 显式，不靠 ContextVar
        "is_internal": True,
        "non_interactive": True,
    }
    return await start_run(..., user_id=user_id, context=context)
```

`non_interactive=True` → 工厂从 lead 工具集里拿掉 `ask_clarification`
（M6 ClarificationMiddleware 不注册；工具列表也不出现这个名字）。
**客户端传入的 `context.non_interactive` 在 normalize 里丢掉。**

`recursion_limit` 用 `scheduler.recursion_limit`（默认 1000，与网页 UI 对齐，
被 `max_recursion_limit` 夹住）。**每次 dispatch 现读配置**，YAML 热重载对
下一次调度 run 生效，不必重启 poller。

### 4.2 两张表，两种状态机

**任务定义** `scheduled_tasks`：用户拥有的计划。

```python
class ScheduledTaskRow(Base):
    id: str
    user_id: str                         # 存储 ID，AUTO 过滤
    thread_id: str | None                # context_mode=reuse_thread 时有值
    context_mode: str                    # fresh_thread_per_run | reuse_thread
    title: str
    prompt: str
    schedule_type: str                   # cron | once
    schedule_spec: dict                  # {"cron": "0 * * * *"} | {"run_at": iso}
    timezone: str                        # IANA，非法值拒绝
    status: str                          # enabled | paused | running | completed | failed
    overlap_policy: str                  # 只允许 enqueue；不要实现 skip
    next_run_at: datetime | None
    last_run_id / last_thread_id / last_error
    lease_owner / lease_expires_at       # 抢"到期任务"的短租约
    run_count: int
```

**occurrence** `scheduled_task_runs`：一次"该跑了"。**不是** Gateway run。

```python
class ScheduledTaskRunRow(Base):
    id: str
    task_id: str
    thread_id: str
    run_id: str | None                   # 绑定成功后才有
    scheduled_for: datetime
    trigger: str                         # scheduled | manual
    status: str                          # queued | launching | running | success | failed | cancelled
    lease_owner / lease_expires_at
    attempt_count: int
    error / started_at / finished_at
```

部分唯一索引，必须同时写在 ORM `__table_args__` 和 migration 里
（空库 bootstrap 走 `create_all`+stamp，不会跑那条 migration）：

```python
Index(
    "uq_scheduled_task_run_active",
    "task_id",
    unique=True,
    sqlite_where=text("status IN ('queued', 'launching', 'running')"),
    postgresql_where=text("status IN ('queued', 'launching', 'running')"),
)
```

**每个 task 最多一个非终态 occurrence。** 重复触发自然合并到那一行。

### 4.3 occurrence 状态机

```
queued  ──────→  launching  ──────→  running  ──→ (success|failed|cancelled)
(持久,重启存活)   (短租约声明)        (已绑定 Gateway run)
```

| 状态 | 含义 | 关键性质 |
|---|---|---|
| `queued` | 该跑了，但还没能启动 | **持久化，重启后存活** |
| `launching` | 某个实例声明了启动权 | 短租约；**唯一可以调用 `start_run()` 的状态** |
| `running` | 已经绑定到一个 Gateway `run_id` | 之后跟随 run 的生命周期 |

为什么需要 `launching`：从"决定要启动"到"run 真的创建出来"之间有一个窗口，
进程在这个窗口里崩溃的话，没有这个状态就无法区分"还没启动"和"可能已经启动了"。
租约过期让它自动回到 `queued`（`attempt_count += 1`），**不要**直接标 failed。

`once` 任务：launch 成功后任务级 status 先变成 `running`，等 Gateway run 终态再
`completed` / `failed`。在 launch 那一刻写成 `completed` 会在进程死亡时卡住。

手动 trigger 失败不得把仍在未来的 `once` 任务打成 `failed`（否则永远不会再被 claim）。
paused 任务抑制自动 occurrence，但允许手动 trigger；手动跑完后任务回到 `paused`。

### 4.4 忙时排队，不是跳过

早期实现很容易写成"上一次还在跑就跳过这次"。这个语义对用户是错的——
一个每小时跑一次的任务，如果某次跑了 70 分钟，用户期望的是下一次紧接着跑，
不是被静默跳过。

所以忙时持久化为 `queued`，由 `scheduler.queue_timeout_seconds`（默认 3600）
约束等待上限。超时 → occurrence `failed`，`error=scheduled task queue wait timeout exceeded`。

**等待中的行不占并发预算。** `max_concurrent_runs` 只计 `launching` + `running`。
让 `queued` 也占预算的话，排队本身会把预算吃光，谁也跑不了。
预算由**原子的队列声明**强制（Postgres advisory lock 或
`claim_queued_run(..., global_max_concurrent_runs=)` 里 `COUNT(*)` 条件更新）。

`overlap_policy` 配置只接受 `enqueue`。不要实现 `skip`。测试里出现 skip 分支就失败。

### 4.5 恢复要用同一个幂等键

每个 occurrence 提供一个**稳定的**运行准入幂等键，例如
`f"scheduled:{occurrence_id}"`。恢复后的重试把这个键传给 M3 `start_run`
的 `idempotency_key`，复用同一条 Gateway run，而不是产生第二条。

`fresh_thread_per_run` 时每次 dispatch 新 thread（走 M3 的 thread id 合同
`^[A-Za-z0-9_-]{1,64}$`）。`reuse_thread` 时打到任务上的 `thread_id`，
走普通的单活跃 run 约束——thread 忙则 occurrence 退回 `queued`。

### 4.6 失败分类

| 启动失败的原因 | occurrence 去向 |
|---|---|
| 线程忙（`ConflictError` / HTTP 409） | 退回 `queued`，稍后重试 |
| 其它错误（配置坏、模型没有、权限） | 终态 `failed` |

把所有错误都退回 `queued` 会让一个配置错误的任务永远重试。

Gateway run 终态通过 `on_run_completed` 回调进调度器：
`success` / `error` / `interrupted` 映射到 occurrence 终态，并推进 cron 的 `next_run_at`。
`once` 不再排下一次。

调度器自己的租约回收 `stop_reason` 用 `scheduled_task_orphan_recovered`，
不要和 Gateway run 的 `orphan_recovered` 混名——查日志时分不清是哪一层丢的。

### 4.7 活跃状态期间任务定义不可变

`queued` / `launching` / `running` 三个状态下，任务定义（提示词、模型、计划、
`thread_id`、`context_mode`）都必须冻结。

队列准入、修改、暂停、删除这四条路径要串行化（按 `task_id` 的事务或行锁）。
否则一个正在 `queued` 的 occurrence 会用改过的定义启动，而用户以为自己改的是下一次。

API：修改 enabled 且无活跃 occurrence 的任务可以立刻改 `next_run_at`；
有活跃 occurrence 时返回 409，body 写明"等这次结束或先取消 occurrence"。

### 4.8 cron 计算

`scheduler/schedules.py` 纯函数，无 IO：

- 5 段 cron，`croniter`，在任务 timezone 的墙上时间取 next，再存 UTC
- `once.run_at` 无 tzinfo = 任务 timezone 的墙上时间；持久化前转到 UTC
  （SQLite 列丢 tzinfo 时，带着 offset 存会把触发时刻平移整个 offset）
- 未知 IANA timezone → `ValueError`，创建任务时 400，不要静默用 UTC

poll 间隔建议 5–10s。`claim_due_tasks(now, lease_owner, lease_seconds, limit)`
原子地把到期且 enabled 的任务标上自己的 lease，再 `dispatch_task`。

`multi_instance=true` 时 poller 每次先 reconcile 过期 launching（回 queued），
但启动后的第一轮可以 skip——刚 recover 过。single-flight 与 9B 同一纪律。

### 4.9 前端

M5 加一页 `/workspace/scheduled-tasks`：列表、cron 编辑、暂停、手动 trigger、
最近 occurrence 状态。权限：`threads:write` + `runs:create`。
本阶段页面可以简陋，但 API 合同要稳定。

---

## 5. 9D IM 通道（W2 骨架，W3 两个平台）

M9 实现通道**架构** + **两种投递策略各一个平台**，不要六个平台一起上。

| 策略 | M9 平台 | 调用 |
|---|---|---|
| 等最终回复 | Slack（或 Discord） | `runs.wait()` |
| 流式改卡片 | Telegram（或飞书） | `runs.stream()`，原地 edit |
| 发射后不管 | **不做**（GitHub 那种） | 需要忙时缓冲，多 worker 有已知缺口 |

Feishu / DingTalk / Buzz / GitHub / WeChat 列为后续增量。架构要让加一个
`Channel` 子类不必改 manager。

### 5.1 一开始就要定的：能发出去的东西是**白名单**

**这是整个 M9 里最值得强调的一条。**

通道要从流里挑出"助手说的话"发给用户。很自然的写法是排除掉不该发的：

```python
# ✗ 黑名单：排除工具消息，其余都发
if "tool" not in payload.get("type", ""):
    publish(text)
```

这是一个**真实发生过的数据泄漏**。M6 的中间件会往消息流里注入隐藏 `HumanMessage`：

- `DynamicContextMiddleware` 把召回的记忆注入为隐藏 HumanMessage（`type == "human"`）
- 还会把用户自己的那一轮改写成新的 HumanMessage
- `DurableContextMiddleware` 注入 `<durable_context_data>` 隐藏块

这些都不含 `"tool"`，于是全部被发到了群里。实际后果：一个 IM 群收到了用户的
记忆事实块，另一次收到了用户自己消息的原样回显。

```python
ASSISTANT_PREFIXES = ("ai", "assistant")

def is_assistant_payload(payload: dict) -> bool:
    kind = stream_payload_type(payload)   # 见下
    return kind == "ai" or kind.startswith("ai") or kind == "assistant" or kind.startswith("assistant")
```

三条实现细节：

- **按前缀匹配，绝不用子串**。普通英文词里就含 `ai`（`chain`、`domain`）
- 同时认 `AIMessage` 的 `"ai"`、流式块的 `"AIMessageChunk"`、外部运行时的 `"assistant"`
- **裸字符串 payload 一律拒绝**：它不带类型信息，无法归属给助手

`stream_payload_type` 要同时处理 `model_dump()` 形状和 LangChain `to_json()`
constructor 形状（顶层 `type` 是字面量 `"constructor"`，类名在 `id` 路径末尾）。

**在连接器 `send()` 再加一道防御**：发送前检查文本里有没有隐藏上下文包装标记
（`<memory>`、`<durable_context_data>`、`<system-reminder>`），有就拒绝、打 ERROR、
清掉这条 stream 的 bookkeeping。对于消息不可撤回的平台这是最后的挽救机会。
匹配字面开标签，谈论 memory 的正常回复仍可发送。

这条测试每个平台都要跑，泄漏是按通道实现的。

### 5.2 通道跑在 Gateway 进程里，用内部 HTTP 调自己

不要让通道直接 `create_agent().astream()`。它必须走和浏览器相同的
LangGraph-compatible HTTP（`start_run` / `stream` / `wait`），这样线程、取消、
事件账本、鉴权是同一套。

SDK 客户端注入 §2.9 的内部 token + CSRF 对。`langgraph_url` 在 Compose 里是
`http://gateway:8001/api`（通道和 API 同容器时 localhost 也可）。

### 5.3 身份穿透要走显式参数

一条入站消息带三样东西：

| 字段 | 含义 |
|---|---|
| `connection_id` | 用户绑定 |
| `owner_user_id` | DeerFlow 存储 ID，**这次 run 的 user_id** |
| `channel_user_id` | 平台上的原始发送者 ID（群里每人不同） |

**owner 必须通过显式的 `user_id=` 一路穿透**：`receive_file`、`ensure_uploads_dir`、
产物投递、记忆桶、`start_run(user_id=)`。通道 worker 跑在自己的 task 里，
请求 ContextVar 在那里是空的或者是错的。少传一处，文件落到 default 桶。

`channel_user_id` 两条纪律：

- **只从内部认证调用方的顶层 `body.context` 接受**，从自由形式 `body.config` 一律清除
- **只写进 runtime.context，绝不写进 `configurable`**——后者会进 checkpoint，
  等于把群里某个人的平台 ID 永久写进对话历史

sandbox `bash` 若要暴露这个 id，用命令串前缀 `export DEERFLOW_CHANNEL_USER_ID=...;`
（无效/超长则 `unset`），**不要**走 `execute_command(env=)`——那会把 AIO 切到
fresh session。这是信息性的，技能不得把它当鉴权身份（bash 能改自己的环境）。
M8 `task` 已要求把 dispatch 当时的 `channel_user_id` 拷进子代理。

### 5.4 绑定码的安全模型

用户在浏览器点"绑定 Telegram"，拿到一次性码，在聊天里发 `/start <code>`
（Telegram）或 `/connect <code>`（其它）。

**绑定码的消费必须发生在 `allowed_users` 过滤之前**，否则一个刚被加进白名单
但还没绑定的用户永远没法完成第一次绑定。

推论：**`allowed_users` 不是绑定期的防御**。任何持有有效码的人都能消费它。
绑定的安全性完全建立在码的机密性上：

- `secrets.token_urlsafe(16)`
- TTL 600 秒
- 一次性 `consume`
- **只在发起绑定的那个浏览器里显示，绝不回显到聊天里**

`allowed_users` 仍然守着普通消息。空 allowlist 的语义按平台写清：
运营 bot 默认空=全放行；若做 Buzz 那种空=拒，`start()` 要 WARNING。

### 5.5 一个外部身份只能有一个活跃 owner

键：`(provider, external_account_id, workspace_id)`。
**最新一次成功绑定获胜**，绑定时撤销其它 owner 的活跃行。

数据库 partial unique index 强制，不能只靠应用层：

```sql
CREATE UNIQUE INDEX uq_channel_connection_active_identity
  ON channel_connections (provider, external_account_id, workspace_id)
  WHERE status != 'revoked';
```

两个 owner 并发绑定同一个身份时只有一个能成功，输的那个针对已经可见的新状态重试。

### 5.6 流式投递的限流与终态

Telegram 群 20 条/分钟。通道侧节流：私聊 1s、群 3s；超长截断（4096）后最终再 split。
被限流丢掉的中间更新可以接受，**最终那一次必须成功**。

Feishu 同类：先发一张 running 卡片，随后 patch 同一 `message_id`。
M9 若只做 Telegram，把"同一 target 上 edit"做成 `Channel` 的能力，而不是写死在 Telegram 里。

同一 thread 上快速跟进：`ChannelRunPolicy.serialize_thread_runs=True` 时
manager 内排队，而不是回一条 THREAD_BUSY 让用户以为丢了。
这个队列是**进程内**的——多 worker 下有已知缺口，写进文档，M9 不修
（需要共享缓冲或通道 leader 选举）。

### 5.7 入站文件

下载上限 20_000_000 字节，读的时候多读 1 字节用于拒绝。
token 不得出现在传给 manager 的 URL 里；字节只走瞬时字段，持久化之前剥掉。
写入走 `write_upload_file_no_symlink`，owner 桶。
非 mounted 沙箱同步用 M8 的 non-releasing 请求租约，取消时排空。

### 5.8 关闭

`Channel.stop()`：先关 manager 准入，等所有 worker / follow-up watcher 退出，
再拆 transport。SDK 线程回调提交协程必须用 `run_coroutine_threadsafe` 的**真 Task**
留在 owner loop 上，不要把 proxy Future 当完成信号。
Gateway 超时取消 shutdown 时，service 保留 channel 单例以便重试 cleanup，
不要把未完成的资源拆掉。

---

## 6. 9E MCP 长时任务（默认跳过）

M7 已把它列为不做。只有确实存在跑几十分钟的远端工具时才开工。形状：

- agent 只负责 submit；包装器持久化远端句柄后只返回本地任务 ID
- 状态查询 / 取消工具对模型隐藏
- 数据库是真相源；租约轮询在 Agent/LLM 循环之外
- `ThreadState` 只收到有界的当前线程投影
- 远端完成后 Gateway 开一次**通知 run** 把结果说回对话；投递指令放在用户输入边界之外，
  序列化的远端事件当不可信数据 framing
- 任务比聊天活得长：thread 已删则 dead-letter，不要为通知重建 thread

三条容易错的（真做时测试钉住）：

1. 租约过期是重启恢复机制。过期后才返回的状态结果必须丢弃，**即使 owner token 还匹配**
2. 轮询时间戳基于远端调用的**完成时刻**，不是扫描开始时刻
3. 提交成功但持久化失败时要尽力取消远端任务；补偿失败时保留原始错误

---

## 7. 配置形状

```yaml
auth:
  enabled: true
  local:
    max_login_attempts: 5          # 热重载，最小 2
    lockout_seconds: 300
  jwt_ttl_seconds: 86400

runs:
  lease_seconds: 30                # STARTUP_ONLY
  lease_grace_seconds: 10
  multi_instance: false            # STARTUP_ONLY；或由 GATEWAY_WORKERS>1 推断

stream_bridge:
  backend: memory                  # memory | redis；多 worker 强制 redis
  redis_url: redis://localhost:6379/0
  stream_ttl_seconds: 3600
  queue_maxsize: 256

scheduler:
  enabled: false
  poll_interval_seconds: 10
  lease_seconds: 30
  max_concurrent_runs: 2           # STARTUP_ONLY；只计 launching+running
  queue_timeout_seconds: 3600      # STARTUP_ONLY
  recursion_limit: 1000            # 热读
  multi_instance: false            # STARTUP_ONLY

channels:
  langgraph_url: http://127.0.0.1:8001/api
  slack: { enabled: false, bot_token: $SLACK_BOT_TOKEN, app_token: $SLACK_APP_TOKEN }
  telegram: { enabled: false, bot_token: $TELEGRAM_BOT_TOKEN }

channel_connections:
  enabled: false                   # 用户绑定层；不替代 bot 凭据
```

环境变量：`DEER_FLOW_INTERNAL_AUTH_TOKEN`（必填当 auth.enabled 且通道/调度开启）、
`GATEWAY_WORKERS`、`GATEWAY_CORS_ORIGINS`、`AUTH_TRUSTED_PROXIES`、
`GATEWAY_ENABLE_DOCS`。

---

## 8. alembic

M4 的混合 bootstrap 仍然适用。M9 新增 revision（不要改已 stamp 的旧 revision）：

| revision | 内容 |
|---|---|
| `00xx_users` | `users` 表 + admin 部分唯一索引 |
| `00xx_run_lease_live` | 若 M4 已有租约列则空；补 `cancel_action` / `cancel_requested_at` / `stop_reason` |
| `00xx_scheduled_tasks` | 两张表 + `uq_scheduled_task_run_active` |
| `00xx_channel_connections` | connections + conversations + `uq_channel_connection_active_identity` |
| `00xx_pat` | W4 可选；只存 SHA-256 digest |

`create_all` + stamp 路径必须在 ORM `__table_args__` 里带上那两个 partial unique index。

---

## 9. 测试清单

| 文件 | 覆盖 |
|---|---|
| `test_identity_strip.py` | 客户端 `user_id` / `owner_id` 在 metadata/configurable/context 三处都被剥；internal 头未经 AuthMiddleware 当不可信 |
| `test_identity_boundary.py` | 优先级链；**图构建和中间件落在同一个桶**（两个用户交错请求） |
| `test_make_safe_user_id.py` | 邮箱 → 稳定目录安全 id；已安全则原样；不同输入不共桶 |
| `test_repository_auto_scope.py` | `AUTO` 无上下文抛错不退化全局；显式 `None` 才全局；跨用户 get 返回 None |
| `test_auth_gate.py` | OpenAPI 里非白名单默认 401 `unauthenticated`；cookie 存在但 tv 不匹配 401；JWT 不出现在响应体；PAT 打 change-password → 403 `session_required` |
| `test_m9_routes.py` | initialize 第二次 409 `admin_already_exists`；登录失败不区分邮箱和密码；调度 PATCH 在活跃 occurrence 上 409 `task_active`；trigger 不调用 `start_run`；别人的 task/run 是 404；反馈 PUT 后再 GET 是覆盖；绑定响应含码，GET /channels 不含码和 token |
| `test_csrf.py` | POST 缺 header 拒；bootstrap 不要求 token 但拒 hostile Origin；坏 Bearer 不回退 cookie |
| `test_token_version.py` | 改密后旧 cookie 立刻 401；新 cookie 可用 |
| `test_owner_404.py` | 访问别人的 thread/run/artifact 一律 404 |
| `test_ownership_lost.py` | **丢租约后不写终态、不发业务 SSE、不 flush journal**；终态守卫挡住迟到的 success |
| `test_orphan_recovery.py` | 两个进程同时认领只有一个成功；扫描不是权限；single-flight 慢轮跳过 |
| `test_cancel_cross_worker.py` | 有效租约 → 202 + request_cancel 不转移 owner；过期 → claim 后本地取消 |
| `test_multi_worker_preflight.py` | 缺 Postgres / 缺 DB 事件存储 / 心跳关 / auth 关 → 拒绝启动 |
| `test_redis_stream_bridge.py` | Last-Event-ID、GAP、publish_end、heartbeat 无 id、TTL 刷新；与 memory 实现同一套协议测试 |
| `test_shutdown_order.py` | 先收 run 再停心跳；倒过来的负例会把本进程 run 标 ownership_lost |
| `test_occurrence_state_machine.py` | 唯一非终态约束；launching 过期回 queued；忙时排队不跳过；queued 不占预算；once 手动失败不毁未来 |
| `test_scheduled_idempotency.py` | 恢复重试复用同一条 Gateway run；`start_run` 被调用而不是内部 astream |
| `test_non_interactive.py` | 内部调用去掉 ask_clarification；客户端塞的键被丢 |
| `test_task_definition_freeze.py` | 活跃 occurrence 期间 PATCH 409 |
| `test_channel_outbound_allowlist.py` | **隐藏 HumanMessage 不外发**；裸字符串拒绝；前缀匹配不误伤 `domain`；`<memory>` 在 send() 被拒 |
| `test_channel_identity.py` | owner 显式穿透到上传/产物；`channel_user_id` 不进 checkpoint / configurable |
| `test_connect_code.py` | 绑定码在 allowed_users 之前消费；不回显聊天；并发绑定只有一个活跃；TTL / 一次性 |
| `test_cron_timezone.py` | naive run_at 按任务 tz 解释后存 UTC；非法 tz 400 |

`test_ownership_lost.py` 和 `test_channel_outbound_allowlist.py` 是这一阶段
最值得写的两个——它们各自对应一个会造成**不可恢复后果**的错误。

TDD：W1 先写身份剥离和 AUTO fail-closed；W2 先写 ownership_lost 负例
（模拟 journal.close 在标志已立时仍 flush，必须失败）；W3 先写
"调度器不得直接 astream"的探测（mock `start_run`，assert 被调用）。

---

## 10. 验收清单

- [ ] 两个用户各自的 thread、文件、记忆完全隔离；访问对方的 thread 返回 404
- [ ] 改密码后，旧浏览器标签页的请求立刻被拒；响应 JSON 里看不到 JWT
- [ ] **两个 Gateway 实例并行，`kill -9` 掉持有某个 run 的那个，
      另一个在租约过期后接管、标 `error`、给客户端补 `end`**
- [ ] 被 kill 的那个进程如果恢复，不会再写任何终态（即使 agent 协程还跑完了）
- [ ] 一个每分钟触发、单次跑 3 分钟的定时任务：后续触发排队而不是跳过，重启后队列存活
- [ ] 定时任务跑起来的 run 走的是和网页完全相同的 run 路径（同一套事件、同一套取消、同一套 token 账本）
- [ ] 调度 run 的工具集没有 `ask_clarification`；网页 run 塞 `non_interactive` 无效
- [ ] IM 群聊里连发三条消息，不会漏回、不会串线程
- [ ] **开启记忆后在 IM 里对话，群里看不到任何 `<memory>` / `<durable_context_data>` 内容**
- [ ] 新加一条 `/api/foo` 路由，不改白名单，未登录访问是 401 `unauthenticated`
- [ ] `POST /api/v1/auth/login/local` 的响应体和 `Authorization` 头都没有 token；cookie 里有
- [ ] `PUT /api/runs/{id}/feedback` 对别人的 run 是 404 `run_not_found`

第三条要真的用 `kill -9` 做，不要用优雅关闭——优雅关闭走的是完全不同的代码路径。
最后第二条建议每个已实现的通道都跑一遍。

---

## 11. 与已有里程碑的接口对照

| 已有槽 | M9 填什么 |
|---|---|
| M0 `STARTUP_ONLY_FIELDS` | `runs.lease_seconds`、`stream_bridge.backend`、`scheduler.max_concurrent_runs` 等 |
| M0 `make_safe_user_id` | 全链路强制；原始 identity 与存储 ID 分离 |
| M2 虚拟路径 / `acquire(user_id=)` | 宿主机改 `users/{uid}/threads/{tid}/...`；agent 仍看 `/mnt/user-data` |
| M3 `RunRecord.ownership_lost` 等 | 心跳真正写租约；丢了就跳过 finally 持久化 |
| M3 `start_run` / 幂等键 | 调度器唯一派发入口；occurrence 键复用 |
| M3 StreamBridge 接口 | Redis 实现；`publish_end` / GAP / Last-Event-ID 不变 |
| M4 租约列、终态守卫、`AUTO` | `claim_for_takeover` 替换单 worker 的一锅 orphan 回收 |
| M4 `recover_inflight_runs` | 多实例下禁止；会误杀 peer 的 inflight |
| M5 `/workspace/scheduled-tasks` | 接本阶段 API |
| M6 Clarification / DurableContext 隐藏块 | `non_interactive` 去工具；通道出站白名单防泄漏 |
| M7 请求级密钥不进 checkpoint | `channel_user_id` 同纪律 |
| M8 `channel_user_id` 拷进子代理 | 9D 群聊身份不断档 |

---

## 12. 刻意不做的事

| 不做 | 理由 |
|---|---|
| 细粒度 RBAC / 工具级 deny | 先只有 `admin` / `user`。`authz.provider` 接口留下，内置 AllowAll |
| OIDC/SSO | W1 阻塞项是本地账号。OIDC 是同一套 cookie + users 表的增量 |
| 跨进程的 IM 忙时缓冲 | 需要共享缓冲或通道 leader 选举。单进程没有正确性问题，写进已知边界 |
| 分布式登录限速 | 进程内计数在单 worker 精确、多 worker 近似 |
| Kubernetes / 微服务化 | 路线图岔路第 3 条。Gateway 仍是单体，只是可以水平加副本 |
| 9E MCP 长时任务 | 整套租约 + 轮询 + 通知 run。除非真有几十分钟的远端工具 |
| IM 双向富交互（按钮、表单） | 每个平台一套 API |
| 六个 IM 平台一次上齐 | M9 两种投递策略各一个平台；其余按相同 Channel 接口后加 |
| GitHub fire-and-forget 通道 | 依赖忙时缓冲，多 worker 缺口与上一条同类 |
| 调度 `overlap_policy=skip` | 已证明对用户是错的；配置枚举里不要出现这个值 |

---

## 13. 一句话总结

M9 四条主线各有难点，但只有两个错误是**不可恢复**的：

1. **丢租约的进程继续写终态** → 数据库和客户端永久不一致，没有任何机制能修
2. **通道用黑名单决定什么能外发** → 用户的记忆和隐藏上下文泄漏到外部平台，
   在不可撤回的平台上永久留存

W1 把身份权威来源定死，W2 把这两条不可恢复错误用测试钉死，
W3 让调度器和通道都只当 `start_run()` 的 caller。其余的（身份桶错了、
调度重复触发、绑定冲突）都能靠修数据恢复。

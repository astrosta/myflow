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

### Checkpointer 后端

Gateway 的 checkpointer 由 `database.backend` 决定（memory / sqlite / postgres），和 ORM 共用
`database.url`，构建后先跑 `setup()` 再交给图。`AgentClient` / CLI 的同步路径由
`checkpointer.type` 决定；选 `sqlite` / `postgres` 时同样读取 `database.sqlite_dir` /
`database.url`，`postgres` 还要求 `database.backend: postgres`。两处都只支持进程内单例或
一次性上下文，换后端要重启。

沙箱段见 `config.yaml` 的 `sandbox`。本机 Local provider 默认不开宿主 bash；可信环境可设 `allow_host_bash: true`。

### CubeSandbox 宿主挂载（Linux 同机部署）

默认仍是本地 provider。要在 CubeSandbox MicroVM 里跑工具，把 `sandbox.use` 切成
`agentkit.sandbox.cube:CubeSandboxProvider`，并启用 `sandbox.cube`（`config.example.yaml`
里有整段注释模板）。这是**宿主挂载**，不是第二套工作区：CubeSandbox 通过 bind mount 看到同一份
`.myflow/users/{user_id}/threads/{thread_id}/user-data`；上传和产物的 `thread_files.py`
链路完全不变，`/mnt/user-data/uploads`、`/mnt/user-data/outputs` 就是宿主上那两个目录。

前置条件（缺一条都会在启动或第一次 acquire 失败）：

1. **Gateway 与 Cubelet 在同一台 Linux 主机**，看到同一个项目根。`host_mount.local_root` 和
   `host_mount.host_root` 默认都取 `project_root()`（`MYFLOW_PROJECT_ROOT` 或 Gateway 的工作目录），
   同机部署时两项都可以省略。这个根必须同时覆盖 `.myflow`（thread 数据 + Git 技能缓存）和
   `skills/`（内置技能），所以默认是整个项目根，不是一个单独的数据盘目录。
2. CubeMaster 的 `allowed_host_mount_prefixes` 必须包含这个项目根。CubeMaster 默认只允许
   `/data/shared/`；创建时报 `hostPath ... is not within an allowed mount prefix` 就是这里没配。
3. 模板已预创建 `/mnt/user-data` 和 `/mnt/skills` 下的技能路径
   （`skills.container_path` 默认 `/mnt/skills`），并提供 `bash`、`python3`、`tar`、`grep`、
   `find`、`sed`、`dd`、`base64`。建议模板 umask `0022`。`workspace`、`uploads`、
   `outputs` 三个子目录由宿主线程目录提供，每次 acquire 前创建。
4. Gateway 进程对项目根有读写权限，沙箱内（默认 `sandbox_user: root`）写出的文件必须能被
   Gateway 读和删。每次 acquire 的 canary 就在验证这一点：宿主机写 token → 沙箱读，
   沙箱写 token → 宿主机读，探针放在 `workspace/.myflow-canary/`，成功后删除。
   失败会 kill 新沙箱并报 `host mount canary failed`，先查 `host_mount`、模板 umask 和目录权限。
5. 装依赖：`cd backend && uv sync --extra cube`（backend 的 `cube` extra = `agentkit[cube,redis]`，
   提供 `cubesandbox>=0.7.3,<0.8` 和 `redis>=5`）。如果直接装 harness 的 extras，
   `agentkit[cube]` 不够：per-thread 锁在 `redis` extra 里，必须 `agentkit[cube,redis]`。
6. Redis 从 Gateway 可达，`sandbox.cube.lease.redis_url`（生产用 `$REDIS_URL`）指向它；
   多 worker 必须共用同一个实例/DB。启动时 Redis `PING` 失败直接拒绝启动；运行中续租失败会把
   handle 标成 dead，不会带着失效的锁继续写。

配置要点：

- `sandbox.use`、整个 `sandbox.cube`、`sandbox.mounts`、`skills.container_path`、
  `skills.builtin.*`、`skills.git.cache_dir`、`skills.source` 都是 **startup-only**，改完重启。
- `release_action` 默认 `pause`：`after_agent` 每轮跑完都会释放，kill 会变成"每条用户消息
  冷启动一个 MicroVM"。pause 保留内存和进程状态，下一轮 `connect()` 自动恢复，宿主文件仍在。
  `shutdown()` 始终 kill。
- `on_timeout` 默认 `pause`，`auto_resume: true`：空闲到 `timeout_seconds` 暂停，下次请求自动恢复。
  集群配额紧张时可以把 `on_timeout` / `release_action` 改成 `kill`，宿主数据不受影响。
- `allow_public_traffic: false` 让 CubeProxy 拒绝未带 traffic token 的公网请求；SDK 会自动带。
- 宿主挂载清单（`/mnt/user-data` 读写，技能树只读）和 mount hash 写进沙箱 metadata。
  配置或技能路径变了，下一次 acquire 会发现 hash 不一致并重建旧沙箱，不会带着过期挂载继续跑。

**回滚**：把 `sandbox.use` 改回 `agentkit.sandbox.local:LocalSandboxProvider`，重启 Gateway 即可。
不需要数据库迁移，也不需要移动宿主文件：local 与 cube 共用同一份 `user-data` 和技能树。
旧 checkpoint 里的 `cube:` sandbox_id 在回滚后 `get()` 返回 `None`，runtime 会重新 acquire
本地沙箱；反过来从 local 切到 cube 也一样。

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

### 持久记忆（M10 / OpenViking）

`memory.openviking.enabled: true` 是长期记忆的唯一开关：自动抽取的偏好/实体/事件等八类记忆
存 OpenViking，MyFlow 只保留运行所需元数据表。用户画像（profile）是数据库里的身份资料
（`memory_profile_documents`），**不随这个开关关闭**：

| 面 | 说明 |
|---|---|
| `GET /api/memory/profile` | 当前画像字段；行不存在返回空字段（不 404） |
| `GET /api/memory/profile/schema` | 全部受支持字段的元数据，前端按它渲染全字段表单 |
| `PATCH /api/memory/profile` | 部分更新；`null` 删除字段；列表整列表替换；etag CAS |
| `GET/POST /api/memory/profile/suggestions` `/onboarding` | 画像建议与引导 |
| `GET /api/memory/items/types` | 记忆管理支持的六类记忆与中文名 |
| `GET /api/memory/items` | 按类型列记忆摘要（OpenViking 过滤型 `find`，不落库） |
| `GET /api/memory/items/detail` / `content` | 详情目录（`.overview.md`）与记忆正文（`read`） |
| `PUT /api/memory/items/content` / `DELETE /api/memory/items` | 编辑/删除具体记忆（`write` / `rm`，sidecar 只读） |

记忆管理页（前端入口 `/workspace/memory`，侧栏“记忆管理”）的查看/编辑/删除直接调用 OpenViking 协议
（`find` / `overview` / `read` / `write` / `rm`），MyFlow 不保存记忆副本，`.abstract.md` / `.overview.md` 只读。

画像在 run assembly 时渲染进系统提示的 `<user_profile>` 段；记忆正文由内置子代理
`memory-researcher` 通过只读工具 `memory_search` / `memory_read` / `memory_overview` 检索，
lead 只拿结论与来源（`2026-11-02-retrieval-subagents-design.md`）。写入只有两个入口：用户在“我的画像”
表单里保存，或用户在对话里**显式**说出画像信息后由内置工具 `update_user_profile` 写入
（模型推断不写画像；工具与表单共用同一行和 etag）。

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
正文一律按需：`read_file` 读 SKILL.md、委派内置子代理 `skill-finder`（内部调
`search_and_load_skills`，只回候选 name/path/description）后由 lead 读正文，或 `/skill-name` 注入
—— 没有"全量注入"模式开关。
已激活的技能由 `<active_skills>`（在 `<durable_context_data>` 里）投影：名字、描述、目录，
外加**核心 SOP 正文**。正文不进 state，每次投影按 `skill_context` 台账里的 path 现读，
预算见 `skills.active_context`；正文还在消息里时默认不重复写（压缩之后它才是唯一副本）。
`search_and_load_skills` 是两段式（本地 BM25 召回 `skills.search.candidate_k` → 可选精排
`top_k`），默认只有本地通道；`skills.search.embedding` / `rerank` 不填地址就不发请求，
地址和模型默认取顶层 `embedding` / `rerank`（section 里的同名字段可覆盖），失败也只降级
（退回 BM25 / 融合顺序）并在 `retrieval.warnings` 里说明。加载即激活：
结果里的 `path` 和 `read_file` 读 SKILL.md 走同一本 `skill_context` 台账。
技能 Git 写入使用 **pygit2/libgit2**，运行时不依赖系统 `git` 可执行文件；仓库镜像、
树/commit/push 都在进程内完成。SSH 认证使用 `skills.git.ssh_key`，值是 **base64 编码的
OpenSSH 私钥内容**（通常从 `.env` 的 `SKILLS_GIT_SSH_KEY` 展开）；不会生成临时私钥文件。
libgit2 的 SSH transport 是构建期特性：如果当前 pygit2 wheel 没有 libssh2 支持，服务会在
初始化 Git 仓库时明确拒绝 SSH URL；此时需要换带 SSH 的 pygit2 构建，或改用 HTTPS 仓库。

技能是**两层开关**：`skills.source: git` 时全局开关写 PostgreSQL 的 `skill_settings`
（按内部 `skill_id`，admin），个人偏好仍写 `skills_prefs.json` 的
`{user_id: {skill_id: bool}}`（任何登录用户），有效状态 = 两者相与。旧的
`extensions_config.json` 技能开关与按名字的偏好用 `backend/scripts/migrate_skill_state.py` 迁移。

**内置技能是例外**（`skills.builtin`，默认 `skills/builtin`）：随项目发布、只读，
不进 Git、不进 PostgreSQL，也不需要数据库迁移。`skills/builtin/<name>/SKILL.md`
放进去就被扫描进索引（按 SKILL.md 的 mtime/size 做目录指纹缓存，
`POST /api/skills/reload` 可显式失效）；`enabled` 与 `path` 都是 **startup-only**：
只读挂载在 Sandbox provider 构造时固定，改完要重启进程。`enabled: false` 时完全不扫描、
不索引、不挂载；目录缺失或读不了只在首次读取时 warning 一次，进程照常起来。
`POST /api/skills/reload` 会清空这个去重标记，重载后如果目录仍不可用会重新告警一次。
它们**没有个人开关，也没有管理员全局开关**：`skills_prefs.json` 的同名 false 与
`extensions_config.json` 的同名条目都不生效，部署级开关只有 `skills.builtin.enabled`；
`subagents.custom_agents.<name>.skills` 白名单仍然生效（`subagents.agents.<name>` 只覆盖超时/轮次/
模型/token 预算，不含 skills），`/name` 与 `read_file` 照常解析，同名时内置优先、同名的 catalog
行被隐藏并 warning（同名冲突告警也是进程级 warn-once，reload 时重置；新建草稿和审核通过都会拒绝内置保留名）。
加载只有渐进式两条：索引行（方括号标 `[builtin]`）→ `read_file` 读
`<container_path>/builtin/<name>/SKILL.md`，或 `/name` 激活；**内置技能不参与
`search_and_load_skills`**（filesystem 分支检索 `visible(include_builtin=False)`，
Git 分支的检索服务从不接收内置技能），关键词通道开着也不会命中它。
沙箱里 `<container_path>/builtin` 是宿主 `skills.builtin.path` 的**只读**映射，
文件工具写不进去，`sandbox.mounts` 也不能覆盖 `<container_path>` 下的路径
（宿主 bash 仍是已知的非隔离边界）。`GET /api/skills` 的内置行排在最前
（`scope` / `category` / `status` 都是 `builtin`，`skill_id`、版本、commit、path 为 null）；
Git runtime 缺失时 catalog 那一半照旧 fail-closed，内置行照样返回；
`/api/skills/mine` 不含内置技能，技能页把它们放在只读的「内置技能」区块：
没有编辑按钮、没有开关，也不跳编辑器。

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
| 用户名形状不对 | 422 | `invalid_username` | 是哪一条（3-32 位 / 字符集 / 首尾 / 保留字） |
| 用户名被占 | 409 | `username_taken` | 是哪个用户名，让你换一个 |
| 口令太短 | 422 | `password_too_short` | 最少 8 字符 + 实际几位 |
| 口令太长 | 422 | `password_too_long` | 上限 72 **字节**（UTF-8）+ 实际字节数 |
| 自助注册关闭 | 403 | `registration_closed` | 让管理员建号 |
| 已有 admin 再走 setup | 409 | `admin_already_exists` | 提示去登录（**不点名**现有 admin，这条路由公开） |
| 没这个账号 | 401 | `account_not_found` | 回显你敲的用户名，提示先注册 |
| 密码不对 | 401 | `wrong_password` | 只说不对，**永不回显口令** |
| 账号被禁用 / 在回收期 | 403 | `account_disabled` / `account_deleted` | 找管理员恢复。**只在口令正确时才给这个答案** |
| 失败次数超限 | 429 | `login_rate_limited` | 剩余秒数 + 响应头 `Retry-After` |
| 改密时当前密码错 | 401 | `invalid_credentials` | 明确指出是 current password 的问题 |
| **服务端**读不出认证配置 | 503 | `auth_unavailable` | 让用户找管理员。**不是**"你没登录"——用户重登一百次也还是这个错 |

两条不变量：**明文口令永不回显**（也不进日志、不进库）；
**账号状态（禁用 / 回收期）只在口令正确之后才透露**——不知道口令的人不该读到"这个账号存在且被禁用了"。

想要"不透露某个用户名是否注册"就把 `auth.local.specific_errors` 设成 `false`：
登录的四种失败（没这个账号 / 密码错 / 被禁用 / 回收期）会收敛成同一个 401 `invalid_credentials`
和同一句 detail。代价是用户不知道为什么失败。

### 用户名：唯一登录标识

`users.username` 不是"显示名"，它是**唯一登录标识**（§2.4）：

- **形状**：3-32 字符，小写字母/数字/下划线/点/连字符，首尾必须是字母或数字；
  `default` / `anonymous` / `auth-disabled` / `system` / `internal` 是保留字。
- **大小写不敏感**：`Alice` 存成 `alice`，登录时敲 `ADMIN` 也进得去。
- **用户名唯一且大小写不敏感**：撞名是 409 `username_taken`。
- **改用户名不换桶**：桶名只来自 `users.id`（§2.2）。
- **邮箱不再是登录标识**：`users.email` 仅保留旧账号资料，可为空；新注册不要求邮箱。

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
- 忘记密码：`uv run python -m backend.app.gateway.auth.reset_admin [--username ...] [--credentials-file ...]`
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
### 知识库 collection 重置（父子块）

```bash
# 先看一眼会删哪两个 collection（默认就是 dry-run）
uv run python -m backend.scripts.reset_knowledge_milvus
# 真删（要求：Gateway 已停；SQL 元数据与上传文件先走删除知识库接口）
uv run python -m backend.scripts.reset_knowledge_milvus --apply
```

父子块这轮的 schema **不兼容**：老 `myflow_knowledge` 没有 `parent_id`，入库会直接抛
`KnowledgeUnavailable`。脚本删 `{collection}_parents` 和 `{collection}` 两套 collection，
只读 `config.yaml` 的 `vector_store` / `knowledge_base.vector_store` 两个块（不展开模型 key，
删 collection 不需要模型凭据）；父 collection 先删，中途失败不会留下孤儿父块。
父 collection 用 2 维占位向量满足“collection 必须含向量字段”——Milvus 拒绝 dim<2
（code 1100 `invalid dimension: 1`），父块全文只按主键读，不参与任何检索。

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
| `GET` | `/api/skills` | 技能目录，`enabled` 是**有效状态**（`skills:read`；`?scope=global` 仅 `admin`；`?scope=public` 只返回已发布公共技能） |
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
| `POST` | `/api/v1/auth/initialize` | 建第一个 admin（公开，且只在零 admin 时；要 username + password） |
| `POST` | `/api/v1/auth/login/local` | 本地登录（公开，响应体不含 token）。body 为 `username` + `password` |
| `POST` | `/api/v1/auth/register` | 自助注册（公开，受 `auth.allow_registration` 控制；username + password） |
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
`base_url` + `api_key`）、顶层 `embedding.base_url`、顶层 `vector_store.uri`
（或 knowledge_base section 内的显式覆盖）会拒绝启动。

知识库与技能检索共用同一套 embedding / rerank / Milvus 连接，配置放在顶层
`embedding`、`rerank`、`vector_store`；`knowledge_base.embedding`、
`knowledge_base.vector_store`、`knowledge_base.retrieval.rerank` 和
`skills.search.*` 只保留 collection、开关、超时、重试、指令等个性化字段，
同名字段仍可覆盖顶层值。`chunking.tokenizer.model` 默认也取顶层
`embedding.model`，需要局部 tokenizer 时再单独指定。

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
法律法规（`legal`）。默认 token 档位：技术 512/64，运维/安全/法律 512/0，
FAQ 384/0，API 768/0；硬上界 1024 token。FAQ 只对问题做 embedding，正文仍保存 Q+A。
运维/安全/API/法律超长逻辑单元使用父子块：父块存父 collection，子块带 parent_id；
检索命中子块后返回父块全文。分片和查询都走 Qwen3 tokenizer，不可用时会明确回退近似计数。

清洗在通用文本规范化后，确定性删除目录页、前言、版权声明、免责声明、修订记录和
重复块；原始上传文件保留。该版本的 Milvus schema 新增 `parent_id` 和
`{collection}_parents`；旧 collection 不兼容，必须删除现有知识库数据后重新入库。

`security` 与 `legal` 是两种东西：前者是**内部**安全/合规要求与控制项，后者是**外部**
法规原文；`legal_clause` 策略按 `第X条（之N）/ Article N / § N / 条款 N` 切分，
一条一个分片，条号写进 `section`，引用能直接落到“《XX 办法》第十二条”。

Agent 通过内置子代理 `knowledge-researcher` 使用只读 `knowledge_search`（lead 不再直接持有它；
子代理不可用时退回 lead 直连）。工具把 `knowledge_base_type` 作为参数，由模型按问题语义路由；服务端始终按 `user_id` 过滤，模型不能传身份。首版入库任务
在 Gateway 进程内运行，重启会中断；按单 worker 部署，多实例前需换成持久任务队列。

检索是混合检索（§7.4）：问题拆成 `query`（向量）与 `keyword_query`（BM25 关键词，
可省略、缺省时复用 `query`），两路各取 `retrieval.candidate_k` 条后由 Milvus 
`hybrid_search` + `RRFRanker(k=retrieval.rrf_k)` 融合；`retrieval.rerank.enabled=true`
时再把融合候选送给 rerank 模型精排（`protocol: cohere | tei`，模型默认取顶层
`rerank.model`）。关键词通道或精排不可用时按设计文档降级，并在返回值里
如实标注 `retrieval_mode` 与 `reranked`。

collection 需要 Milvus 2.5+ 的 BM25 能力：`content` 字段带 analyzer（默认 `chinese`）、
`sparse` 字段由 BM25 Function 自动生成、稀疏索引 metric 为 `BM25`。上线前的旧
collection 没有这些字段，关键词通道会报 `keyword_search_unavailable` 并降级为纯向量，
需要新建 collection 后重索引。`retrieval.keyword.analyzer` 是 destructive 参数。

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/api/knowledge/status` | 启用状态、限制、六类分片参数 |
| `GET`/`POST` | `/api/knowledge/bases` | 列 / 建知识库 |
| `GET`/`PATCH`/`DELETE` | `/api/knowledge/bases/{id}` | 详情 / 改名 / 级联删除 |
| `GET`/`POST` | `/api/knowledge/bases/{id}/documents` | 文档列表 / 批量上传（202） |
| `GET` | `/api/knowledge/bases/{id}/documents/{doc}` | 文档元数据 |
| `GET` | `/api/knowledge/bases/{id}/documents/{doc}/chunks` | 文档切片，按 `chunk_index` 排序并分页 |
| `POST` | `/api/knowledge/bases/{id}/documents/{doc}/reindex` | 重新解析 / 索引 |
| `DELETE` | `/api/knowledge/bases/{id}/documents/{doc}` | 删除文档与向量 |
| `GET` | `/api/knowledge/search` | 检索测试（`q` 向量 + `keyword` BM25、`type` / `base_id` 过滤） |

## 测试

```bash
# 必须在仓库根跑：测试 import backend.* 和 agentkit.*
uv run python -m pytest backend/tests
```

CubeSandbox 的真集群用例默认跳过，只在 Linux 宿主上显式开启（会创建真实 MicroVM 和宿主挂载，
不要指向共享生产 Cube）：

```bash
cd backend
CUBE_INTEGRATION=1 \
CUBE_API_URL=http://<cube-api> \
CUBE_TEMPLATE_ID=<template-id> \
CUBE_PROXY_NODE_IP=<node-ip> \
REDIS_URL=redis://127.0.0.1:6379/0 \
uv run pytest tests/test_cube_integration.py -v
```

有鉴权时再加 `CUBE_API_KEY`。用例跑真实 provider 的 acquire → `echo`/python3 →
`/mnt/user-data/workspace` 双向读写 → canary → pause/release → 重新 acquire。

## 排障

| 现象 | 处理 |
|---|---|
| `No module named 'backend'` | 在仓库根启动 |
| `No config file found` | `--env-file .env`，或设置 `MYFLOW_CONFIG_PATH` |
| `$DEEPSEEK_API_KEY is not set` | `.env` 里补上，并用 `--env-file` |
| `address already in use` | 换 `--port` 或杀掉占用 8001 的进程 |
| 登录页一直说"登录状态已失效" | 先确认网关进程不是旧的：**改完代码要重启 uvicorn**（README 里的 `--reload` 只在开发时开）。旧 cookie 从 9A 起不会再锁死登录页 |
| SSE 只有一帧 `event: end` | 订阅早于 worker 第一帧 `publish`；见 `MemoryStreamBridge.subscribe` 在 buffer 为 None 时直接 END |

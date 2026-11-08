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

### E2B（无宿主挂载，显式同步）

默认仍是本地 provider。要在 E2B 兼容沙箱里跑工具，把 `sandbox.use` 切成
`agentkit.sandbox.e2b:E2BSandboxProvider`，并启用 `sandbox.e2b`（`config.example.yaml`
里有整段注释模板）。E2B 协议没有 bind mount：**沙箱文件系统是权威**，宿主与沙箱之间只通过
文件 API 显式同步。

一次完整的往返：

1. **acquire**：在沙箱里 `mkdir -p /mnt/user-data/{workspace,uploads,outputs}`，然后把宿主
   `.myflow/users/{user_id}/threads/{thread_id}/user-data/uploads/` 整棵树上传到
   `/mnt/user-data/uploads/`。上传失败会让整次 acquire 失败，并 kill 新建的沙箱，不会带着
   半份文件继续跑。
2. **技能按需上传**：不预传整棵技能树。`read_file` 命中 `/mnt/skills` 下的文件时只上传那一个；
   `list_dir` / `glob` / `grep` 命中时上传这次查询的子树，之后同一棵子树不再重复传。技能路径
   仍然是只读挂载语义，由路径守卫保证，不依赖宿主目录共享。
3. **release**：把 `/mnt/user-data/outputs/` 下载回宿主同名目录，覆盖同名文件、保留仅宿主存在
   的文件。下载失败只 warning，不会因为一份产物丢掉一个还在用的沙箱。`workspace/` 默认不传
   也不回写；需要"下一轮接着干上轮工作区"时把 `sync.upload_workspace` /
   `sync.download_workspace` 打开。

`thread_files.py` 完全不变：前端按虚拟路径上传/读产物，宿主 `uploads/`、`outputs/` 仍是唯一
访问点，差别只是 `outputs/` 的内容由 release 时的下载写进来。

前置条件：

1. 一个 E2B 兼容控制面（E2B Cloud、自建 envd，或 CubeSandbox），以及一个模板。模板不需要
   预建 `/mnt/user-data` 的 bind 源，但要自带 `mkdir`、`cat`、`python3`、`grep`、`find`、`sed`
   （目录初始化、canary、文本工具与 search helper 用）。
2. 装依赖：`cd backend && uv sync --extra e2b`（backend 的 `e2b` extra =
   `agentkit[e2b,redis]`，提供 E2B 官方 Python SDK `e2b>=2.62,<3` 和 `redis>=5`）。如果直接装
   harness 的 extras，`agentkit[e2b]` 不够：per-thread 锁在 `redis` extra 里，必须
   `agentkit[e2b,redis]`。
3. Redis 从 Gateway 可达，`sandbox.e2b.lease.redis_url`（生产用 `$REDIS_URL`）指向它；
   多 worker 必须共用同一个实例/DB。启动时 Redis `PING` 失败直接拒绝启动；运行中续租失败会把
   handle 标成 dead，不会带着失效的锁继续写。
4. **数据面域名可解析**：E2B 官方 SDK 按 `<port>-<sandbox_id>.<sandbox_domain>` 访问 envd，
   所以 Gateway 必须能解析 `*.cube.app`（或你在 `sandbox_domain` 配的域名，需要 wildcard DNS）。
   反向代理 / 网关本身也必须允许数据面地址。

配置要点：

- `sandbox.use`、整个 `sandbox.e2b`、`skills.container_path`、`skills.builtin.*`、
  `skills.git.cache_dir`、`skills.source` 都是 **startup-only**，改完重启。
- `release_action` 默认 `pause`：`after_agent` 每轮跑完都会下载 outputs 再释放，kill 会变成
  "每条用户消息冷启动一个沙箱"。pause 保留内存和进程状态，下一轮 `connect()` 自动恢复。
  `shutdown()` 会先下载 outputs 再 kill。
- `on_timeout` 默认 `pause`，`auto_resume: true`：空闲到 `timeout_seconds` 暂停，下次请求自动恢复。
  配额紧张时可以把 `on_timeout` / `release_action` 改成 `kill`；宿主文件仍由显式同步保证。
- `sync` 是同步开关与上下限：`upload_uploads` / `download_outputs` 默认开，`upload_workspace` /
  `download_workspace` 默认关；`max_file_bytes`（默认 32 MiB）超限的文件跳过并 warning，
  `max_tree_bytes`（默认 256 MiB）超限的树直接放弃并 warning，绝不截断。
- `allow_public_traffic: false` 让 CubeProxy 拒绝未带 traffic token 的公网请求；e2b SDK 会自动带。
- `api_key` 留空时由 e2b SDK 读 `E2B_API_KEY`，`api_url` / `template_id` 建议直接用
  `$E2B_API_URL` / `$E2B_TEMPLATE_ID`，与 SDK 自己的环境变量保持一致。
- 沙箱 metadata 只有 `myflow.version` / `myflow.user` / `myflow.thread` / `myflow.instance`，
  不再写 `host-mount` 或 `mounts_hash`。发现只按 user/thread 匹配；旧 `cube:` sandbox_id 在
  `get()` 里是内存 miss，runtime 会重新 acquire。
- acquire 的 canary 是**三方一致**：`files.write` 写 token → `files.read` 读回 → 容器内
  `cat` 看同一份文件，证明文件 API 与容器视图没有分叉。探针放在
  `/mnt/user-data/workspace/.myflow-canary/`，成功后删除；失败会 kill 新沙箱。
- `sandbox.mounts` 只对 local / opensandbox 生效；E2B provider 不读宿主目录。

**回滚**：把 `sandbox.use` 改回 `agentkit.sandbox.local:LocalSandboxProvider`，重启 Gateway 即可。
不需要数据库迁移，也不需要移动宿主文件：local 与 E2B 共用同一份 `user-data` 和技能树。旧
`e2b:` sandbox_id 在回滚后 `get()` 返回 `None`，runtime 会重新 acquire 本地沙箱；反过来从 local
切到 E2B 也一样。

### OpenSandbox（容器隔离）

在容器里跑工具：`sandbox.use: agentkit.sandbox.opensandbox:OpenSandboxProvider`，启用
`sandbox.opensandbox`（`config.example.yaml` 里有整段注释模板）。容器里看到的
`/mnt/user-data` 还是同一个宿主线程目录，上传与产物链路不变，所以和 local 之间切换
不需要迁数据。

**容器由 `opensandbox-server` 托管**，Gateway 不再驱动 docker：少了一整套端口发布、
容器名、label 发现和孤儿扫描，Gateway 侧也不需要 `docker` CLI。

前置条件：

1. 起服务端（默认 `127.0.0.1:8080`，`GET /health` 可探）：
   ```bash
   uvx opensandbox-server init-config ~/.sandbox.toml --example docker
   uvx opensandbox-server
   ```
   Docker/Kubernetes runtime 都在服务端的 `~/.sandbox.toml` 里选，不在这份配置里。
2. 装依赖：`cd backend && uv sync --extra opensandbox`（= `agentkit[opensandbox,redis]`）。
3. 镜像只要自带 bash：默认 `python:3.12`。不需要 AIO 那种自带浏览器/Jupyter 的大镜像——
   我们只用命令与文件 API（`grep` 走 shell）。
4. 宿主挂载：`host_mount.local_root` 是 Gateway 进程看到的根（要覆盖 `.myflow` 与
   `skills/`），`host_mount.host_root` 是**服务端宿主**看到的同一个根（卷源按它翻译）。
   生产写 `$SANDBOX_HOST_ROOT`（仓库根 `.env`）；`opensandbox.toml` 的
   `allowed_host_paths` 与 `backend-compose` 挂载共用该变量。
5. Redis 必填：`sandbox.opensandbox.lease.redis_url`，与 E2B 共用同一个 per-thread 锁；
   不可达时启动失败。

**数据面地址（Gateway 在宿主机 vs 在容器）**：直连模式（`use_server_proxy: false`，默认）
下，服务端返回的 endpoint 由它的 `[server] host` 和 `[docker] host_ip` 决定。典型配置
`server.host: 0.0.0.0` + `docker.host_ip: host.docker.internal` 会返回
`host.docker.internal:<宿主端口>/proxy/<端口>`。这个名字只在 OpenSandbox 容器里指向宿主；
Gateway 在宿主机上解析不到它，表现为请求发不出去（`pre_send`、`status=None`）。配置里按部署位置二选一：

- **Gateway 在宿主机**：服务端返回 `host.docker.internal` 时设
  `sandbox.opensandbox.endpoint_host: 127.0.0.1`，或精确映射
  `endpoint_host_map: {host.docker.internal: 127.0.0.1}`。
- **Gateway 在容器里**：保留服务端返回值（给 Gateway 容器加
  `--add-host=host.docker.internal:host-gateway`），或把服务端返回的 host 映射成容器实际可达的
  宿主机/网关 IP。

改写只替换 host，服务端发布的端口和 `/proxy/...` 路径原样保留；`endpoint_host_map` 的 key 可以带端口，
用来只改 44772/18080 中的一个。控制面（`domain`）不受影响。`use_server_proxy: true` 是 SDK 已弃用的
代理数据面路径，文件下载曾出现 400，默认不建议打开。

生命周期：

- `release_action` 默认 `keep`：每轮结束只放租约，沙箱留给 TTL（`timeout_seconds`）
  回收，下一轮 `connect` 接回同一个沙箱；改成 `destroy` 则每轮 `kill`。
- `prewarm`（默认开）把 acquire 从 `before_agent` 移到后台：只有最新用户消息带
  代码块、路径、命令或 traceback 等工具信号时才预取，模型首 token 与容器启动并行。
  首个沙箱工具仍走正常的 lazy acquire，通过 provider 的 per-thread 锁复用同一个
  acquire 结果；纯文本轮不会创建沙箱。预取没被工具用到时，收尾会等这次 acquire
  结束后再按 `release_action` 释放/保留（只影响 run 结束，不影响首 token）；
  `prewarm_max_concurrent` 限制每进程并发预取，满额时该轮退回工具惰性 acquire。
- `reset()` 不动远端；`shutdown()` kill 本进程持有的沙箱；LRU 驱逐只丢 handle。
- acquire 会用 `get_info()` 复核缓存：TTL 到期或被外部 kill 了就重建。
- 挂载或技能路径变了（metadata 里 `myflow.mounts_hash` 不一致）会 kill 后重建。
- 上次进程崩溃留下的沙箱：`orphan_policy: report`（默认）只在启动日志里列出 id；
   改成 `remove` 会 kill 掉超过 `orphan_ttl_seconds` 的孤儿。

**run_code（跑一段代码）**：`sandbox.run_code.enabled: true` 才会把 `run_code` 工具
交给模型（默认关着，模型看不见就不会调）。实现在 `Sandbox.run_code`：

| provider | 走哪条路 |
|---|---|
| `opensandbox` | 配了 `opensandbox.run_code` 就走 **Code Interpreter**（镜像 + entrypoint 换成官方 code-interpreter，能返回表达式结果，支持 python/javascript/typescript/bash/java/go）；没配就走 shell 通道 |
| `e2b` | shell 通道（模板自带 `python3`），支持 python/javascript/bash |
| `local` | shell 通道，但要求 `allow_host_bash: true`：这就是在宿主上跑模型代码，与 bash 工具同一个信任前提 |

shell 通道会把片段写到 `/mnt/user-data/workspace/.myflow-run/`、跑完即删；typescript/java/go
在 shell 通道下会明确报"需要 code interpreter"，不会猜着跑。

**回滚**：`sandbox.use` 改回 local 或 E2B 并重启即可；旧的 `osb:` sandbox_id 在
`get()` 里是内存 miss，runtime 会重新 acquire；残留沙箱交给服务端的 TTL 或 `osb` CLI。

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
| `GET /api/memory/profile/schema` | 全部受支持字段的元数据，前端按它渲染全字段表单；字段均为自由填写，描述中的“常见”只是提示 |
| `PATCH /api/memory/profile` | 部分更新；`null` 删除字段；列表整列表替换；etag CAS |
| `GET/POST /api/memory/profile/onboarding` | 画像引导 |
| `GET /api/memory/items/types` | 记忆管理支持的七类记忆与中文名（不含 skills） |
| `GET /api/memory/items` | 按类型列记忆摘要（OpenViking 过滤型 `find`，不落库） |
| `GET /api/memory/items/detail` / `content` | 详情目录（`.overview.md`）与记忆正文（`read`） |
| `PUT /api/memory/items/content` / `DELETE /api/memory/items` | 编辑/删除具体记忆（`write` / `rm`，sidecar 只读） |

记忆管理页（前端入口 `/workspace/memory`，侧栏“记忆管理”）的查看/编辑/删除直接调用 OpenViking 协议
（`find` / `overview` / `read` / `write` / `rm`），MyFlow 不保存记忆副本，`.abstract.md` / `.overview.md` 只读。

画像在 run assembly 时渲染进系统提示的 `<user_profile>` 段；记忆正文由内置子代理
`memory-researcher` 通过只读工具 `memory_search` / `memory_read` / `memory_overview` 检索，
lead 只拿结论与来源（`2026-11-02-retrieval-subagents-design.md`）。写入只有两个入口：用户在“我的画像”
表单里保存，或用户在对话里**显式**说出画像信息后由内置工具 `update_user_profile` 写入
（模型推断不写画像；工具与表单共用同一行和 etag）。写入工具由 `HumanInTheLoopMiddleware`
在图层面暂停：模型发出 `update_user_profile` 后、工具执行前产生 interrupt，
前端显示变更确认卡片，用户允许后用 `Command(resume={"decisions": [...]})` 续跑，
拒绝则不执行工具。

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

### Langfuse 可观测性（远程自建）

Langfuse 已在远程机器上运行；MyFlow 只做客户端接入，不管理 Langfuse 的 ClickHouse/Postgres/Redis/MinIO。
整段 `observability.langfuse` 是 **startup-only**，改完后重启 Gateway。

1. 装可选依赖：`cd backend && uv sync --extra observability`。
2. 在 `.env` 写 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`、`LANGFUSE_USER_ID_SALT`；`base_url` 只写根地址，不要带 `/api/public`。
3. 把 `config.yaml` 的 `observability.langfuse.enabled` 改为 true，密钥写 `$LANGFUSE_...` 形式。
4. 重启 Gateway，发一条会触发工具的 workspace 消息；Langfuse 中 trace id 应与响应头 `X-Trace-Id` 相同。

默认 `capture_content: redacted`：导出前用 `mask_otel_spans` 把字面 PII 和 `[[PII:...]]` handle 替换为
`[REDACTED]`。更严格的环境改 `metadata_only`，只保留模型、耗时、token 和错误类型。Langfuse 故障、
未安装 SDK、远程不可达都不会让 run 失败；需要启动即失败时设 `required: true`。

```yaml
observability:
  langfuse:
    enabled: true
    base_url: $LANGFUSE_BASE_URL
    public_key: $LANGFUSE_PUBLIC_KEY
    secret_key: $LANGFUSE_SECRET_KEY
    capture_content: redacted
    user_id_mode: hashed
    user_id_salt: $LANGFUSE_USER_ID_SALT
```

回滚：`enabled: false` 并重启。服务端 trace 保留期/采样由远程 Langfuse 管理；`sample_rate` 是 head sampling，
降采样会同时丢掉一部分错误 trace。

## ContextForge 工具管理（可选）

ContextForge 是 MCP 工具的**唯一来源和写入目标**：MyFlow 不建工具表，也不复制 CRUD 语义。浏览器只打同源
`/api/tools`，ContextForge 的 admin token 始终留在网关。

部署前提（ContextForge 1.1.0）：

- `MCPGATEWAY_ADMIN_API_ENABLED=true`：`/tools` 与 `GET /admin/events` 才会挂载；
  `MCPGATEWAY_UI_ENABLED` 可以保持 `false`，本功能不依赖 Admin UI。
- `admin_token` 需要 `admin.events` 或 `*`；事件订阅只发 `Authorization: Bearer ...`，
  GET 不要求 CSRF。
- ContextForge 多 worker 时，跨进程事件需要它自己的 Redis 配置；否则只有处理写操作的那个
  worker 能看到事件，MyFlow 会退回"管理动作主动刷新 + 工具调用纠错刷新"。
- Milvus 复用现有实例，但 collection 固定为 `myflow_tools_catalog`，**绝不写**
  `myflow_knowledge` / `myflow_skills_active`。不需要数据库迁移。

```bash
# MCP 加载器 + pymilvus；knowledge / skills 的 extra 不会自动带上它。
uv sync --all-packages --all-extras
# 或只装这一组：
uv sync --package backend --extra contextforge
```

配置见 `config.example.yaml` 的 `contextforge:`。`enabled: false` 时不发任何请求，
`/api/tools` 返回 503 `contextforge_disabled`；`tool_loading: auto` 在工具数超过
`deferred_threshold`（默认 24）时把 ContextForge 工具的 schema 藏起来，模型先看到
`search_tools`，再按任务检索并提升。`eager` 直接把全部工具放进模型 schema；
`deferred` 强制延迟。`auto` / `deferred` 都要求 `contextforge.search.enabled=true`。

工具更新有三条路径，都必须知道：

1. **管理页写操作**：`POST/PUT/DELETE /api/tools` 写 ContextForge 后立即返回，网关异步
   刷新 MCP 快照并 `index.sync()`；`sync.admin_refresh_async=false` 时改成同步等待。
2. **事件订阅**：启动时连 `GET /admin/events`，对 `tool_added / tool_updated / tool_deleted /
   tool_activated / tool_deactivated / tool_offline / tool_removed / gateway_*` 做 debounce
   刷新；断线指数退避重连，状态在 `/api/tools/status` 的 `admin_events` 里可见。
3. **工具调用纠错**：MCP 返回 not-found 或参数校验错误时，先刷新 ContextForge +
   `index.sync()`，再重试**一次**；认证失败、超时、429、5xx、网络错误和工具执行错误都不重试。

检索是混合检索：dense embedding + Milvus BM25 稀疏通道，RRF 融合由 Milvus
`RRFRanker` 在服务端完成，之后可选 rerank。应用进程不计算 BM25、不融合排名；
Milvus 不可用时返回空结果并在 `retrieval.warnings` 里说明；仅 embedding 不可用时
仍走 Milvus BM25，没有本地降级索引。
召回结果必须能在当前 MCP 快照里确认，Milvus 只负责缩小候选集。

`/api/tools` 对**所有已登录用户**开放；ContextForge 的 admin token 只由网关持有，
浏览器永远拿不到。工具按 MyFlow 用户隔离：每个用户只看到、修改和调用自己创建的工具；
没有 `myflow_owner` 注释的历史工具显示为「未归属」，可由第一个认领者接管。
归属信息存在 ContextForge 的 `annotations.myflow_owner`（存储用户 ID），在 API 响应和
编辑器里会被剥掉。上游错误映射：工具不存在 404 `tool_not_found`、
凭据/权限 502 `contextforge_auth_failed`、ContextForge 不可达 502
`contextforge_unavailable`、超时 504 `contextforge_timeout`、参数不合法 422
`contextforge_validation_error`。详情里的敏感 header 打成 `****`，编辑时原样回传，
由网关还原真实值；只发 `****` 而没有已存值会被拒绝，不会把掩码当凭据写进 ContextForge。

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/api/tools` | 自己的工具 + 未归属工具（可认领），支持分页与过滤 |
| `GET` | `/api/tools/status` | ContextForge 可达/鉴权、管理事件、同步、索引与三路通道状态（登录用户） |
| `POST` | `/api/tools/reindex` | 手动刷新 MCP 快照并重建工具索引（登录用户） |
| `GET` | `/api/tools/search?q=...` | 检索预览：命中、通道、候选数、是否重排与 warnings（登录用户） |
| `POST` | `/api/tools` | 新建工具并触发异步刷新（登录用户） |
| `GET` | `/api/tools/{id}` | 自己的或未归属工具详情，敏感 header 掩码 |
| `PUT` | `/api/tools/{id}` | 整份更新；未归属工具会先认领 |
| `POST` | `/api/tools/{id}/claim` | 认领未归属的历史工具（登录用户） |
| `POST` | `/api/tools/{id}/state` | 启用/停用并触发异步刷新（登录用户；未归属工具会先认领） |
| `DELETE` | `/api/tools/{id}` | 删除并触发异步刷新（登录用户） |

前端入口是 `/workspace/tools`（侧栏「工具管理」），所有登录用户都能打开；认证关闭的
单人模式同样放行。写操作后状态栏可能短暂显示旧快照——这是异步刷新的预期表现，
不是数据丢失。

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
| `GET` | `/api/skills` | 内置技能 + 当前用户已生效的私有技能（`skills:read`）；没有公共目录 |
| `GET` | `/api/skills/mine` | 当前用户全部私有技能（含草稿、提炼来源与扫描状态，`skills:read`） |
| `POST` | `/api/skills` | 新建私有草稿（`skills:write`） |
| `POST` | `/api/skills/{skill_id}/draft/save` | Git commit + 不可变版本；不生效、不建索引（`skills:write`） |
| `POST` | `/api/skills/{skill_id}/scan` | import `skill_scanner`，用规则 + 默认模型 LLM 扫描最新已保存版本的 Git commit 并持久化结果（`skills:write`） |
| `GET` | `/api/skills/{skill_id}/scans` | 扫描历史（`skills:read`） |
| `POST` | `/api/skills/{skill_id}/activate` | 扫描通过后生效、刷新快照并写索引 outbox（`skills:write`） |
| `DELETE` | `/api/skills/{skill_id}` | 删除私有技能与 active 指针，并刷新目录/索引（`skills:write`） |
| `PUT` | `/api/skills/{skill_id}/preference` | 开关"我用不用"（任何登录用户，写 PostgreSQL 的 `skills.enabled`，按 `skill_id`） |
| `POST` | `/api/skills/reload` | 重建本进程的目录快照与挂载树（`admin`） |
| `GET` | `/api/v1/auth/setup-status` | 库里有没有 admin（公开） |
| `POST` | `/api/v1/auth/initialize` | 建第一个 admin（公开，且只在零 admin 时；要 username + password） |
| `POST` | `/api/v1/auth/login/local` | 本地登录（公开，响应体不含 token）。body 为 `username` + `password` |
| `POST` | `/api/v1/auth/register` | 自助注册（公开，受 `auth.allow_registration` 控制；username + password） |
| `POST` | `/api/v1/auth/logout` | 清 cookie（公开，幂等） |
| `GET` | `/api/v1/auth/providers` | 有哪些登录方式（公开；OIDC 不在 M9） |
| `GET` | `/api/v1/auth/me` | 当前用户（含 `username`；**仅会话**，PAT → 403 `session_required`） |
| `PATCH` | `/api/v1/auth/me` | 改**自己**的用户名（body `{"username": ...}` → `UserResponse`；**仅会话**）。不动令牌、不换桶 |
| `POST` | `/api/v1/auth/change-password` | 改密 = 撤销所有会话（**仅会话**） |

技能创作、安全扫描与生效路由见上表（`POST /api/skills`、`/api/skills/mine`、
`/api/skills/{id}/scan`、`/api/skills/{id}/activate`）。

浏览器应走前端同源 `/api`，不要直接跨源打 8001（开发时 Next rewrite，生产时 nginx）。

## 知识库（MinerU + Qwen3-Embedding-8B + Milvus）

`knowledge_base.enabled: true` 后，lifespan 建立知识库服务。**所有服务地址只来自
config.yaml**，代码里没有默认端点；启用时缺 `parse.local.endpoint`（或云端
`base_url` + `api_key`）、顶层 `embedding.base_url`、顶层 `vector_store.uri`
（或 knowledge_base section 内的显式覆盖）会拒绝启动。

知识库与技能检索共用同一套 embedding / rerank / Milvus 连接，配置放在顶层
`embedding`、`rerank`、`vector_store`。技能检索的 dense 向量、BM25 和
Milvus RRFRanker 融合全部在 `myflow_skills_active` 内完成；应用侧只做 query
embedding、active 指针确认与可选精排。升级到该 schema 时旧 collection 没有
BM25 `sparse` 字段，必须先停网关、删除 `myflow_skills_active` 再重新激活技能；
旧配置里的 `keyword_weight` / `name_weight` 已移除，改用 Milvus 的
`keyword.analyzer` / `bm25_k1` / `bm25_b`。`knowledge_base.embedding`、
`knowledge_base.vector_store`、`knowledge_base.retrieval.rerank` 和
`skills.search.*` 只保留 collection、开关、超时、重试、指令等个性化字段，
同名字段仍可覆盖顶层值。分片 token 计量固定使用内置的近似计数器（CJK 字符/
英文词），没有 `chunking.tokenizer` 配置项；不加载模型 tokenizer，也不需要
`transformers`。

MinerU 的**接口路径也是配置**：本地 `parse.local.file_parse_path`（默认 `/v1/file_parse`）、
云端 `parse.cloud.file_urls_path`（默认 `/api/v4/file-urls/batch`）与
`parse.cloud.extract_results_path`（默认 `/api/v4/extract-results/batch/{batch_id}`，必须含
`{batch_id}`）。解析器里没有路径常量，换网关/代理只改配置；路径必须相对（含 `://` 直接
报错），主机写在 `endpoint`/`base_url`。云端响应里的签名上传 URL 和 `full_zip_url` 是
服务端临时地址，不配置。

```bash
# 只在启用知识库时需要；同时保留 postgres 等其它 extra。
# knowledge extra 只含 pymilvus，不再安装 transformers。
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
检索命中子块后返回父块全文。分片使用内置近似 token 计数，不加载 tokenizer、
不访问 Hugging Face；清洗和切分在 worker 线程执行，不会阻塞 Gateway 的事件循环
和文档状态轮询。

清洗在通用文本规范化后，确定性删除目录页、前言、版权声明、免责声明、修订记录和
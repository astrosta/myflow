# CubeSandbox 宿主目录挂载沙箱设计

> 状态：已确认并实现（2026-11-04；部署前提已确认）
> 2026-11-04 协议修订：调用协议改为 E2B，SDK 相关段落以 [CubeSandbox E2B 协议迁移设计](2026-11-04-cubesandbox-e2b-protocol-design.md) 为准。
> 适用范围：MyFlow 的 `agentkit.sandbox` 沙箱层、Gateway 启动配置、技能挂载、上传与产物读取
> 已选方案：A（同机 Linux 宿主目录 + CubeSandbox `host-mount`）
> 目标 provider：`agentkit.sandbox.cube:CubeSandboxProvider`
> CubeSandbox 版本：0.7.x（官方 Python SDK `cubesandbox>=0.7.3,<0.8`）
> 已确认部署前提：Gateway 与 Cubelet 在同一台 Linux 主机；宿主目录在 Ubuntu 本地文件系统（按 ext4 处理），不存在 NFS `root_squash`；多 worker 使用 Redis per-thread 锁（V1 必选）。

## 1. 意图与问题

MyFlow 现在的 `LocalSandboxProvider` 是宿主路径映射，不是隔离边界：`allow_host_bash=true` 时，shell 可以直接越过 `PathMapping` 访问宿主。我们要把 agent 的代码执行放进 CubeSandbox 的 MicroVM，同时保留现有的文件工具、bash 工具、技能路径、上传和产物链路。

CubeSandbox 是腾讯云开源的 E2B 兼容沙箱。它提供：

- `Sandbox.create()` / `Sandbox.connect()` / `kill()` / `pause()` 生命周期；
- `commands.run()` 在沙箱内执行 shell；
- `files.read()` / `write()` / `list()` / `stat()` 文件 API；
- 宿主目录挂载扩展：`metadata["host-mount"]`。

宿主挂载的形状是：

```json
[
  {
    "hostPath": "/opt/myflow/.myflow/users/u1/threads/t1/user-data",
    "mountPath": "/mnt/user-data",
    "readOnly": false
  }
]
```

`hostPath` 是 **Cubelet 节点**上的路径，不是运行 Python 脚本那台机器的路径；必须位于 CubeMaster 的 `allowed_host_mount_prefixes` 下。CubeMaster 默认只允许 `/data/shared/`；本部署需要把项目根（例如 `/opt/myflow`）加入允许前缀。

方案 A 的核心判断是：**让 CubeSandbox 直接看到 Gateway 正在写的同一份工作区**。这样 `thread_files.py`、`upload`、`artifacts`、技能目录都不需要新增同步层，改动集中在 provider、挂载描述和启动校验。

## 2. 目标与非目标

### 2.1 目标

1. 新增 `CubeSandboxProvider`，通过 `sandbox.use` 切换，不改变 `Sandbox` / `SandboxProvider` 的公共契约。
2. 每个 thread 的 `/mnt/user-data` 挂到该 thread 的宿主 `user-data` 目录，读写与本地沙箱一致。
3. 技能目录按现有语义挂载：
   - git 来源：`<skills.container_path>/private` 挂当前用户的活动树，只读；
   - git 来源：`<skills.container_path>/builtin` 挂内置技能，只读；
   - filesystem 来源：`<skills.container_path>` 挂整棵技能树，只读；
   - 内置目录在技能树内时不再重复挂载。
4. `uploads` / `outputs` 继续由 `backend/app/gateway/thread_files.py` 直接读写宿主；因为宿主目录就是挂载源，浏览器和沙箱看到的是同一份文件。
5. 子代理共享同一个 thread 沙箱；`SandboxLeaseManager` 的最后一个持有者语义不变。
6. 远端沙箱被 TTL 回收、Gateway 重启、provider 缓存被 reset 后，能重新发现或重建沙箱，宿主工作区不丢。
7. 路径安全、错误契约、环境变量校验与本地 provider 保持一致。
8. 多 worker 通过 Redis per-thread 锁串行化；V1 就启用，不把多 worker 留到后续阶段。

### 2.2 非目标（V1）

- 不做 file-sync / tar 同步数据面。V1 只实现 host-mount。
- 不做 CubeSandbox 云端多租户计费、模板市场、快照回滚 UI。
- 不做跨节点文件系统；Gateway 和 Cubelet 已确认在同一台 Linux 主机上。
- 不做浏览器直连沙箱端口；默认关闭公网流量。
- 不做沙箱内浏览器、桌面、GPU 等模板能力。
- 不做非 root 用户的完整 UID/GID 映射；V1 默认沙箱用户 `root`，模板负责 umask。
- 不做 CubeSandbox 模板的自动构建；模板由运维预先构建并配置 `template_id`。
- 不改 `thread_files.py` 的协议和错误体。

### 2.3 反目标

- 不能把整个 `.myflow` 根或项目根挂给沙箱；只挂当前 thread 的 `user-data` 和只读技能目录。
- 不能把 `.myflow/skills` 缓存根或 `active/users` 根整体挂给普通用户沙箱；私有技能只挂当前用户的活动树。
- 不能让宿主挂载的 `hostPath` 来自客户端输入；它只能由服务端从已验证的 user/thread 和配置生成。
- 不能静默接受“挂载了但权限不对”的沙箱；必须有双向 canary。
- 不能让 provider 的 `get()` 做网络 IO；`get()` 必须是纯内存查找。
- 不能因为 CubeSandbox 不可用而回退到宿主 bash；失败必须显式返回错误。
- 不能在 `reset()` 里杀远端沙箱；那会杀掉别的 Gateway 进程正在用的实例。

## 3. 现状约束

这些是设计必须对齐的既有事实：

- `Sandbox` / `SandboxProvider` 在 M2 就按“沙箱在另一台机器上”设计；`acquire_async` 默认把同步 `acquire` 丢到线程池。
- `SandboxProvider.get()` 被要求是纯内存查找；健康检查、所有权必须在 `acquire` 时确定并缓存。
- `SandboxMiddleware` 负责预热和正常路径释放；`agentkit.sandbox.lease` 负责 lead/子代理的引用租约和兜底释放。
- `is_host_bash_allowed()` 对非 local provider 返回 `True`，所以 Cube provider 下 bash 工具会自动出现在工具列表里。
- `sandbox.use` 是 startup-only：`get_sandbox_provider()` 首次调用时创建单例；热重载不会重建。
- `skills.container_path`、`skills.builtin.path`、`skills.builtin.enabled` 已经是 startup-only，因为它们会进入沙箱挂载和提示词索引。
- `skills_catalog._private_mounts_supported()` 通过 provider 的 `supports_per_user_skill_mounts` 决定是否向前端/提示词暴露私有技能容器路径。
- `thread_files.py` 的 `_mappings()` 直接用 `get_paths().sandbox_user_data_dir(thread_id, user_id)`，上传、列表、删除、产物读取都走宿主路径。
- `Paths.ensure_thread_dirs()` 创建 `workspace` / `uploads` / `outputs`，目录权限是 `0777`。
- 当前 `runtime_home()` 返回 `project_root() / ".myflow"`；本设计默认用 `project_root()` 作为同机项目根，不依赖 `MYFLOW_HOME`。
- 当前开发网关可能跑在 Windows，而 CubeSandbox 依赖 Linux/KVM；本设计的目标部署已确认是**同一台 Linux 主机**，Gateway 和 Cubelet 直接看到同一份 Ubuntu 本地文件系统目录（按 ext4 处理）。开发机验证不等于生产验证。

## 4. 方案与取舍

### 4.1 A. 同机宿主目录 + host-mount（采用）

- Gateway 继续把工作区写在 `.myflow/users/{user}/threads/{thread}/user-data`。
- CubeSandbox 创建时用 `metadata["host-mount"]` 把这个目录挂到 `/mnt/user-data`。
- 技能目录以只读方式挂到 `<skills.container_path>` 下。
- `thread_files.py` 不需要新增同步逻辑。

优点：

- 上传、产物、workspace 的读写没有第二条数据路径，语义最接近本地 provider。
- 沙箱内写出的产物立刻出现在宿主，`GET artifacts` 不需要 flush。
- 浏览器上传的文件立刻出现在沙箱，不需要“下次 acquire 再同步”。
- 不需要 tar 解包、文件清单、冲突合并，不需要在 `release` 时做网络同步。
- 本地 provider 和 Cube provider 可以共用同一份 thread 数据，切换 provider 不迁移数据。

代价：

- 部署强耦合：Gateway 和 Cubelet 必须在同一台 Linux 主机上看到同一个根；本设计默认 `local_root == host_root == project_root()`。
- CubeMaster 必须把项目根加入 `allowed_host_mount_prefixes`。
- 宿主目录权限、UID 映射、umask 必须能被 canary 验证。
- 生产 Gateway 必须跑在 Linux 主机上；Windows 开发机只能做 fake/单元测试，不能做真实 Cube 验收。
- 多 worker 必须用 Redis per-thread 锁；V1 必选，否则两个沙箱会同时写同一份宿主文件。

### 4.2 B. Gateway 存储 + 文件 API 同步（未采用）

在 `acquire` 时把 uploads/workspace 灌入沙箱，在 `release` 或产物请求时拉回。它可移植到远端 CubeAPI，但需要同步层、tar 安全解包、产物 flush 和冲突策略；当前需求选 A，先不承担这部分复杂度。

### 4.3 C. E2B sidecar / 外部适配层（未采用）

多一个组件，宿主目录问题没有消失，只是被推到 sidecar；对当前仓库没有收益。

## 5. 总体架构

```
浏览器
  └─ FastAPI Gateway
       ├─ thread_files.py ──────────────┐
       │   uploads / artifacts           │ 直接读写宿主
       │                                 ▼
       │                    {local_root}/.myflow/users/{uid}/threads/{tid}/user-data
       │                                 ▲
       └─ agentkit tools                 │ host-mount（同一份数据）
            └─ ensure_sandbox_initialized()
                 └─ CubeSandboxProvider
                      ├─ Redis per-thread 锁（多 worker 串行化）
                      ├─ 控制面：CubeAPI  /sandboxes
                      ├─ 数据面：CubeProxy → envd  /files  /process
                      └─ HostMountDataPlane
                           ├─ 生成 host-mount 描述
                           ├─ 把 local_root 翻译成 Cubelet 的 host_root
                           └─ 双向 canary
```

关键点：**没有第二套工作区存储**。Gateway 的宿主目录是唯一真相源，CubeSandbox 只是通过 bind mount 看到它。

## 6. 配置设计

### 6.1 `config.yaml`

```yaml
sandbox:
  use: agentkit.sandbox.cube:CubeSandboxProvider

  cube:
    # 控制面。CUBE_API_URL 建议显式配置，不依赖 SDK 默认的 127.0.0.1:3000。
    api_url: $CUBE_API_URL
    api_key: ""                     # 留空时读 CUBE_API_KEY；无鉴权部署可保持空
    template_id: $CUBE_TEMPLATE_ID
    sandbox_domain: cube.app
    proxy_node_ip: ""               # 留空读 CUBE_PROXY_NODE_IP；DNS 可解析时可空
    proxy_port: 80

    # 生命周期
    timeout_seconds: 1800           # 空闲 TTL；显式给值，不依赖集群默认
    on_timeout: pause               # kill | pause
    auto_resume: true
    release_action: pause           # pause | kill；pause 保持 MicroVM 热，kill 每次重建
    acquire_timeout_seconds: 120
    request_timeout_seconds: 30
    startup_healthcheck: true

    # 网络
    allow_internet_access: true
    allow_public_traffic: false
    sandbox_user: root              # V1 默认 root；非 root 需先验证 UID/GID/umask
    network:
      deny_out:
        - 169.254.169.254/32

    # 宿主挂载（同机部署）
    host_mount:
      # Gateway 进程看到的根；默认 project_root()。
      local_root: /opt/myflow
      # Cubelet 看到的同一个根；同机部署与 local_root 相同，可省略。
      host_root: /opt/myflow
      # 启动时断言 host_root 在 CubeMaster 允许前缀下；只做提示，不代替 CubeMaster 校验。
      allowed_prefix: /opt/myflow

    # V1 必选：多 worker per-thread 锁。
    lease:
      redis_url: $REDIS_URL
      ttl_seconds: 30

    # 进程内缓存上限；驱逐只丢 handle，不杀远端沙箱。
    max_cached_threads: 64
```

说明：

- `sandbox.cube` 是可选嵌套；本地 provider 不需要这段配置。
- `template_id` 在选择 Cube provider 时必填；`CubeSandboxConfig` 本身在 pydantic 里用 `| None`，避免本地配置被迫填写。
- `api_key: ""` 不写成 `$CUBE_API_KEY`，因为 `AppConfig.resolve_env_variables` 对缺失环境变量会直接报错；provider 在 `api_key` 为空时读 `os.getenv("CUBE_API_KEY")`。`proxy_node_ip` 同理：留空时读 `CUBE_PROXY_NODE_IP`。
- 已确认同机部署：`local_root` 和 `host_root` 指向同一个目录；`host_root` 省略时默认等于 `local_root`。`local_root` 默认是整个 `project_root()`，因为它必须同时覆盖 `.myflow`（thread 数据 + git 技能缓存）和 `skills/`（内置技能）。
- `host_root` / `local_root` 不能是 `/`；建议直接使用项目根（例如 `/opt/myflow`），并在 CubeMaster 的 `allowed_host_mount_prefixes` 中加入这个根。
- 宿主目录在 Ubuntu 本地文件系统（按 ext4 处理）上，不存在 NFS `root_squash`；仍然需要 canary 验证 guest root 写出的文件对 Gateway 进程可读、可删除。
- `lease.redis_url` 是 V1 必填；启动时 `PING` 失败直接拒绝启动。`ttl_seconds` 默认 30，续租周期 `ttl/3`。
- `allow_public_traffic: false` 使 CubeProxy 拒绝未带 traffic token 的公网请求；SDK 会自动带 `trafficAccessToken`。
- `release_action: pause` 是 V1 默认：`SandboxMiddleware.after_agent` 在每轮 run 结束都会触发释放，如果每次都 kill，就会变成“每条用户消息冷启动一个 MicroVM”。pause 保留内存和进程状态，下一轮 `connect()` 自动恢复；宿主文件本来就在同机项目根上，pause 不影响产物。
- 对资源隔离要求更高、或线程数量很大时改 `kill`；宿主文件仍在，下一轮重建。`shutdown()` 始终用 kill。
- `on_timeout: pause` 让空闲沙箱暂停而不是销毁；下次请求自动恢复。如果集群配额紧张，可改 `kill`，宿主数据仍在。

### 6.2 startup-only 与校验

以下字段在 provider 构造时固化，修改后必须重启：

- `sandbox.use`
- `sandbox.cube.*` 全部
- `sandbox.mounts`
- `skills.container_path`
- `skills.builtin.path` / `skills.builtin.enabled`
- `skills.git.cache_dir`、`skills.source`（因为决定 host-mount 源）

在 `reload_boundary` 的注释和 README 中写清楚。V1 不承诺热切换 provider。

### 6.3 新增配置模型

依赖：`agentkit[cube,redis]`。`cube` extra 提供 `cubesandbox>=0.7.3,<0.8`；`redis` extra 提供 `redis>=5`。两个都装齐才允许选择 Cube provider。

在 `agentkit/config/sandbox_config.py` 增加：

```python
class CubeSandboxNetworkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow_out: list[str] = Field(default_factory=list)
    deny_out: list[str] = Field(default_factory=list)
    rules: list[dict] | dict | None = None


class CubeSandboxHostMountConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    local_root: str | None = None      # None -> project_root()
    host_root: str | None = None       # None -> local_root（同机部署）
    allowed_prefix: str | None = None


class CubeSandboxLeaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    redis_url: str                      # V1 必填；启动时 PING
    ttl_seconds: int = 30


class CubeSandboxConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_url: str = "http://127.0.0.1:3000"
    api_key: str = Field(default="", description="留空时读环境变量 CUBE_API_KEY；日志与错误中必须脱敏")
    template_id: str
    sandbox_domain: str = "cube.app"
    proxy_node_ip: str | None = None
    proxy_port: int = 80
    timeout_seconds: int = 1800
    on_timeout: Literal["kill", "pause"] = "pause"
    auto_resume: bool = True
    release_action: Literal["kill", "pause"] = "pause"
    acquire_timeout_seconds: float = 120
    request_timeout_seconds: float = 30
    startup_healthcheck: bool = True
    allow_internet_access: bool = True
    allow_public_traffic: bool = False
    sandbox_user: str = "root"
    network: CubeSandboxNetworkConfig = Field(default_factory=CubeSandboxNetworkConfig)
    host_mount: CubeSandboxHostMountConfig
    lease: CubeSandboxLeaseConfig        # V1 必填；多 worker 共用同一 Redis
    max_cached_threads: int = 64
```

`SandboxConfig` 增加：

```python
cube: CubeSandboxConfig | None = None
```

`CubeSandboxProvider.__init__` 负责：

- 动态导入 `cubesandbox` 和 `redis`；缺失时抛 `SandboxRuntimeError("install agentkit[cube,redis] ...")`。
- 解析 `local_root` 默认值 `project_root()`，校验其为绝对路径且存在。
- 解析 `host_root` 默认值 `local_root`；校验其为绝对 POSIX 路径、不是 `/`、不含 `..`。
- 校验 `lease.redis_url` 非空；网络 `PING` 放在 `startup_check()`，避免懒加载构造函数做 IO。
- 校验所有挂载源都在 `local_root` 下。
- 校验 `template_id` 非空。
- `startup_healthcheck=true` 时调用 `Sandbox.health()`，超时按 `request_timeout_seconds`。
- 不创建 sandbox，不做 per-thread 的 canary。

### 6.4 启动钩子

`get_sandbox_provider()` 是懒加载单例，单靠它无法保证“Gateway 启动时就发现配置/挂载错误”。增加一个显式启动钩子：

```python
# agentkit/sandbox/runtime.py
def start_sandbox_provider(*, app_config=None) -> SandboxProvider:
    provider = get_sandbox_provider()
    provider.startup_check()
    return provider
```

- `SandboxProvider.startup_check()` 默认 no-op；local provider 不做事。
- `CubeSandboxProvider.startup_check()`：
  - 校验 `cubesandbox` 已安装、`template_id` 非空；
  - 校验 `local_root` / `host_root` / 技能源 / `sandbox.mounts`；
  - `startup_healthcheck=true` 时调用 `Sandbox.health()`；
  - 校验 `lease.redis_url` 非空并 `PING`；失败直接拒绝启动，不回退到单 worker。
- `backend/app/gateway/app.py` 的 lifespan 在加载 `AppConfig` 后调用 `start_sandbox_provider(app_config)`；失败直接让启动失败。
- 启动钩子不创建 per-thread 沙箱，也不做 canary；canary 仍在首次 `acquire` 做，因为它需要真实 thread 目录。

## 7. Host mount 描述构建

### 7.1 路径翻译

定义唯一翻译函数：

```python
def to_host_path(local: Path) -> str:
    root = local_root.resolve()
    resolved = local.resolve()
    if not resolved.is_relative_to(root):
        raise SandboxRuntimeError(
            f"sandbox mount source {resolved} is outside host_mount.local_root {root}"
        )
    relative = resolved.relative_to(root)
    return str(PurePosixPath(host_root) / relative.as_posix())
```

要求：

- `local_root` 是 Gateway 进程看到的目录；`host_root` 是 Cubelet 看到的同一个目录。
- 所有挂载源必须能通过 `resolve()` 后仍然位于 `local_root` 内；符号链接逃逸直接拒绝。
- `host_root` 只做字符串拼接；同机部署时它和 `local_root` 是同一个目录，Cubelet 直接看到。
- 如果 `local_root == host_root`，函数退化为直接使用绝对路径。

### 7.2 固定挂载项

每个 sandbox 至少包含：

| mountPath | hostPath | readOnly | 说明 |
|---|---|---|---|
| `/mnt/user-data` | `{local_root}/.myflow/users/{uid}/threads/{tid}/user-data` | false | workspace / uploads / outputs 的聚合根 |
| `<container>/private` | `skills.git.cache_dir/active/users/{uid}` | true | 仅 `skills.source=git` |
| `<container>/builtin` | `resolve_builtin_root(skills)` | true | 仅在启用且路径存在时 |
| `<container>` | `skills.root_dir()` | true | 仅 `skills.source=filesystem` |

其中 `<container>` 是 `skills.container_path.rstrip("/")`。

构建规则：

1. 先调用 `get_paths().ensure_thread_dirs(thread_id, user_id=user)`，保证 `user-data/{workspace,uploads,outputs}` 存在。无 thread 的通用实例使用 `.myflow/users/{uid}/local/user-data`，与 local provider 的通用路径保持一致。
2. git 来源：
   - 计算 `private_root = skills_cache_dir / "active" / "users" / user_id`；
   - 如果不存在，`mkdir(parents=True, exist_ok=True)` 一个空目录；后续 catalog 物化私有技能时会写入；
   - 加入 `<container>/private` 只读挂载。
3. filesystem 来源：
   - 如果 `skills.root_dir()` 存在，加入 `<container>` 只读挂载；
   - 如果不存在，跳过并 warning；没有技能就没有提示词索引，不阻塞启动。
4. builtin：
   - `skills.builtin.enabled=false` 时跳过；
   - 路径存在才加入；路径不存在时 warning 并跳过，不创建目录；
   - 如果 filesystem 来源的根已经覆盖 builtin 路径（`builtin_root.is_relative_to(skills_root)`），不再单独加入，避免嵌套挂载；
   - 否则加入 `<container>/builtin` 只读挂载。
5. 合并 `sandbox.mounts`：
   - 每项要求 `container_path`、`local_path` 都存在；
   - `container_path` 必须是绝对 POSIX 路径，不能是 `/`，不能含 `..`；
   - `local_path` 必须能 `to_host_path()`；
   - `read_only` 原样传递；
   - 如果 `container_path` 等于 `/mnt/user-data` 或位于其下，跳过并 warning；
   - 如果 `container_path` 等于 `<container>` 或位于其下，跳过并 warning（沿用 local provider 对 operator mount 的保护）；
   - 如果 `local_path` 与 skills cache、skills root、builtin root 相同或在其中，跳过并 warning；
   - 相同 `container_path` 重复出现时，保留第一个并 warning。
6. 按 `mountPath` 的层级从浅到深排序；父挂载先于子挂载。
7. 生成 `metadata["host-mount"] = json.dumps(entries, separators=(",", ":"))`。

### 7.3 目标目录必须在模板中存在

host-mount 在 MicroVM 启动前由 Cubelet bind mount；目标目录必须在模板里存在。模板至少预创建：

```
/mnt
/mnt/user-data
/mnt/user-data/workspace
/mnt/user-data/uploads
/mnt/user-data/outputs
/mnt/skills
/mnt/skills/private
/mnt/skills/builtin
```

如果 `skills.container_path` 配成别的路径，模板必须同步创建。缺失时 CubeMaster/Cubelet 会报 bind mount 错误；provider 在创建失败时要把原始错误翻译成可操作提示，而不是只返回 HTTP 500。

### 7.4 只读与权限

- 技能挂载一律 `readOnly: true`。
- `/mnt/user-data` 必须是 `readOnly: false`。
- `sandbox.mounts` 的 `read_only` 原样传递；默认 `false`，但推荐运维显式写 `true`。
- V1 默认 `sandbox_user: root`。MicroVM 内的 root 不是宿主机 root；它只能通过挂载点访问当前 thread 的目录和只读技能。这样避免 host mount 的 UID/GID 映射问题。
- 模板应设置 `umask 0022`，使沙箱写出的产物对 Gateway 进程可读。若使用非 root 用户，必须额外验证 UID/GID 和 umask；V1 不承诺。
- canary 会验证“Gateway 写、沙箱读”和“沙箱写、Gateway 读”；权限不满足时直接失败，不进入缓存。
- canary 范围：
  - `/mnt/user-data` 做双向写读；探针放在 `workspace/.myflow-canary/`，完成后删除；清理失败只 warning，不阻断（隐藏文件可能出现在 `ls` 的深度 2 里，但不会出现在 uploads/outputs）；
  - 技能挂载只做存在性/可读性检查（`stat` 或读一个已知路径），不尝试写；只读性由集成测试验证；
  - `sandbox.mounts` 的额外挂载做存在性检查；写权限不自动测试，避免改动运维数据。

## 8. Provider 生命周期

### 8.1 缓存与 sandbox_id

- 缓存键：`(user_id, thread_id)`；无 thread 的通用实例用 `(user_id, None)`。
- 缓存值：`_CubeHandle`，包含：
  - `sandbox_id`：对外的 `cube:<remote_id>`；
  - `remote_id`：CubeSandbox 的 `sandbox_id`；
  - `sdk`：`cubesandbox.Sandbox` 实例；
  - `mounts`：本次挂载描述；
  - `lock_owner` / `lock_thread`：Redis 锁的 value 和续租线程；
  - `alive`：是否仍可用；
  - `created_at` / `last_used_at`。
- `get(sandbox_id)` 只做内存查找：
  - 解析 `cube:` 前缀；
  - 查 `_by_remote_id`；
  - `alive` 为 false 返回 `None`；
  - 不发 HTTP，不探活。
- `sandbox_id` 不直接使用 Cube 的裸 ID，避免和 local 的 `local:` 前缀混淆；工具层只把它当不透明字符串。

### 8.2 acquire

```text
acquire(thread_id, user_id)
  -> 校验 user/thread
  -> 进程内按 key 加锁（同一 key 只允许一个创建者）
  -> 抢 Redis per-thread 锁（等待上限 acquire_timeout_seconds）
  -> 缓存命中、alive、且 Redis 锁仍归本 handle：更新 last_used，返回
  -> 缓存命中但锁已丢失：标 dead，清缓存，重新抢锁
  -> 计算本 thread 的挂载描述
  -> 按 metadata 查找已有 sandbox
       -> 找到：校验 mounts_hash；Sandbox.connect(id)
       -> 找不到：Sandbox.create(...)
  -> canary 双向检查
  -> 失败：kill 新 sandbox、释放 Redis 锁，抛 SandboxRuntimeError
  -> 成功：写入缓存，启动续租线程，返回 cube:<remote_id>
```

创建参数：

```python
Sandbox.create(
    template=config.template_id,
    timeout=config.timeout_seconds,
    metadata={
        "myflow": "1",
        "myflow.version": "1",
        "myflow.user": user_id,
        "myflow.thread": thread_id or "__generic__",
        "myflow.generic": "0" if thread_id else "1",
        "myflow.instance": instance_id,
        "myflow.mounts_hash": mounts_hash,
        "host-mount": json.dumps(mounts, separators=(",", ":")),
    },
    lifecycle={
        "on_timeout": config.on_timeout,
        "auto_resume": config.auto_resume,
    },
    network={
        "allow_public_traffic": config.allow_public_traffic,
        **network_policy,
    },
    allow_internet_access=config.allow_internet_access,
)
```

注意事项：

- `metadata` 的值都转成字符串；CubeAPI 的 metadata 是字符串 map。
- `mounts_hash` 是对排序后的 host-mount 描述（含 `mountPath` / `hostPath` / `readOnly`）做 SHA-256 的前 16 位；用于发现阶段判断挂载是否漂移。
- `host-mount` 是 Cube 扩展，不是 E2B 标准字段；不要放进 E2B 兼容的其它字段。
- `network` 里的 `allow_out` / `deny_out` 从配置原样传递；`allow_public_traffic` 必须显式 `False`，否则沙箱端口可能被公网访问。
- 创建超时后不要盲目重试；先按 metadata 查找，避免重复创建两个挂载同一目录的沙箱。
- 如果创建失败错误里包含 `hostPath ... is not within an allowed mount prefix`，翻译成“检查 CubeMaster 的 allowed_host_mount_prefixes 与 cube.host_mount.host_root”。

### 8.3 metadata 发现

优先使用 CubeAPI 的 v2 列表：

```text
GET /v2/sandboxes?metadata=myflow=1&myflow.user=<uid>&myflow.thread=<tid>&state=running,paused
```

无 thread 的通用实例用 `myflow.thread=__generic__` 这个哨兵值，不能传空字符串：CubeAPI 把空 metadata 值当作“不过滤”。

官方 Python SDK 的 `Sandbox.list_v2()` 没有暴露 metadata 参数，所以 provider 需要一个小型控制面客户端：

- 复用 `cubesandbox.Config` 的 `api_url` / `api_key`；
- 用 `httpx` 或 `requests` 发请求；
- 处理 `x-next-token` 分页；
- 结果中再校验 `metadata["myflow.user"]` / `metadata["myflow.thread"]` 完全匹配，不能只信查询参数；
- 找到多个时选最早创建的一个，记录 warning；不要主动杀另外的，除非确认是本实例创建且没有租约持有者。
- 比较 `metadata["myflow.mounts_hash"]` 与本次计算值；不一致说明配置/技能路径变了，旧沙箱的 host-mount 已过期。已持 Redis per-thread 锁时，kill 旧沙箱并重建；拿不到锁或发现不是本实例的沙箱时，fail loudly，让运维清理，绝不带着旧挂载继续跑。

如果没有权限或接口不可用，退化为 `Sandbox.list_v2()` 客户端过滤；记录一次 warning，不阻塞。

### 8.4 release

```text
release(sandbox_id)
  -> 停止续租线程
  -> 用 SDK 执行 release_action
       kill: DELETE /sandboxes/:id
       pause: POST /sandboxes/:id/pause
  -> Lua compare-and-delete Redis 锁
  -> 从缓存移除
  -> 关闭 SDK client
```

- host mount 下宿主文件仍然存在，kill 不丢数据。
- 404 视为幂等成功。
- 503/409 可重试一次，带短退避；仍失败则记录 warning 并保留 `_pending_cleanup`，由下次 acquire 或 shutdown 再试。
- 无论 `release_action` 成功与否，都要在 `finally` 里释放自己的 Redis 锁（compare-and-delete），否则其它 worker 要等到 TTL。
- `release` 不删除宿主文件，不修改 `thread_files`。

### 8.5 release_async

`release` 里有远端 `kill`/`pause` 网络调用，不能直接在事件循环上跑。增加：

```python
class SandboxProvider(ABC):
    async def release_async(self, sandbox_id: str) -> None:
        await asyncio.to_thread(self.release, sandbox_id)
```

- `SandboxLeaseClient` 同时持有 sync `release` 和 async `release_async`；
- `SandboxLeaseManager.release_async()` 优先 await async 版本；
- `SandboxMiddleware.aafter_agent` 和 `release_sandbox_lease()` 走 async；
- 同步 graph 的 `after_agent` 仍可走 sync `release`，因为它本身运行在线程里；
- host-mount 下 release 不做数据同步，但仍然要避免把 kill/pause 的 HTTP 往返放在事件循环上。

### 8.6 reset / shutdown

- `reset()`：只清进程内缓存和 `_by_remote_id`，**不杀远端**；但必须停止续租线程，并用 Lua compare-and-delete 释放自己持有的 Redis 锁，避免锁挂到 TTL。下一次 acquire 通过 metadata 重新发现。
- `shutdown()`：对当前缓存中的 handle 逐个执行 `release_action`（kill，不用 pause），停止续租，删除自己的 Redis 锁，然后清空；不扫描全局沙箱。这样不会误杀别的 Gateway 进程的实例。
- 进程崩溃留下的沙箱由 `timeout_seconds` + `on_timeout` 回收；V1 不做全局 reaper。

### 8.7 LRU 与缓存上限

- `max_cached_threads` 只限制进程内 handle 数量。
- 驱逐时只丢 handle，**不 kill 远端**；但必须停止续租并删除自己的 Redis 锁，否则其它 worker 会被锁到 TTL。远端有 TTL / pause 兜底。
- 被驱逐的 thread 下次 acquire 重新抢锁，通过 metadata 重新发现并 `connect`。
- 不要照抄 local provider 的“驱逐即销毁”；那会误杀正在被其它 run 使用的沙箱。

## 9. Sandbox 实现

### 9.1 类结构

```python
class CubeSandbox(Sandbox):
    persistent_shell_sessions: bool | None = False

    def __init__(self, sandbox_id, sdk, mounts, config): ...

    # execute_command / read_file / write_file / download_file /
    # update_file / list_dir / glob / grep
    # shutdown
```

### 9.2 SDK 版本与 connect 语义

- 以 `cubesandbox>=0.7.3,<0.8` 为唯一支持区间；`host-mount` 是 Cube 扩展，不是 E2B 标准字段。
- 所有依赖的 SDK 面（`metadata["host-mount"]`、`Sandbox.health()`、`list_v2()`、`files.read(path, user=...)` 的文本语义、`commands.run(user=...)`、`lifecycle` / `network` 形状）都要在 P0 用真实 0.7.3 做一次接口契约测试，不能只凭文档。
- 真实 PyPI 的 cubesandbox 0.7.3 `Filesystem.read` 只有 `read(path, *, user=None) -> str`，没有 `format` 参数；二进制下载必须走沙箱内 base64 分块传输，不能依赖 `format="bytes"`。`Filesystem.write` 接受 `str | bytes`。
- `Sandbox.connect(id)` 会恢复 paused 沙箱；provider 不额外调用已废弃的 `resume()`。如果真实集群的 connect 行为不同，以集成测试为准并更新本节。

### 9.3 方法映射

| Sandbox 方法 | CubeSandbox 实现 |
|---|---|
| `execute_command` | `sdk.commands.run(command, timeout=timeout, envs=env, user=sandbox_user)`；合并 stdout/stderr；非零退出码追加 `exit code N` |
| `read_file` | `sdk.files.read(path, user=sandbox_user)`（0.7.3 没有 `format` 参数）；行范围用 `splitlines()` 截取；大文件先 `stat`，超过阈值用 `sed -n` |
| `write_file` | 先 `mkdir -p` 父目录，再 `sdk.files.write(path, content, user=sandbox_user)`；追加用按路径锁 + 读改写，或临时文件 + `cat >>` |
| `download_file` | 沙箱内 `dd` + `base64 -w0` 分块读取（256 KiB/块），Gateway 逐块解码后拼接为 `bytes`；0.7.3 不支持 `files.read(format="bytes")` |
| `update_file` | 先 `mkdir -p` 父目录，再 `sdk.files.write(path, bytes, user=sandbox_user)` |
| `list_dir` | `sdk.files.list()` 递归两层，复刻 `render_tree` 的排序和树形符号；空目录返回 `[]` |
| `glob` | 沙箱内 Python helper；返回 `(matches, truncated)` |
| `grep` | 沙箱内 Python helper；返回 `(matches, truncated)` |
| `execute_command_in_scope` | 不覆盖；Cube 每次都启动新的 `bash -lc`，没有持久 shell，scope 无意义 |

### 9.4 输出上限与超时

`commands.run` 会把 stdout/stderr 收进内存；必须限制输出，否则一条 `yes` 就能打爆 Gateway。

建议包装命令：

```bash
set -o pipefail
{ <command> ; } 2>&1 | head -c 1048576
status=${PIPESTATUS[0]}
if [ "$status" -eq 141 ]; then status=0; fi
exit "$status"
```

- 1 MiB 与 local provider 的 `_MAX_OUTPUT_BYTES` 对齐。
- 141 是 `head` 提前关闭管道导致的 SIGPIPE；对截断来说算成功。
- 命令墙钟超时沿用 `sandbox.bash_command_timeout`（默认 600s）：工具传了 `timeout` 就用它，否则用这个值；每次 `commands.run` 显式传 `timeout`，不能依赖 SDK client 的 `request_timeout_seconds`。
- `request_timeout_seconds` 只用于控制面/短文件 API；长命令的连接和读取由 per-call timeout 控制。P0 必须用真实 600s 命令验证，避免 30s transport timeout 悄悄截断长任务。
- stdin 不可用；交互式命令会直接 EOF，和本地 `/dev/null` 一致。

### 9.5 glob / grep

为了保持与本地 provider 的正则/glob 语义一致，不让模型在两个 provider 之间看到不同的通配符行为：

- 在沙箱内放一个很小的 Python helper（例如 `/tmp/.myflow/search.py`，创建时用 `files.write` 写入）；
- helper 接收 base64 编码的 JSON 参数，执行：
  - `glob`：`os.walk` + `PurePosixPath.match` / `fnmatch`，支持 `**`、`*`、`?`、`[...]`；
  - `grep`：逐文件 UTF-8 `errors="replace"` 读取，Python `re` 匹配，支持 `literal` / `case_sensitive` / `glob` 过滤；
- 输出 JSON：`{"matches": [...], "truncated": true|false}`；
- 在 helper 内做 `max_results` 截断，不能把上万条传回 Gateway；
- 模板必须提供 `python3`（3.10+）。如果模板没有 Python，provider 在创建时直接报模板不满足要求，不做 `grep` 方言降级。

### 9.6 输出反向遮蔽

`CubeSandbox` 要暴露 `path_mappings`，供 `mask_host_paths()` 把宿主路径换回虚拟路径：

```python
self.path_mappings = [
    PathMapping(
        container_path=mount.mountPath,
        local_path=mount.hostPath,
        read_only=mount.readOnly,
    )
    for mount in mounts
]
```

- `mount.hostPath` 是 Cubelet 侧路径，可能出现在 CubeAPI/Cubelet 的错误里；
- 如果 `local_root != host_root`，再追加一份 `local_root` 侧的 `PathMapping`，避免 Gateway 自己的异常泄露本地路径；
- `mask_host_paths()` 只用于工具输出和错误脱敏，不参与路径安全判断；安全判断在 `assert_virtual_path()`。

### 9.7 错误映射

| Cube/底层异常 | 映射 |
|---|---|
| `FilesystemNotFoundError` + `list_dir` | `FileNotFoundError` |
| `FilesystemNotFoundError` + `read/download` | `OSError` |
| `ApiError` 404/410 | 标记 handle dead；抛 `SandboxNotFoundError` |
| `AuthenticationError` / 401/403 | `SandboxRuntimeError`，提示检查 api_key |
| `TemplateNotFoundError` | `SandboxRuntimeError`，提示 template_id |
| `httpx.TimeoutException` / `requests.Timeout` + 命令 | `SandboxTimeoutError` |
| 文件 API 超时/连接错误 | `OSError` |
| 路径 guard 拒绝 | `PermissionError` |
| 非法 env key | 基类 `ValueError`，provider 不重复检查 |

所有错误信息：

- 不回显 API key；
- 不把 CubeAPI 的完整 URL 带查询参数拼进模型可见文本；
- 保留可操作提示（检查 host mount 前缀、模板、网络、权限）；
- 走 `mask_host_paths()`，避免宿主路径进入模型上下文。

## 10. 路径安全

远程沙箱没有宿主路径映射表，不能复用 `resolve_sandbox_path()` 的 `PathMapping.local_path`。新增一个轻量类型：

```python
@dataclass(frozen=True)
class VirtualMount:
    container_path: str
    read_only: bool = False
```

```python
def assert_virtual_path(path: str, mounts: list[VirtualMount], *, write: bool = False) -> str:
    # 纯字符串检查：
    # - NUL
    # - 非绝对路径
    # - "://"
    # - Windows 盘符 / UNC
    # - 任何 ".." 段
    # - posixpath.normpath 后仍以 "/" 开头
    # 然后按最长 container_path 匹配；没有匹配 -> PermissionError
    # write 且匹配项 read_only -> PermissionError
    # 返回规范化后的 POSIX 路径
```

规则：

- `read_file` / `list_dir` / `glob` / `grep` / `download_file` 只允许读挂载点内。
- `write_file` / `update_file` 只允许写非 `read_only` 挂载点内。
- `execute_command` 不做路径 guard；bash 可以在 MicroVM 内探索。隔离边界是 MicroVM，不是路径表。
- 额外挂载只允许 `sandbox.mounts` 配置的容器路径；模型不能新增挂载。
- 不使用 `Path.resolve()` 在沙箱内做安全判断；路径在远端，Gateway 无法解析。使用 POSIX 规范化和前缀匹配，拒绝 `..` 而不是试图解析它。

## 11. 技能挂载

### 11.1 git 来源

- `skills.source=git`：
  - 私有技能：`<container>/private` ← `skills.git.cache_dir/active/users/{uid}`，只读；
  - 内置技能：`<container>/builtin` ← `skills.builtin.path`（如果存在），只读；
  - 不在进程级挂载 `active/users` 根，也不挂 `active` 根。
- `supports_per_user_skill_mounts=True`，`skills_catalog` 会向前端/提示词暴露 `<container>/private`。
- `supports_agent_skill_isolation=True`：技能只读挂载，且每个用户只看到自己的私有树。

### 11.2 filesystem 来源

- `skills.source=filesystem`：
  - 整个 `skills.root_dir()` 挂到 `<container>`，只读；
  - 如果 `skills.builtin.path` 位于该根之下，不重复挂载；
  - 如果位于根之外，额外挂 `<container>/builtin`，只读。
- filesystem 来源没有 active 私有树，`supports_per_user_skill_mounts` 仍然返回 `True` 吗？
  - 本设计返回 `False`：没有私有 active 树，暴露 `<container>/private` 会指向不存在的目录。该值只影响前端/提示词是否显示私有路径；filesystem 来源本来就不使用私有路径。
  - 如果未来 filesystem 来源也支持私有目录，再改回 `True`。

### 11.3 启动与物化时序

- `skills_catalog` 启动时会物化私有 active 树；provider 的 `acquire` 发生在首次工具调用，通常晚于启动。
- 如果 `private_root` 不存在，provider `mkdir -p` 空目录，保证 host-mount 源存在；后续物化写入同一目录，挂载立即可见。
- 如果 catalog 启动失败，`skills_catalog` 会 fail-closed；provider 仍可创建空目录，但提示词不会有私有技能。
- 内置技能目录缺失时跳过挂载并 warning；不要为了挂载创建项目目录，避免掩盖打包错误。

## 12. 上传与产物

### 12.1 不需要改 `thread_files.py`

`thread_files._mappings()` 用的是：

```
{local_root}/.myflow/users/{uid}/threads/{tid}/user-data/{workspace,uploads,outputs}
```

host-mount 把同一个 `user-data` 挂到 `/mnt/user-data`。因此：

- `POST /uploads` 写入宿主 `uploads`，沙箱立即可见；
- `GET /uploads/list` 读宿主，和本地 provider 一致；
- `DELETE /uploads/{filename}` 删除宿主，沙箱立即可见；
- `GET /artifacts/{path}` 读宿主 `outputs`，沙箱写出的产物立即可见；
- 不需要 flush、同步、临时文件、tar 解包。

### 12.2 唯一需要注意的权限问题

- 沙箱写出的文件必须对 Gateway 进程可读，否则 `FileResponse` 会 404/500。
- canary 会验证“沙箱写 → Gateway 读”；不满足时 provider 直接失败，并提示：
  - 检查模板 umask（推荐 `0022`）；
  - 检查 `sandbox_user` 与宿主目录权限；
  - 检查同机宿主目录的 UID/GID 和 umask。
- 已确认宿主目录在 Ubuntu 本地文件系统（按 ext4 处理）上，不存在 NFS `root_squash`；canary 仍要验证 guest root 写出的文件对 Gateway 进程可读、可删除。如果未来换成 NFS/CephFS 且开启 `root_squash`，canary 会在首次 acquire 暴露权限问题，届时需要权限归一化或改非 root 用户。
- `Paths.ensure_thread_dirs()` 目前是 `0777`；V1 保留，避免沙箱用户无法写入。后续可收紧到 `0770` + 共享组。

## 13. 多 Gateway worker

### 13.1 V1：Redis per-thread 锁（必选）

host-mount 让多个 worker 共享同一份文件；如果两个 worker 各自创建一个 sandbox，它们会同时写同一份 workspace，产生命令级竞态。V1 就用 Redis per-thread 锁串行化，不依赖 sticky routing，也不提供无锁单 worker 回退。

配置：

```yaml
cube:
  lease:
    redis_url: $REDIS_URL
    ttl_seconds: 30
```

- `redis_url` 必填；启动时 `PING` 失败直接拒绝启动。
- 新增 optional extra `agentkit[redis]` 依赖；已有 `redis` extra 可复用。
- 没装 Redis 客户端或 Redis 不可达时，Cube provider 失败关闭，不回退到本地 provider。

### 13.2 锁的实现

锁 key：

```text
myflow:sandbox:{user_id}:{thread_id or "__generic__"}
```

锁 value：

```text
{instance_id}:{uuid4}
```

算法：

1. 用同步 `redis.Redis` 客户端（`decode_responses=True`），因为 provider 的同步方法本来就在 worker thread 里跑；async 路径用 `asyncio.to_thread` 包 `acquire` / `release`。
2. `SET key owner NX PX {ttl_ms}`；
3. 拿不到锁时循环等待，单次 sleep 0.1–0.5s，总时限 `acquire_timeout_seconds`；超时抛 `SandboxRuntimeError`（可重试，提示 thread busy）。
4. 拿到锁后才允许 metadata 发现 / create / connect。
5. 每个 handle 启动一个 daemon 续租线程，每 `ttl/3` 用 Lua 做“value 仍等于 owner 才 PEXPIRE”的续租；续租失败或 value 被替换时：
   - 把 handle 标为 dead；
   - 记录 warning；
   - **不要 kill/pause 远端**，另一个 worker 可能已经接管；
   - 后续工具调用返回 `SandboxNotFoundError`，runtime 重新 acquire（重新抢锁）。
6. `release` 前用 Lua 做 compare-and-delete：
   - value 仍是自己的：执行 `release_action`（kill/pause），再删除锁；
   - value 已不是自己的：不执行 kill/pause，只清本地 handle。
7. 进程退出 / `shutdown()` 时停止续租线程；锁的 TTL 兜底，避免 Gateway 崩溃后永久占锁。
8. 命令最长可跑 `bash_command_timeout`（默认 600s），续租线程必须独立于命令，不能只在工具调用开始时续租。

Redis 故障语义：

- 启动时不可达：拒绝启动。
- 运行中续租失败：handle dead；在途命令可能继续跑完，但不再接受新调用；下一次 acquire 会重新抢锁。
- 不要把“Redis 暂时不可达”当成“锁还在”；TTL 过期后另一个 worker 可能接管，继续写会冲突。

### 13.3 metadata 发现与锁的关系

- 锁是“谁有权操作这个 thread 的沙箱”；
- metadata 是“这个 thread 已经有哪些沙箱”；
- 先拿锁，再发现，再 create；不要在锁外 create。
- 发现多个时选最早的一个；如果已持锁，可以 kill 其余本实例创建的重复项；不是本实例的只 warning，不 kill。
- `mounts_hash` 漂移时，持锁者可以 kill 旧沙箱并重建；未持锁者 fail loudly。

## 14. 失败模式与可观测性

### 14.1 失败模式

| 场景 | 行为 |
|---|---|
| CubeAPI 不可达 | startup healthcheck 失败（可配关闭）；首次 acquire 失败，工具返回明确错误 |
| 模板不存在 | `SandboxRuntimeError`，提示检查 `template_id` |
| hostPath 不在允许前缀 | 创建失败，提示检查 CubeMaster `allowed_host_mount_prefixes` |
| 挂载源不存在 | 启动校验失败，或 acquire 前 `ensure_thread_dirs` 创建；extra mount 缺失直接失败 |
| canary 失败 | kill 新沙箱，清缓存，抛错；不把坏挂载交给模型 |
| 沙箱被 TTL 回收 | 下一次命令 404/410 → handle dead；runtime 重新 acquire，host 文件仍在 |
| Gateway 重启 | 缓存空；metadata 发现 + connect，或重建；host 文件仍在 |
| 多 worker 竞争 | V1 Redis per-thread 锁串行化；抢锁超时返回可重试错误 |
| Redis 不可达 | 启动时拒绝启动；运行中续租失败则 handle dead，不继续接受新调用 |
| 沙箱写文件权限不可读 | canary 失败，提示 umask/UID |
| 技能目录未物化 | 创建空 private 目录；catalog 物化后立即可见；记录 warning |

### 14.2 日志与指标

日志字段：

- `sandbox_id`（`cube:<remote>`）
- `user_id` / `thread_id`（可脱敏）
- action：`acquire` / `connect` / `create` / `canary` / `release` / `pause`
- duration_ms
- mount count / read-only count
- error class + sanitized message

不要记录：

- API key
- 完整 hostPath（生产日志只记相对 `local_root` 的路径）
- 模型输出内容

指标建议：

- `sandbox_acquire_seconds`
- `sandbox_create_total`
- `sandbox_connect_total`
- `sandbox_canary_failures_total`
- `sandbox_release_failures_total`
- `sandbox_active_handles`

## 15. 测试设计

### 15.1 单元测试（不依赖真实 Cube）

- `test_cube_mounts.py`
  - `to_host_path()` 同路径 / 不同路径 / 逃逸路径；
  - git 来源私有 + builtin 挂载；
  - filesystem 来源整树挂载，builtin 在树内不重复；
  - `sandbox.mounts` 保护路径过滤；
  - 重复 container_path、非法路径、缺失源；
  - 父挂载先于子挂载。
- `test_cube_sandbox_contract.py`
  - 用 fake SDK 覆盖 `Sandbox` 契约；
  - `list_dir` 空 / 缺失 / 失败三态；
  - `glob` / `grep` 截断；
  - `read_file` 行范围；
  - `write_file` 追加；
  - 路径 guard 读写；
  - 错误映射表。
- `test_cube_provider_lifecycle.py`
  - acquire 缓存命中；
  - metadata 发现并 connect；
  - create 后 canary；
  - canary 失败 kill；
  - release kill/pause；
  - reset 不杀；
  - shutdown 只杀自己的；
  - 404/410 标记 dead；
  - LRU 驱逐不杀远端。
- `test_cube_mount_canary.py`
  - 双向写读；
  - 权限失败；
  - 清理隐藏文件；
  - token 不出现在日志。

### 15.2 集成测试（真实 Cube）

用环境变量开启，CI 默认跳过：

```bash
CUBE_API_URL=http://... \
CUBE_TEMPLATE_ID=... \
CUBE_PROXY_NODE_IP=... \
CUBE_INTEGRATION=1 \
pytest -m cube_integration
```

验收：

1. `bash` 执行 `echo hello`。
2. 文件工具读写 `/mnt/user-data/workspace/main.py`。
3. `POST /uploads` 上传文件后，沙箱 `ls /mnt/user-data/uploads` 可见。
4. 沙箱写 `/mnt/user-data/outputs/report.md`，`GET /artifacts/report.md` 可下载。
5. `/mnt/skills/private/<name>/SKILL.md` 可读。
6. `/mnt/skills/builtin/<name>/SKILL.md` 可读且只读（写操作失败）。
7. `../../etc/passwd` 被文件工具拒绝；bash 内部访问 `/etc/passwd` 仍允许（隔离边界）。
8. 两个子代理共享同一沙箱；最后持有者释放。
9. Gateway 重启后同 thread 能重新发现或重建，宿主文件仍在。
10. 切换回 local provider，原文件仍可读。

### 15.3 多 worker 测试

- `fakeredis` 测试 `SET NX PX`、续租、释放、超时；
- 两个 worker 同时 acquire 同一 thread，只有一个 create；
- 锁丢失后 handle dead，不误杀。

## 16. 实施顺序

### P0：provider + host mount 基础（约 3–4 天）

- 配置模型与 `pyproject.toml` extra（`cube` + `redis`）；
- `CubeSandboxProvider` 构造、`startup_check()`（Cube healthcheck + 挂载源；Redis PING 随 P2 锁实现接入）、缓存；
- `release_async` 基类 + lease/middleware 接线；
- `CubeSandbox` 文件/命令实现；
- mount 描述构建 + 路径翻译；
- fake 契约测试。

如果真实 Cube 集群尚不可用，P0 可以只做 fake-SDK 和配置/挂载描述；P1 之后的 canary、技能挂载、上传/产物、重启恢复全部阻塞在集群可用性上，不要把“代码写完”当成“可验收”。

### P1：canary + 技能 + 上传/产物验收（约 2–3 天）

- 双向 canary；
- git/filesystem 技能挂载；
- `thread_files` 端到端验收（预期无需改代码）；
- 错误映射和日志；
- 真实 Cube 集成测试。

### P2：生命周期与多 worker（约 3–4 天）

- metadata 发现 + connect + mounts_hash 漂移处理；
- Redis per-thread 锁、续租线程、Lua compare-and-delete；
- 多 worker 并发 acquire/release、锁丢失、Redis 重启；
- 指标和故障注入。

### P3：硬化（约 2–3 天）

- 大文件/大输出/超时/并发测试；
- 模板校验脚本；
- 部署文档；
- 回滚演练。

总计约 2 周 MVP，3 周生产化。具体取决于真实 Cube 集群是否已可用。

## 17. 回滚与兼容

- 回滚：`sandbox.use` 改回 `agentkit.sandbox.local:LocalSandboxProvider`，重启 Gateway。
- 不需要数据库迁移；不需要移动宿主文件。
- local 和 cube 共用同一份 `user-data` 与技能树，切换后文件仍在。
- 旧 checkpoint 里的 `sandbox_id` 如果指向 `cube:`，切回 local 后 `get()` 返回 `None`，runtime 重新 acquire local 沙箱。
- 旧 checkpoint 里的 local id 切到 cube 同理。
- `reset_sandbox_provider()` 在测试中会调用 `shutdown()`；Cube 测试必须用 fake/临时集群，不能指向共享生产 Cube。

## 18. 留给 builder 的决定

这些不是需求缺口，实施时可以自行决定：

- metadata 发现用 `httpx` 还是 `requests`；建议复用 `cubesandbox` 已有的依赖，避免新增。
- Python search helper 放 `/tmp/.myflow/search.py` 还是模板 `/opt/myflow/search.py`；建议运行时写入，减少模板耦合。
- `read_file` 大文件阈值；建议 256 KiB 以下直接 `files.read`，以上用 `sed -n`。
- 是否在 `CubeSandboxProvider` 里缓存 SDK client；建议每个 handle 一个，release 时 close。
- canary 文件命名；建议 `.myflow-mount-check-in` / `.myflow-mount-check-out`，失败时保留用于诊断。
- 指标实现用现有日志还是新增 Prometheus 依赖；V1 可以只做结构化日志。

## 19. 需要运维确认的前置条件

1. Gateway 与 Cubelet 在同一台 Linux 主机上，看到同一个项目根，且该根在 `allowed_host_mount_prefixes` 下。
2. CubeSandbox 集群已安装，`CUBE_API_URL` / `CUBE_TEMPLATE_ID` / 可选 `CUBE_PROXY_NODE_IP` 可用。
3. 模板已预创建 `/mnt/user-data` 和技能目录，包含 `bash`、`python3`、`tar`、`grep`、`find`、`sed`。
4. Gateway 进程对项目根有读写权限；沙箱写出的文件对 Gateway 可读（canary 会验证）。
5. Redis 可用，`REDIS_URL` 可从 Gateway 访问；多 worker 共用同一个 Redis 实例/DB。
6. 如果使用非 root 沙箱用户：已确认 UID/GID 映射和 umask；V1 默认 root。
7. 同机项目根必须覆盖 `.myflow` 和技能树：`local_root` 默认 `project_root()`，因为 `.myflow` 和 `skills/` 都在项目根下；不需要额外的共享卷。

## 20. 验收标准

- [ ] `sandbox.use` 切到 Cube 后，Gateway 启动时能通过 `startup_check()`（Cube healthcheck + 挂载源 + Redis PING）。
- [ ] 首次 acquire 创建沙箱，canary 双向通过。
- [ ] 文件工具、bash、glob、grep 在 Cube 沙箱内工作。
- [ ] 上传文件、沙箱读取、沙箱写产物、浏览器下载四条链路端到端通过。
- [ ] 私有技能和内置技能在沙箱内可见，写操作被拒绝。
- [ ] 路径逃逸被文件工具拒绝。
- [ ] 子代理共享沙箱，最后持有者释放。
- [ ] Gateway 重启后同 thread 可恢复，宿主文件不丢。
- [ ] 两个 Gateway worker 同时 acquire 同一 thread，只有一个能创建沙箱；另一个等待 Redis 锁。
- [ ] Redis 不可达时启动失败；运行中锁丢失时 handle dead，不误杀别人的沙箱。
- [ ] 回滚到 local provider 后同一份数据仍可读。

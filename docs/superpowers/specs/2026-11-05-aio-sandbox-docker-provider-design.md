# AIO Sandbox（Docker 容器隔离）Provider 设计

> 状态：待确认（2026-11-05）
> 关系：新增第四个 `SandboxProvider`，与 `agentkit.sandbox.local`（进程内路径映射）、
> `agentkit.sandbox.cube`（CubeSandbox MicroVM / E2B 协议）并列；不改动它们的语义。
> 上游参考：deerflow 的 `deerflow.community.aio_sandbox:AioSandboxProvider`（Docker 模式）。
> 目标 provider 路径：`agentkit.sandbox.aio_sandbox:AioSandboxProviderDocker`
> 配置段：`sandbox.aio`（新增）
> 对照：宿主挂载模型与租约见 [CubeSandbox 宿主挂载设计](2026-11-04-cubesandbox-host-mount-design.md)，
> SDK 适配层的写法见 [CubeSandbox E2B 协议迁移设计](2026-11-04-cubesandbox-e2b-protocol-design.md)。

## 1. 为什么要再加一个 provider

现有三个选择各自缺一块：

| provider | 隔离级别 | 需要什么 | 缺什么 |
|---|---|---|---|
| `local` | 无（同进程路径映射） | 什么都不需要 | 不是隔离边界；宿主 bash 默认关掉 |
| `cube` | MicroVM | CubeMaster + Cubelet + wildcard DNS + Redis | 部署重；本地/单机开发几乎跑不起来 |
| **`aio_sandbox`（本设计）** | **容器** | **一台能跑 Docker 的 Linux 主机** | 隔离弱于 MicroVM（共享内核），但足以把模型执行的命令关进容器 |

使用场景：

1. 单机部署：一台 Linux 机器上跑 Gateway + Docker，不需要 CubeMaster/K8s。
2. 开发与验收：本机（含 Docker Desktop）就能验证"命令真的在容器里跑"这条路径。
3. 富运行时：AIO 镜像自带 python/node/jupyter 与浏览器工具，后续要做
   "在沙箱里跑 notebook / 打开页面"时不用换 provider。

非目标：

- 不做 Kubernetes / 远程 provisioner 模式（deerflow 的 `provisioner_url` 分支）。
- 不做 AIO 的 browser/VNC/mcp 能力接入（本设计只落地我们 `Sandbox` 契约需要的
  file / shell / bash 三组 API）。
- 不改 Redis 租约、`thread_files.py`、上传/产物链路。
- 不追求 deerflow 那套 quarantine / 跨进程 generation 记账（见 §11.2）。

## 2. 上游事实（已核对，不是推测）

### 2.1 镜像

- 默认镜像：`enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest`。
- 容器内 AIO server 默认端口 **8080**；就绪探针是 `GET /v1/sandbox` 返回 200。
- 镜像自带 `/mnt/user-data` 约定：deerflow 把宿主
  `users/{user_id}/threads/{thread_id}/user-data/{workspace,uploads,outputs}`
  挂到 `/mnt/user-data/{workspace,uploads,outputs}`，与我们的线程目录布局**完全一致**。
- `bash.exec`（带 `env` 注入、`hard_timeout`）要求镜像 **>= 1.9.3**；旧镜像整个
  `/v1/bash/*` 返回 404。我们的 provider 依赖它（见 §6.1），因此**必须 pin 版本**。

### 2.2 数据面 SDK：`agent-sandbox`（PyPI，0.0.30）

在隔离环境装真包核对过签名（`Python SDK for the All-in-One Sandbox API, >=1.7.0`）：

```text
Sandbox(*, base_url: str, headers: dict[str,str] | None = None,
        timeout: float | None = None, follow_redirects=True, httpx_client=None)

sandbox.file.read_file(*, file, start_line=None, end_line=None, sudo=None)
sandbox.file.write_file(*, file, content, encoding='utf-8'|'base64'|'raw', append=None, sudo=None)
sandbox.file.download_file(*, path)                      -> Iterator[bytes]
sandbox.file.upload_file(*, file: bytes|IO, path=None)   -> ResponseFileUploadResult
sandbox.file.list_path(*, path, recursive=None, max_depth=None, include_size=None, ...)
sandbox.file.glob_files(*, path, pattern, files_only=None, max_results=None, ...)
sandbox.file.grep_files(*, path, pattern, include=None, case_insensitive=None,
                        fixed_strings=None, max_results=None, ...)
sandbox.file.replace_in_file(*, file, old_str, new_str, sudo=None)

sandbox.bash.create_session(*, session_id=None, exec_dir=None, snapshot_path=None)
sandbox.bash.exec(*, command, session_id=None, env=None, async_mode=None,
                  timeout=None, hard_timeout=None, max_output_length=None)
sandbox.bash.output(*, session_id, command_id=None, offset=None, stderr_offset=None,
                    wait=None, wait_timeout=None)
sandbox.bash.kill(*, session_id, signal=None)
sandbox.bash.close_session(session_id)

sandbox.shell.exec_command(*, command, id=None, exec_dir=None, async_mode=None,
                           timeout=None, strict=None, no_change_timeout=None,
                           hard_timeout=None, truncate=None)   # 持久 PTY 会话
```

依赖面：`httpx` + `pydantic`（运行期），`volcengine-python-sdk`（安装期）。
已实测：`import agent_sandbox` 与 `Sandbox(...).file/bash/shell` 都**不会**导入
volcengine（`sys.modules` 里没有任何 volc 模块），所以它是"装得重、跑得轻"。

### 2.3 上游命令语义（deerflow 踩过的坑）

- 旧的持久 PTY 通道（`/v1/shell` + `shell.exec_command`）在**并发**下会返回
  `ErrorObservation` 而不是真实输出（deerflow 为此加了进程内锁 + 检测重试）。
- 带 `env` 的命令只能走 `/v1/bash/exec`：每次调用**新建并显式释放**一个 bash 会话，
  这样请求级 secret 不会留在会话里。
- `bash.exec` 的返回带 `status`：`running` / `completed` / `timed_out` / `killed` /
  `terminated`；退出码在 `command.exit_code`。
- 一次 `bash.exec` 有两条超时：`timeout`（命令自身）与 `hard_timeout`（服务端强杀）。

## 3. 方案选择

### A. `agent-sandbox` SDK（数据面）+ Docker CLI（控制面）——推荐

- 数据面：`file` / `bash` 两组 API，原生行范围读取、原生 bytes、原生 glob/grep。
- 控制面：`docker run/inspect/start/stop/rm`，我们自己掌握挂载、端口、labels、回收。

优点：能力最全、协议由上游维护、我们只写映射；控制面用 CLI，不需要引入 docker
Python SDK，也不会被 Docker SDK 的版本/上下文语义拖住。
代价：新增一个可选依赖（`agent-sandbox`）；必须给容器发一个 loopback HTTP 端口。

### B. 只用 Docker CLI（`docker exec` + `docker cp`），不发 HTTP——次选

优点：零 SDK 依赖、容器不暴露任何端口、二进制走 `docker cp` 很快、
`persistent_shell_sessions` 天然为 `False`。
代价：`glob`/`grep`/行范围读取要么再往容器里塞 helper（`cube/files.py` 已有先例），
要么靠镜像里的 `find`/`grep`；AIO 镜像的富能力（browser/jupyter/code）以后接不进来；
每条命令一次 `docker exec`，进程开销比 HTTP 高。

### C. Python `docker` SDK + AIO HTTP

只在"控制面也想要对象模型"时才有意义；`docker` 包要跟宿主 daemon API 版本对齐，
比 CLI 更难排查。不选。

**决策：A。** 理由与我们为 Cube 选官方 E2B SDK 的理由一致：协议由上游维护，
我们只写"契约 → SDK"的适配层；同时把 B 记录为依赖不可用时的降级路线（§11.2），
因此 `client.py` 必须是可替换的协议边界。

## 4. 总体架构

```text
tool 层（bash / read_file / write_file / glob / grep ...）
        │  只认 Sandbox 契约
        ▼
agentkit.sandbox.aio_sandbox:AioSandboxProviderDocker   ── provider 生命周期
        │                                                  acquire / get / release / reset / shutdown
        ├── docker.py     控制面：create/start/stop/rm/inspect/labels/端口/就绪探针
        ├── client.py     数据面：SDK 适配（协议 + fake 可替换）
        ├── sandbox.py    AioSandboxImpl：契约方法 → SDK 调用 + 错误映射 + 路径守卫
        └── provider.py   per-thread 缓存、LRU、（可选）Redis 租约、孤儿报告
```

复用（**需要一次小的搬迁**，见 §8）：

- 宿主挂载规划：`build_host_mounts` / `MountEntry` / `mounts_hash` / `validate_mount_sources`
  （现在在 `agentkit.sandbox.cube.mounts`）。
- Redis per-thread 锁：`RedisThreadLock` / `LockLease`（现在在 `agentkit.sandbox.cube.locks`）。
- 路径守卫：`assert_virtual_path` / `VirtualMount` / `PathMapping`（`agentkit.sandbox.security`）。
- 树渲染：`agentkit.sandbox.local.list_dir.render_tree`。

## 5. 容器控制面设计

### 5.1 容器身份

- 名字：`<container_name_prefix>-<hash12>`，
  `hash12 = sha256(user_id + "\0" + (thread_id or "__generic__"))[:12]`。
  确定性命名让"进程重启后能捡回同一个容器"，也让 `docker run` 的名字冲突成为可识别信号。
- labels（`docker run --label`）：

| label | 值 | 用途 |
|---|---|---|
| `myflow` | `1` | 粗筛，别误删别人的容器 |
| `myflow.provider` | `aio` | 与未来其它 Docker provider 区分 |
| `myflow.user` | `user_id` | 发现与审计 |
| `myflow.thread` | `thread_id` 或 `__generic__` | 发现与审计 |
| `myflow.instance` | provider 实例 id（进程内随机） | 判断"是不是本进程拥有的" |
| `myflow.mounts_hash` | 挂载指纹（16 hex） | 挂载变了要重建容器 |
| `myflow.created_at` | unix 秒 | 孤儿判断 |

### 5.2 创建

1. `build_host_mounts()` 得到 `MountEntry` 列表（父挂载在前，沿用现有排序）。
2. `docker run -d`：`--name`、`--label ...`、
   `--mount type=bind,source=...,target=...[,readonly]`（用 `--mount` 而不是 `-v`：
   路径里可能有逗号/冒号）、`-p 127.0.0.1::8080`（随机宿主端口，只绑 loopback）、
   `--cap-drop=ALL --security-opt no-new-privileges`、`--memory`、`--cpus`、`--pids-limit`、
   可选 `--network`。
3. `docker inspect` 读 `NetworkSettings.Ports["8080/tcp"][0].HostPort`，拼出
   `base_url = http://127.0.0.1:<port>`。
4. 就绪：轮询 `GET {base_url}/v1/sandbox`，预算 `ready_timeout_seconds`（默认 60s，
   与 deerflow 的 `SANDBOX_LOCAL_PROVIDER_READY_TIMEOUT` 同量级）。超时 → `docker rm -f` →
   `SandboxRuntimeError`。
5. 建 AIO 客户端 `Sandbox(base_url=...)`。
6. 跑 mount canary（宿主写 → 容器读 → 容器写 → 宿主读），失败 → `rm -f` →
   `SandboxRuntimeError("host mount canary failed: ...")`。

**不做** deerflow 的端口预分配表：`-p 127.0.0.1::8080` 让 Docker 自己选端口，
冲突由内核处理，省掉 `PortAllocator` 与"端口泄漏"记账。

### 5.3 复用与发现

- 内存命中：`(user, thread)` → handle（provider 契约要求 `get()` 是纯内存查询）。
- 内存未命中且 `discover_existing: true`：
  `docker ps -a --filter label=myflow.user=<u> --filter label=myflow.thread=<t>` →
  `docker inspect` 校验 `myflow.mounts_hash`：
  - 一致且容器 running → 按 §5.2 步骤 3/5 重连；
  - 一致但容器 exited → `docker start` + 就绪探针 + 重连（这是 `release_action: stop` 的热恢复）；
  - 不一致 → `rm -f` 后重建（与 cube 的 stale mount 处理一致：只在拿到本线程锁后做）。
- 多个匹配容器（上次崩溃残留）：保留 `created_at` 最早的；属于本实例的多余容器 `rm -f`，
  不属于本实例的交给孤儿策略（§5.6）。

### 5.4 释放语义

| 动作 | 我们对容器的处理 |
|---|---|
| `release(sandbox_id)`（最后一个租约持有者离开） | `release_action`：`stop`（默认，保留容器与可写层，下一轮 `start`）或 `remove`（`rm -f`） |
| `reset()` | 只丢进程内 handle 与租约，**不碰容器**（provider 契约） |
| `shutdown()` | 对每个已缓存 handle 做 `rm -f`（进程退出即销毁） |
| LRU 驱逐（超过 `max_cached_threads`） | `stop`（省 CPU/内存），不 `rm`：容器仍可被下一次 acquire 捡回 |
| 容器已不存在（`No such object`） | 视为 dead：丢 handle + 放租约，让 runtime 重新 acquire（同 cube 的 `SandboxNotFoundError`） |

### 5.5 多 worker 与租约

- 一台 Docker daemon 上，两个 Gateway worker 共用同一 `(user, thread)` 容器会互相
  `stop`。因此：
  - `sandbox.aio.lease` **可选**：配了就复用 `RedisThreadLock`（key
    `myflow:sandbox:{user}:{thread or __generic__}`，TTL 30s，续租/丢失语义与 cube 完全一致）。
  - 没配时 `startup_check()` 在 `GATEWAY_WORKERS > 1` 的情况下**拒绝启动**并说明原因
    （宁可起不来，也不要两个 worker 互相把容器停掉）。

### 5.6 孤儿容器

- `orphan_policy: report`（默认）：`startup_check()` 只列出"带我们的 label 但 instance
  不是本进程"的容器，写一条 warning + 一条可直接执行的 `docker rm -f <ids>` 提示。
- `orphan_policy: remove`：启动时把 `created_at` 早于 `orphan_ttl_seconds` 的孤儿删掉。
- 不做 deerflow 的 quarantine/generation 记账：我们的模型是"容器可丢弃 + 宿主目录是唯一真相"，
  丢一个容器最多丢一次冷启动时间。

## 6. 数据面设计（契约 → AIO API）

### 6.1 `execute_command`

**每次调用一个新建、用完即删的 `bash` 会话**，不做 PTY 复用：

```text
session = bash.create_session()
try:
    resp = bash.exec(command=command, session_id=session, env=env,
                     hard_timeout=timeout, max_output_length=max_output_bytes,
                     async_mode=False, request_options={timeout: timeout + grace})
finally:
    bash.close_session(session)
```

- `persistent_shell_sessions = False`：cwd/export 不跨调用保留（与 local / cube 一致），
  同时天然规避上游记录的 PTY 并发损坏问题。
- `env` 走结构化字段，不拼进命令字符串：M9 的请求级 token 不会进 shell 历史，
  也不会留在容器 env（`docker inspect` 看不到）。
- `hard_timeout` = `sandbox.bash_command_timeout`（服务端强杀，子进程一起死）。
- 输出截断交给 `max_output_length`（服务端截），**退出码仍由客户端追加**
  `exit code N`，因此截断不会吃掉这一行。
- status 映射：

| AIO `status` | 我们的行为 |
|---|---|
| `completed` / `None` | 正常返回文本；`exit_code != 0` 追加 `exit code N` |
| `timed_out` | `SandboxTimeoutError("Command timed out after Xs")` |
| `killed` | `SandboxRuntimeError("... was killed")` |
| `running` / 其它 | `SandboxRuntimeError`（不重试、不重放：结果未知） |
| `bash.exec` 404 | `SandboxRuntimeError`，文案指明镜像版本要求（>= 1.9.3） |

- `execute_command_in_scope(scope=...)`：V1 直接透传（每次调用本来就是独立会话），
  但把 `scope` 记进日志，便于子代理并发排查。

### 6.2 文件

| 契约 | AIO 调用 | 备注 |
|---|---|---|
| `read_file(path, start_line, end_line)` | `file.read_file(file=..., start_line=..., end_line=...)` | 原生行范围，不再需要 `sed` |
| `read_file` 全文 + 截断 | `file.read_file(file=...)` | 超过 `_MAX_READ_CHARS` 截断并加同一句提示 |
| `write_file(append=...)` | `file.write_file(file=..., content=..., append=...)` | 父目录由 AIO 侧创建；失败按 OSError 报 |
| `download_file` | `file.download_file(path=...)`（流式） | 边读边累计，超过 `max_bytes` 断开并报 OSError |
| `update_file(bytes)` | `file.upload_file(file=bytes, path=...)` | 真二进制，无 base64 |
| `list_dir` | `file.list_path(path, max_depth=..., include_size=True)` | 用 `render_tree` 出同样的树形 glyphs |
| `glob` | `file.glob_files(path=..., pattern=..., max_results=max+1, files_only=...)` | 多取 1 条判断截断 |
| `grep` | `file.grep_files(path=..., pattern=..., include=glob, fixed_strings=literal, case_insensitive=not case_sensitive, max_results=max+1)` | 映射到 `GrepMatch(path, line_number, line)` |

- `list_dir` 的"空目录 vs 失败"必须仍可区分：404 → `FileNotFoundError`；空列表 → `[]`。
- 路径守卫：所有路径先过 `assert_virtual_path(path, virtual_mounts, write=...)`，
  越界 `PermissionError`、只读挂载写 `PermissionError`。
- 二进制上限（`max_bytes`）与读取截断都保留现有常量语义，`Sandbox` 契约不变。

### 6.3 错误映射

| 来源 | 映射 |
|---|---|
| `ApiError(status=404, 路径不存在)` | `FileNotFoundError`（list/read）/ `OSError`（其它文件操作） |
| `ApiError(status=401/403)` | `SandboxRuntimeError("check sandbox.aio ...")` |
| `ApiError(status=5xx)` / `httpx.TransportError` | `OSError`（文件）/ `SandboxRuntimeError`（命令） |
| `httpx.TimeoutException`（请求超时） | `SandboxTimeoutError`（命令）/ `OSError(... timed out)`（文件） |
| 容器已不存在（`docker inspect` 失败） | `SandboxNotFoundError` + 丢 handle + 放租约 |

与 cube 的经验一致：**"沙箱没了"和"文件不存在"必须分开**，否则一个缺文件就把线程容器丢了。

## 7. 配置与启动检查

### 7.1 配置

```yaml
sandbox:
  use: agentkit.sandbox.aio_sandbox:AioSandboxProviderDocker
  aio:
    image: <registry>/all-in-one-sandbox:1.11.0   # 必填；>= 1.9.3 才有 bash.exec
    container_name_prefix: myflow-aio
    port: 8080                      # 容器内 AIO 端口
    publish_host: 127.0.0.1         # 宿主侧绑定地址；只允许 loopback 或显式列出
    ready_timeout_seconds: 60
    request_timeout_seconds: 30     # AIO HTTP 客户端超时（不是命令超时）
    release_action: stop            # stop | remove
    max_cached_threads: 64
    discover_existing: true
    startup_healthcheck: true       # 启动时 docker info + 镜像存在性检查（不创建容器）
    env: {}                         # 容器级 env；不要放 secret
    network:
      mode: bridge                  # bridge | none
      name: null                    # 已有网络名；为空则用默认 bridge
    resources:
      cpus: 2
      memory: 2g
      pids_limit: 512
    host_mount:                     # 与 sandbox.cube.host_mount 同形状
      local_root: /opt/myflow       # Gateway 看到的根（要覆盖 .myflow 与 skills/）
      host_root: /opt/myflow        # docker daemon 看到的同一个根；同机可省略
    lease:                          # 可选；多 worker 必填
      redis_url: $REDIS_URL
      ttl_seconds: 30
    orphan_policy: report           # report | remove
    orphan_ttl_seconds: 86400
```

- `sandbox.use` 与整段 `sandbox.aio` 都是 **startup-only**（`STARTUP_ONLY_FIELDS` 增加一条）。
- `sandbox.bash_command_timeout` 继续作为命令墙钟超时（服务端 `hard_timeout` 用同一值）。
- 配置模型：`AioSandboxConfig`（`extra="forbid"`），`host_mount` 复用共享的 `HostMountConfig`。

### 7.2 `startup_check()`

只做检查，不创建容器：

1. `docker version --format '{{.Server.Version}}'`（daemon 可达；失败 → `SandboxRuntimeError`）。
2. `docker image inspect <image>`；缺失只 warning（首次 acquire 会拉）；可加 `pull: never` 开关变成硬失败。
3. `validate_mount_sources()`（与 cube 同一函数）：根路径形状、技能源是否在根下。
4. `GATEWAY_WORKERS > 1` 且没有 `lease` → 拒绝启动。
5. `orphan_policy: remove` 时执行一次孤儿清理；`report` 只打印。

## 8. 共享代码搬迁（前置小重构）

现在 `cube/` 里有两块与 Cube 无关的东西，AIO provider 必须复用，不该反向 import cube：

1. `agentkit/sandbox/cube/mounts.py` → 抽到 `agentkit/sandbox/host_mounts.py`：
   `HostRoots` / `MountEntry` / `to_host_path` / `mounts_hash` / `validate_mount_sources` /
   `build_host_mounts(app_config, host_mount, *, user_id, thread_id, paths)`。
   `cube/mounts.py` 保留 re-export，现有测试与 import 不动。
2. `agentkit/sandbox/cube/locks.py` → 抽到 `agentkit/sandbox/redis_lock.py`，`cube/locks.py` re-export。
3. 配置：新增共享 `HostMountConfig` 模型；`CubeSandboxHostMountConfig` 保留为别名，
   保证 `config.yaml` 与已发布字段名不变。

搬迁是机械改动，测试不重写；风险集中在"re-export 漏了谁"。

## 9. 测试计划

### 9.1 单元（无 Docker、无网络）

- `docker.py`：用 fake `docker` 可执行（记录 argv、回放 inspect/ps 输出）验证
  - create 的 argv：labels、`--mount` 形式、`-p 127.0.0.1::8080`、加固参数；
  - 端口解析与 `base_url` 拼装；
  - 名字冲突 → 走发现路径；`No such object` → `SandboxNotFoundError`。
- `client.py` / `sandbox.py`：用 fake AIO 客户端（记录调用、可注入状态）验证 §6 的映射：
  - 每个 `Sandbox` 方法对应的 SDK 调用与参数；
  - `timed_out` / `killed` / `running` / `exit_code != 0` 的输出与异常；
  - 行范围读取、append、bytes 往返、glob/grep 截断标志；
  - 错误映射表逐行（404 / 401 / 5xx / transport / timeout / 容器消失）。
- `provider.py`：LRU、release=stop、shutdown=rm、reset=只丢 handle、
  `GATEWAY_WORKERS>1` 无 lease 拒绝启动、mounts_hash 不一致重建。
- canary：宿主写→容器读、容器写→宿主读，失败路径 `rm -f`。

### 9.2 契约一致性

对照 cube 已有的 `test_cube_sandbox.py`，把同一批契约用例跑在 AIO 实现上
（`test_aio_sandbox_contract.py`）：命令退出码、超时、截断、路径越界、只读挂载、
空目录 vs 失败。

### 9.3 集成（opt-in）

`backend/tests/test_aio_sandbox_integration.py`：`AIO_INTEGRATION=1` + 真 Docker：

```bash
AIO_INTEGRATION=1 AIO_IMAGE=<image> REDIS_URL=redis://... \
  uv run pytest backend/tests/test_aio_sandbox_integration.py -v
```

覆盖：create → 就绪 → `echo`/`python3` → `/mnt/user-data` 双向读写 → 二进制往返 →
`stop` 后 `acquire` 热恢复 → `shutdown` 后容器消失。默认 skip（与 cube 集成测试同款）。

## 10. 迁移与回滚

- 切换：`sandbox.use` 改成 `agentkit.sandbox.aio_sandbox:AioSandboxProviderDocker`，
  打开 `sandbox.aio`，重启 Gateway。
- `local → aio`、`cube → aio` 都不需要迁移数据：三者共用同一份宿主
  `.myflow/users/{user}/threads/{thread}/user-data` 与技能树，`thread_files.py` 只认宿主路径。
- 回滚：把 `sandbox.use` 改回去、重启；旧 checkpoint 里的 `aio:<...>` sandbox_id 在
  `get()` 里是内存 miss，runtime 会重新 acquire。遗留容器由 §5.6 的孤儿策略或
  `docker rm -f $(docker ps -aq --filter label=myflow.provider=aio)` 清理。

## 11. 风险与取舍

### 11.1 必须解决

1. **镜像可达性**：`enterprise-public-cn-beijing.cr.volces.com` 不一定在所有环境可达。
   需要内网镜像/代理，或自建同源镜像（Dockerfile 只加依赖，不改 entrypoint）。**待确认**。
2. **镜像版本下限**：`bash.exec` 需要 >= 1.9.3。设计里 pin tag；运行期遇到 404 直接给出
   可操作的报错（"upgrade image to >= 1.9.3"），不重试。
3. **Gateway 与 Docker 是否同机**：默认同机（与 cube 的宿主挂载同款假设）。
   若 Gateway 自己跑在容器里（DooD），`host_mount.host_root` 必须写 **daemon 视角**的路径，
   并挂载 `/var/run/docker.sock`；本设计支持但不默认推荐。
4. **文件属主**：容器内以 root 写宿主挂载，宿主文件会变 root 所有。canary 与
   `thread_files.py` 都会碰到，模板 umask / 挂载目录权限需要与 cube 相同的运维约定。

### 11.2 已知取舍

1. **隔离弱于 MicroVM**：共享内核，容器逃逸面真实存在。适合"可信用户 + 单机部署"。
   需要更强隔离时换 cube；两者共用同一套挂载/租约/契约，切换成本只有配置。
2. **依赖降级路线**：若某环境不允许装 `agent-sandbox` 或不允许容器暴露 HTTP 端口，
   可按 §3 方案 B 实现同一个 provider 的数据面（`docker exec` + `docker cp` + 现有
   search helper），provider/控制面/配置都不变——所以本设计把 `client.py` 定成协议边界。
3. **不做 quarantine**：Docker 容器可丢弃，我们没有 deerflow 那种跨进程恢复需求；
   代价是极端情况下（`rm` 失败）可能留个僵尸容器，由孤儿策略兜底。
4. **端口暴露**：容器 HTTP 只绑 `127.0.0.1`，但它仍是本机进程可访问的 root 权限 shell。
   多租户主机上应把 Gateway 与 Docker 放进同一台专用机器。

## 12. 需要你确认

1. **命名**：provider 路径用 `agentkit.sandbox.aio_sandbox:AioSandboxProviderDocker`
   （我的建议，与 deerflow 对位，名字里带 Docker 以区分未来的远程模式），
   还是保持 `AioSandboxProvider`？
2. **镜像**：用上游 volces 的 `all-in-one-sandbox` 并 pin 到哪个 tag（建议 `1.11.0`）？
   是否需要内网 registry？
3. **Gateway 部署形态**：宿主直跑（默认假设）还是要支持 Docker socket（DooD）模式？
4. **`release_action` 默认值**：`stop`（容器热保留，下一轮秒起）还是 `remove`
   （更干净，每轮冷启动）？
5. **多 worker**：V1 就支持 `lease.redis_url`（复用 Redis 锁），还是先限定单 worker？
6. **AIO 富能力**：要不要在 V1 就把 `browser`/`jupyter` 暴露成工具（会扩大设计范围）？
   本设计建议先只落地现有 8 个文件/命令方法。

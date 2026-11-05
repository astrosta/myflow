# OpenSandbox Provider 设计（取代 AIO Sandbox 方案）

> 状态：待确认（2026-11-05）
> 关系：本文件取代 [AIO Sandbox Provider 设计](2026-11-05-aio-sandbox-docker-provider-design.md)；
> 宿主挂载、Redis per-thread 租约、`Sandbox` 契约与 `thread_files.py` 链路不变。
> 目标 provider：`agentkit.sandbox.opensandbox:OpenSandboxProvider`
> 配置段：`sandbox.opensandbox`（替换 `sandbox.aio`）

## 1. 为什么换

AIO 方案三块太重：

1. **镜像重**：`all-in-one-sandbox` 自带浏览器/Jupyter/桌面那一套，我们只用 bash + 文件，
   镜像体积和冷启动都吃在这上面。
2. **控制面重**：为了建容器我们自己做了一整套 Docker 控制面——argv 加固、端口发布、
   拓扑推导（宿主 vs DooD）、确定性容器名、`myflow.*` labels、`docker ps` 发现、孤儿扫描。
   这些是"通用沙箱平台"该做的事，不该由 Gateway 重复实现。
3. **协议重**：`agent-sandbox` 的 bash/shell 双通道、会话生命周期、`ErrorObservation` 之类的坑，
   我们为了绕开它们写了适配层。

OpenSandbox 把这层做成平台：一个 `opensandbox-server` 控制面（Docker 或 K8s runtime）+
多语言 SDK。我们只保留真正属于业务的部分：**配置、宿主挂载、Redis 租约、Sandbox 契约**。

## 2. 上游事实（在隔离环境装 `opensandbox 1.2.0` 逐条核对签名）

### 2.1 组成部分

- 服务端：`uvx opensandbox-server init-config `/.sandbox.toml --example docker` 生成配置，
  `uvx opensandbox-server` 启动，默认 `127.0.0.1:8080`，健康检查 `GET /health`。
  **Docker runtime 由服务端拥有**：容器由它创建、它管理生命周期。
- SDK：PyPI `opensandbox`，提供 async（`Sandbox`）与 **sync（`SandboxSync`）** 两套 API。
  我们用的是 sync 那套——`Sandbox` 契约本身就是同步的，不需要事件循环桥。

### 2.2 关键 API（sync）

```text
ConnectionConfigSync(api_key, domain, protocol, request_timeout, headers, ...)

SandboxSync.create(
    image, *, timeout=timedelta(minutes=10), ready_timeout=timedelta(seconds=30),
    env={}, metadata={}, resource={"cpu": "1", "memory": "2Gi"},
    volumes=[Volume(host=Host(path=...), mount_path=..., read_only=...)],
    entrypoint=["tail","-f","/dev/null"], network_policy=..., connection_config=...,
)
SandboxSync.connect(sandbox_id, connection_config=..., skip_health_check=...)
SandboxSync.resume(sandbox_id, connection_config=...)

sandbox.id / get_info() / is_healthy() / renew(timedelta) / pause() / destroy() / kill() / close()
sandbox.get_endpoint(port) -> SandboxEndpoint(endpoint, headers, origin)

sandbox.commands.run(cmd, opts=RunCommandOpts(background, working_directory, timeout,
                                              uid, gid, envs))
    -> Execution(exit_code, logs=ExecutionLogs(stdout=[OutputMessage(text)], stderr=[...]),
                 error, complete)
sandbox.commands.create_session(working_directory=...) / run_in_session(...) / delete_session(...)

sandbox.files.read_file(path, encoding='utf-8', offset=, limit=) / read_bytes(...) /
                read_bytes_stream(...) / write_file(path, data, encoding=, mode=) /
                write_files([WriteEntry(path, data, mode, encoding)]) /
                list_directory(DirectoryListEntry(path, depth)) -> [EntryInfo] /
                search(SearchEntry(path, pattern)) -> [EntryInfo] /
                delete_files([...]) / create_directories([...]) / get_file_info([...])

SandboxManagerSync.create(connection_config=...)
    .list_sandbox_infos(SandboxFilter(states=[...], metadata={...}, page_size=, page=))
    .get_sandbox_info(id) / .kill_sandbox(id) / .pause_sandbox(id) / .resume_sandbox(id)
    .patch_sandbox_metadata(id, {"k": "v" | None})

Volume(name?, host=Host(path), pvc, ossfs, mount_path, read_only, sub_path)
EntryInfo(path, entry_type, mode, owner, group, size, modified_at, created_at)
SandboxInfo(id, status=SandboxStatus(state, reason, message), image, metadata, expires_at, ...)
SandboxFilter(states, metadata, page_size, page) -> PagedSandboxInfos(sandbox_infos, pagination)
```

异常族：`SandboxException` / `SandboxApiException` / `SandboxConnectionException` /
`SandboxTimeoutException` / `SandboxReadyTimeoutException` / `SandboxUnhealthyException`。

### 2.3 由此删掉的东西

| AIO 方案里的东西 | OpenSandbox 里 |
|---|---|
| `docker.py`：argv、端口发布、拓扑（``publish_host``/``connect_host``）、``docker ps`` 发现、孤儿扫描 | **全部删除**：服务端拥有容器 |
| 确定性容器名 + `myflow.*` labels + `mounts_hash` label | `metadata={"myflow": "1", "myflow.user": ..., "myflow.thread": ..., "myflow.mounts_hash": ...}`，服务端按 `SandboxFilter(metadata=...)` 过滤 |
| `-p 127.0.0.1::8080` + `docker inspect` 取端口 + `/v1/sandbox` 轮询 | `SandboxSync.create` 的 `ready_timeout` 与内置健康检查 |
| `docker stop` / `docker start` 热恢复 | `metadata` 发现 + `SandboxSync.connect(id)`；过期交给 `timeout` |
| `agent-sandbox` 的 bash/shell 双通道 | 单一 `commands.run(cmd, opts=RunCommandOpts(envs=...))` |

## 3. 生命周期（与本仓库语义对齐）

| 我们的动作 | OpenSandbox 调用 |
|---|---|
| `acquire` 命中缓存 | `get_info()` 看 `status.state`；不是 `RUNNING` 就 `connect` 或重建 |
| `acquire` 未命中 | `SandboxManagerSync.list_sandbox_infos(SandboxFilter(metadata={user,thread}, states=[RUNNING]))` → 有则 `connect`，`mounts_hash` 不一致则 `kill_sandbox` 后重建 |
| 新建 | `SandboxSync.create(image, volumes=..., metadata=..., timeout=..., env=..., resource=...)` + 一次 canary |
| `release`（最后一个持有者离开） | `release_action`：`keep`（默认，什么都不做，TTL 到期由服务端回收）或 `destroy`（立即 `kill_sandbox`） |
| `reset()` | 只丢进程内 handle 与租约，不碰远端 |
| `shutdown()` | 对每个 handle 做 `kill_sandbox` |
| LRU 驱逐 | 只丢 handle（不 destroy），远端由 TTL 收尾 |
| 容器/sandbox 已不存在 | `SandboxException` code 404 / `get_info` 失败 → 丢 handle，让 runtime 重新 acquire |

``release_action` 默认从 `stop` 改成 `keep`：OpenSandbox 没有"停容器"这个动作（`pause` 依赖
runtime，Docker 下不保证留状态），而 TTL 已经能收尾。要更干净就配 `destroy`。

## 4. 数据面（契约 → OpenSandbox）

| 契约 | 实现 |
|---|---|
| `execute_command` | `commands.run(cmd, opts=RunCommandOpts(envs=env, timeout=timedelta(seconds=t)))`；拼接 `logs.stdout` / `logs.stderr`，非零退出追加 `exit code N` |
| `read_file(start_line,end_line)` | `files.read_file(path, offset=, limit=)`；整读后按行切（服务端按 byte 偏移，不按行） |
| `write_file(append)` | `read_file` + `write_file`（`append` 在客户端合并），父目录用 `create_directories` |
| `download_file` / `update_file` | `files.read_bytes` / `files.write_file(path, bytes)` |
| `list_dir` | `files.list_directory(DirectoryListEntry(path, depth=max_depth))` → 自己渲染同一套树形 glyphs |
| `glob` | `files.search(SearchEntry(path, pattern))`，按 `max_results` 截断 |
| `grep` | `commands.run` 跑一条受限的 `grep -r`（`-F` / `-i` / `--include`），NUL 分隔解析 |

`persistent_shell_sessions = False`：每次 `commands.run` 都是一个新进程；`env` 走 `envs` 结构化字段，
不进命令字符串（M9 的请求级 token 不会落到 shell 历史或容器 env）。

## 5. 配置

```yaml
sandbox:
  use: agentkit.sandbox.opensandbox:OpenSandboxProvider
  opensandbox:
    domain: 127.0.0.1:8080        # opensandbox-server 地址
    protocol: http                # http | https
    api_key: ""                   # 留空读 OPEN_SANDBOX_API_KEY
    image: python:3.12            # 只要 bash + grep/find 就够；不强制 AIO 镜像
    timeout_seconds: 1800         # sandbox TTL（服务端过期回收）
    ready_timeout_seconds: 30
    request_timeout_seconds: 30
    acquire_timeout_seconds: 120
    resource: {cpu: "2", memory: "2Gi"}
    env: {}
    release_action: keep          # keep | destroy
    max_cached_threads: 64
    discover_existing: true
    orphan_policy: report         # report | remove
    orphan_ttl_seconds: 86400
    host_mount:
      local_root: /opt/myflow     # Gateway 视角
      host_root: /opt/myflow      # sandbox 服务端宿主视角，卷源按它翻译
    lease:
      redis_url: $REDIS_URL
      ttl_seconds: 30
```

- 依赖：`agentkit[opensandbox] = ["opensandbox>=1.2,<2"]`；Docker 由 `opensandbox-server` 拥有，
  Gateway 不再需要 docker CLI。
- `sandbox.opensandbox` 整段 startup-only。
- `startup_check`：`GET {domain}/health` 可达、挂载源合法、Redis `PING` 通；不再检查 docker/拓扑。

## 6. 风险

1. **新增一个服务**：`opensandbox-server` 必须由部署方跑起来（Docker runtime 场景下它就是
   本地进程）。这换来的是 Gateway 侧代码与依赖大幅变薄。
2. **卷源由服务端解析**：`Volume(host=Host(path))` 的路径要在**服务端服主机的命名空间**里成立，
   所以仍保留 `host_mount.local_root`/`host_root` 双根翻译（与 cube 同一套）。
3. **`pause` 不可依赖**：Docker runtime 下不保证保留状态，所以本设计不用它；
   跨轮复用靠 `connect` + TTL。
4. **镜像要自带 bash**：`grep` 走 shell；`python:3.12` 这类镜像足够，不要求 AIO 镜像。
5. **`files.search` 的递归语义**要真实环境确认；unit 测试用 fake 固定行为，集成测试覆盖。

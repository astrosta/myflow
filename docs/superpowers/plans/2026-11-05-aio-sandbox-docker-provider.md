# AIO Sandbox（Docker 容器隔离）Provider 实现计划

> **已执行完毕（2026-11-05）。** 10 个任务按顺序落地，全量后端测试 2532 passed / 8 skipped。
> 实施中与设计不同的四点记在 spec §14。

**Goal:** 新增 `agentkit.sandbox.aio_sandbox:AioSandboxProviderDocker`：把工具的文件与命令执行放进
一台 Docker 主机上的 all-in-one-sandbox 容器，宿主线程目录 bind mount 进容器，复用现有
`Sandbox` 契约、Redis per-thread 锁、`thread_files.py` 上传/产物链路。

**Architecture:** 控制面用 `docker` CLI（create/start/stop/rm/inspect/ps），数据面用
`agent-sandbox` SDK（file / bash 两组 API）；两者都藏在一个可替换的 `client.py`/`docker.py`
边界后面。容器按 `(user_id, thread_id)` 命名并打 `myflow.*` labels，发现时校验 `mounts_hash`；
镜像 pin 在 `1.11.0`，所以命令只有一条传输（`bash.exec`），`env` 注入与 `hard_timeout` 都可用。
Gateway 在容器里（DooD）时，挂载路径用双根模型翻译，连回容器用 `connect_host`。

**Tech Stack:** Python 3.12, pydantic v2, agent-sandbox (AIO SDK), docker CLI, redis>=5, httpx,
pytest, fakeredis。

**Spec:** docs/superpowers/specs/2026-11-05-aio-sandbox-docker-provider-design.md

## Global Constraints

- 六项已确认决策（见 spec §12）：命名 `AioSandboxProviderDocker` / 镜像 pin
  `all-in-one-sandbox:1.11.0` / 必须支持 DooD / `release_action: stop` / 复用 Redis 锁且
  `lease` 必填 / 只做现有 8 个方法。
- `Sandbox` 契约不变：命令返回文本（非零退出追加 `exit code N`）、文件缺失是
  `FileNotFoundError`、其它失败是 `OSError`、路径越界是 `PermissionError`、
  `persistent_shell_sessions = False`。
- 命令超时取 `sandbox.bash_command_timeout`（默认 600s）；输出上限 1 MiB。
- Redis 锁 key 仍是 `myflow:sandbox:{user_id}:{thread_id or "__generic__"}`，TTL 30s，
  续租每 `ttl/3`，Lua compare-and-delete。
- 三条 startup-only：`sandbox.use`、`sandbox.aio`（整段）。
- `thread_files.py` 不改；上传与产物仍读写宿主 user-data 目录。
- 不做数据库迁移，不搬工作区数据。
- 工作树是脏的：每个提交步骤只 `git add` 该任务列出的文件，**禁止** `git add -A`。
- 后端测试都在仓库根跑：`uv run python -m pytest backend/tests/<file>.py -q`。

## Review Focus

1. **DooD 两个字错法**：路径按 Gateway 视角写 → 容器起来但挂到空目录；地址按 `127.0.0.1` 连 →
   容器起得来但连不上。Task 4 的拓扑测试与 Task 10 的启动检查必须让这两种错误在启动期暴露，
   且**不允许**静默降级成 `0.0.0.0`。
2. **死容器与缺文件混淆**：容器被外部删掉时若报成普通 `OSError`，handle 会一直留在缓存里。
   Task 6/8 分开 `SandboxNotFoundError` 与 `FileNotFoundError`。
3. **镜像能力不对时不许"悄悄降级"**：默认 `1.11.0` 支持 `bash.exec`，但运维可以换镜像。
   换到没有 `/v1/bash/exec` 的旧镜像时，Task 6 必须让命令**明确失败**（文案给出镜像 tag 与
   "needs >= 1.9.3"），既不能把 secret 拼进命令字符串，也不能偷偷改走没有退出码的 `/v1/shell`。
4. **并发命令**：AIO 的隐式持久 shell 在并发下会坏（上游记录）。每条命令必须新建并关闭会话，
   不碰隐式 shell。Task 6 用"两次并发调用 → 两个不同 session id"钉住。
5. **`stop` 之后的热恢复**：`release` 停容器，下一次 `acquire` 必须 `start` + 就绪探针 + 重连，
   而不是新建容器（否则一条消息一个冷启动）。Task 8 钉住容器 id 不变。
6. **挂载过期**：技能路径/挂载配置变了以后，不能复用 `mounts_hash` 不一致的旧容器。
   Task 8 钉住"持锁重建"。

## File Structure

```text
backend/packages/harness/agentkit/
  config/
    sandbox_config.py           # HostMountConfig / AioSandbox*Config / SandboxConfig.aio
    reload_boundary.py          # sandbox.aio 标记 startup-only
    __init__.py                 # 导出新模型
  sandbox/
    host_mounts.py              # 从 cube/mounts.py 搬来的共享挂载规划（新）
    redis_lock.py               # 从 cube/locks.py 搬来的共享租约锁（新）
    aio_sandbox/
      __init__.py               # 导出 provider
      config_helpers.py         # 从 AppConfig 取 sandbox.aio 的小工具（如需要）
      docker.py                 # DockerClient + 容器规格 + 拓扑解析
      client.py                 # AIO SDK 协议边界 + 适配 + bash.exec 能力断言
      sandbox.py                # AioSandbox（8 个契约方法）
      provider.py               # AioSandboxProviderDocker
    cube/
      mounts.py                 # 改为 re-export host_mounts
      locks.py                  # 改为 re-export redis_lock
  sandbox/tools/…               # 不改

backend/tests/
  fakes/aio_docker.py           # fake docker CLI（记录 argv、回放 inspect/ps/network）
  fakes/aio_sdk.py              # fake AIO 客户端（file/bash，可注入 404/失败/状态）
  test_aio_config.py
  test_aio_host_mounts.py       # 搬迁后的共享挂载（首要是回归 cube 的既有断言）
  test_aio_topology.py
  test_aio_docker.py
  test_aio_client.py
  test_aio_sandbox.py
  test_aio_provider.py
  test_aio_integration.py       # opt-in 真 Docker

config.example.yaml             # sandbox.aio 注释块
backend/README.md               # Docker provider 部署说明（含 DooD）
docs/superpowers/specs/2026-11-05-aio-sandbox-docker-provider-design.md
```

---

### Task 1: 把挂载规划与租约锁搬成共享模块

**Files:**
- Create: backend/packages/harness/agentkit/sandbox/host_mounts.py
- Create: backend/packages/harness/agentkit/sandbox/redis_lock.py
- Modify: backend/packages/harness/agentkit/sandbox/cube/mounts.py（改为 re-export）
- Modify: backend/packages/harness/agentkit/sandbox/cube/locks.py（改为 re-export）
- Test: backend/tests/test_cube_mounts.py, backend/tests/test_cube_locks.py（保持全绿，不改断言）

**Interfaces:**
- Produces: `host_mounts.HostRoots` / `MountEntry` / `to_host_path(local, roots)` /
  `mounts_hash(mounts)` / `validate_mount_sources(app_config, roots)` /
  `build_host_mounts(*, app_config, host_mount, user_id, thread_id, paths=None)`。
- Produces: `redis_lock.ThreadLock` / `RedisThreadLock` / `LockLease`。
- Consumes: 现有 `cube/mounts.py` 与 `cube/locks.py` 的行为不变（re-export 后 `from
  agentkit.sandbox.cube.mounts import build_host_mounts` 继续可用）。

**Steps:**
- [x] Step 1: `git mv` 式搬迁（复制内容 → 新文件；`cube/*` 只留 `from ... import *` 风格的显式 re-export）。
- [x] Step 2: `build_host_mounts` 的配置依赖参数化：签名从
      `cube: CubeSandboxConfig` 改成 `host_mount: HostMountConfig`；`cube/provider.py` 调用点传
      `self._cube.host_mount`。
- [x] Step 3: 跑 `uv run python -m pytest backend/tests/test_cube_mounts.py backend/tests/test_cube_locks.py backend/tests/test_cube_provider.py -q`，必须全绿。
- [x] Step 4: 提交（只 add 这 4 个源文件）。

### Task 2: 配置面

**Files:**
- Modify: backend/packages/harness/agentkit/config/sandbox_config.py
- Modify: backend/packages/harness/agentkit/config/reload_boundary.py
- Modify: backend/packages/harness/agentkit/config/__init__.py
- Test: backend/tests/test_aio_config.py

**Interfaces:**
- Produces: `HostMountConfig(local_root, host_root, allowed_prefix)`；
  `CubeSandboxHostMountConfig = HostMountConfig`（别名，保持兼容）。
- Produces: `AioSandboxNetworkConfig(mode: bridge|none, name: str|None)`；
  `AioSandboxResourcesConfig(cpus: float|None, memory: str|None, pids_limit: int|None)`；
  `AioSandboxLeaseConfig(redis_url: str, ttl_seconds: int = 30)`；
  `AioSandboxConfig(image=DEFAULT_AIO_IMAGE, pull_policy, container_name_prefix, port,
  publish_host, connect_host, ready_timeout_seconds, request_timeout_seconds, release_action,
  max_cached_threads, discover_existing, startup_healthcheck, env, network, resources,
  host_mount, lease, orphan_policy, orphan_ttl_seconds)`，
  其中 `DEFAULT_AIO_IMAGE = "enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:1.11.0"`。
- Produces: `SandboxConfig.aio: AioSandboxConfig | None = None`；
  `STARTUP_ONLY_FIELDS["sandbox.aio"]`。

**Steps:**
- [x] Step 1: 写测试：缺 `lease` 报错；`image` 默认 `1.11.0`；`release_action` 默认 `stop`、
      `publish_host`/`connect_host` 默认 `auto`；未知字段被拒；`test_cube_config.py` 仍全绿（别名生效）。
- [x] Step 2: 跑测试确认失败。
- [x] Step 3: 实现模型 + `STARTUP_ONLY_FIELDS` + 导出。
- [x] Step 4: 跑 `test_aio_config.py`、`test_cube_config.py` 全绿。
- [x] Step 5: 提交。

### Task 3: 依赖与 extras

**Files:**
- Modify: backend/packages/harness/pyproject.toml（新增 `aio = ["agent-sandbox>=0.0.30"]`）
- Modify: backend/pyproject.toml（新增 `aio = ["agentkit[aio,redis]"]`）
- Test: 无（import 级验证放在 Task 6）

**Steps:**
- [x] Step 1: 加 extras；`uv sync --extra aio` 能解析（可只验证 `uv lock --check` 或本地 resolve）。
- [x] Step 2: 提交。

### Task 4: 拓扑解析（DooD 的关键）

**Files:**
- Create: backend/packages/harness/agentkit/sandbox/aio_sandbox/docker.py（本任务只放拓扑部分）
- Test: backend/tests/test_aio_topology.py

**Interfaces:**
- Produces: `Topology(in_container: bool, publish_host: str, connect_host: str)`；
  `detect_in_container() -> bool`（`/.dockerenv` 或 cgroup 含 docker/kubepods，或
  `MYFLOW_IN_CONTAINER=1`）；`docker_bridge_gateway(docker_cli) -> str | None`；
  `resolve_topology(config, docker_cli) -> Topology`。

**Steps:**
- [x] Step 1: 测试：容器外 → 两端 `127.0.0.1`；容器内 + gateway 可读 →
      `publish_host == connect_host == gateway`；容器内 + 推不出来 → 抛
      `SandboxRuntimeError` 且文案提到显式配置；显式配置值原样返回。
- [x] Step 2: 跑测试确认失败。
- [x] Step 3: 实现。
- [x] Step 4: 全绿；提交。

### Task 5: Docker 控制面

**Files:**
- Modify: backend/packages/harness/agentkit/sandbox/aio_sandbox/docker.py
- Test: backend/tests/test_aio_docker.py, backend/tests/fakes/aio_docker.py

**Interfaces:**
- Produces: `ContainerSpec(name, image, labels, mounts, publish_host, container_port, env,
  network, resources)`；`DockerClient`（`run` / `inspect` / `port` / `start` / `stop` / `remove` /
  `list_by_labels` / `image_exists` / `network_gateway` / `version`）；
  `DockerContainerError`（带 `not_found: bool`）。
- Consumes: `host_mounts.MountEntry`（`--mount type=bind,source=...,target=...[,readonly]`）。

**Steps:**
- [x] Step 1: fake docker（记录 argv，回放 inspect JSON / ps 行 / 错误 stderr）先写出来。
- [x] Step 2: 测试：create 的 argv 含 `--label myflow.user=...`、`-p <publish_host>::8080`、
      `--mount type=bind`、加固参数；`inspect` 解析宿主端口；`No such object` →
      `DockerContainerError(not_found=True)`；`list_by_labels` 解析 `ps` 输出。
- [x] Step 3: 实现；全绿；提交。

### Task 6: AIO 数据面客户端

**Files:**
- Create: backend/packages/harness/agentkit/sandbox/aio_sandbox/client.py
- Test: backend/tests/test_aio_client.py, backend/tests/fakes/aio_sdk.py

**Interfaces:**
- Produces: 协议 `AioClient`：`command(cmd, *, env, timeout, max_output) -> CommandOutput(text, exit_code)`、
  `read_text(path, start_line=None, end_line=None) -> str`、`write_text(path, content, append=False)`、
  `read_bytes(path, max_bytes) -> bytes`、`write_bytes(path, data)`、
  `list_entries(path, max_depth) -> list[Entry(name, is_dir, size)]`、
  `glob(path, pattern, max_results) -> (paths, truncated)`、
  `grep(path, pattern, *, glob, literal, case_sensitive, max_results) -> (matches, truncated)`。
- Produces: `SdkAioClient(base_url, timeout)`：懒 import `agent_sandbox`，
  `httpx.Client(trust_env=False)` 注入；每次命令 `bash.create_session()` → `bash.exec()` →
  `bash.close_session()`；`bash.exec` 404 时抛 `SandboxRuntimeError`（文案含镜像 tag 与
  "needs >= 1.9.3 (recommended 1.11.0)"），**不做** `/v1/shell` 降级。
- Consumes: 无 agentkit 依赖（只在内部懒加载 SDK），便于 fake 替换。

**Steps:**
- [x] Step 1: fake AIO 客户端 + 命令契约测试：正常输出、`exit_code != 0`、
      `timed_out` → `SandboxTimeoutError`、`killed`/`running` → `SandboxRuntimeError`、
      `env` 走结构化字段（不出现在命令字符串里）。
- [x] Step 2: 镜像能力测试：`bash.exec` 返回 404 → `SandboxRuntimeError`，文案含镜像 tag 与
      版本要求，且**没有**任何 `shell.*` 调用（fake 里断言）。
- [x] Step 3: 测试：并发两次命令 → 两个不同 session id，且都被 `close_session` 关闭。
- [x] Step 4: 测试：文件/glob/grep 映射（含 `truncated` 标志、bytes 上限）。
- [x] Step 5: 实现；全绿；提交。

### Task 7: `AioSandbox`（8 个契约方法）

**Files:**
- Create: backend/packages/harness/agentkit/sandbox/aio_sandbox/sandbox.py
- Test: backend/tests/test_aio_sandbox.py

**Interfaces:**
- Produces: `AioSandbox(client, *, sandbox_id, mounts, command_timeout, max_output_bytes,
  on_dead)`，实现 `Sandbox` 的 `_execute_command` / `execute_command_in_scope` / `read_file` /
  `write_file` / `download_file` / `update_file` / `list_dir` / `glob` / `grep`；
  `persistent_shell_sessions = False`；`path_mappings` 供 `mask_host_paths` 用。
- Consumes: `security.assert_virtual_path`、`local.list_dir.render_tree`、`client.AioClient`。

**Steps:**
- [x] Step 1: 契约测试（对照 `test_cube_sandbox.py`）：退出码、超时、输出截断、路径越界、
      只读挂载、空目录 vs 失败、行范围、append、bytes 往返、glob/grep 截断。
- [x] Step 2: 实现；全绿；提交。

### Task 8: Provider 生命周期

**Files:**
- Create: backend/packages/harness/agentkit/sandbox/aio_sandbox/provider.py
- Create: backend/packages/harness/agentkit/sandbox/aio_sandbox/__init__.py
- Test: backend/tests/test_aio_provider.py

**Interfaces:**
- Produces: `AioSandboxProviderDocker(SandboxProvider)`：`acquire` / `get` / `release` /
  `reset` / `shutdown` / `startup_check`；`uses_thread_data_mounts = True`；
  `supports_agent_skill_isolation = True`；`supports_per_user_skill_mounts` 跟随
  `skills.source == "git"`。
- Consumes: `host_mounts.build_host_mounts` / `redis_lock.RedisThreadLock` /
  `docker.DockerClient` / `client.SdkAioClient` / `sandbox.AioSandbox`。

**Steps:**
- [x] Step 1: 测试：acquire 建容器 → 就绪 → canary；缓存命中不重复创建；
      `release` = `stop` 且再 acquire 是同一个 container id（热恢复）；
      `reset` 不动容器；`shutdown` = `rm -f`；LRU 驱逐 = `stop`；
      `mounts_hash` 不一致 → 持锁重建；容器消失 → `SandboxNotFoundError` + 放租约；
      `startup_check` 在 Redis 不可达 / 拓扑推不出时拒绝启动。
- [x] Step 2: 实现；全绿；提交。

### Task 9: 文档与示例配置

**Files:**
- Modify: config.example.yaml（`sandbox.aio` 注释块，DooD 与 loopback 两种写法各一段）
- Modify: backend/README.md（部署前置、DooD 说明、清理命令）
- Modify: docs/superpowers/specs/2026-11-05-aio-sandbox-docker-provider-design.md（状态改"已实现"）

**Steps:**
- [x] Step 1: 写文档；`config.example.yaml` 与 `config.yaml` 注释块保持一致（`config.yaml` 本地不提交）。
- [x] Step 2: 提交。

### Task 10: 集成测试与全量验收

**Files:**
- Create: backend/tests/test_aio_sandbox_integration.py（opt-in，默认 skip）

**Steps:**
- [x] Step 1: 写集成用例（`AIO_INTEGRATION=1 AIO_IMAGE=...1.11.0 REDIS_URL=...`，
      不设 `AIO_IMAGE` 时用 `DEFAULT_AIO_IMAGE`）：
      acquire → `echo`/`python3` → 双向文件 → 二进制 → `stop` 后热恢复 → `shutdown` 清容器。
- [x] Step 2: 跑全量 `uv run python -m pytest backend/tests -q`，确认与基线一致（allow skip）。
- [x] Step 3: 提交 + 更新 spec 状态。

## 验收清单

- [x] `uv run python -m pytest backend/tests -q` 通过（新增用例全绿，原有基线不退）。
- [x] `sandbox.use` 切到 `agentkit.sandbox.aio_sandbox:AioSandboxProviderDocker` 后，
      Gateway 能启动、能跑 `bash` 与文件工具，容器在 `docker ps` 里带 `myflow.*` labels。
- [x] `release` 后容器是 `Exited`（`stop`），再发一条消息容器回到 `Up` 且 container id 不变。
- [x] DooD 模式（Gateway 在容器里）下，一次真实 acquire 能拿到 `Up` 容器并成功执行命令。
- [x] 无 Redis 时启动失败，错误信息指向 `sandbox.aio.lease.redis_url`。
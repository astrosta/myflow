# CubeSandbox E2B 协议迁移设计（修订原宿主挂载方案）

> 状态：已实施（2026-11-04，选方案 A，官方 e2b SDK）
> 关系：本文件修订 docs/superpowers/specs/2026-11-04-cubesandbox-host-mount-design.md 中“调用 cubesandbox SDK”的部分；宿主目录挂载、Redis 锁、provider 生命周期不变。
> 目标：调用 CubeSandbox 时改用 E2B 协议，不再依赖腾讯 cubesandbox Python SDK。
> 推荐协议客户端：官方 Python SDK e2b>=2.62,<3（当前验证版本 2.62.1）。

## 1. 为什么要改

当前实现的 CubesandboxFactory 使用 cubesandbox 0.7.3：

- Filesystem.read 只有 read(path, *, user=None) -> str，没有 format 参数；
- 因此二进制下载不得不做 dd + base64 分块传输；
- 元数据发现要自己发 /v2/sandboxes HTTP 请求，并维护 403 fallback；
- 命令非零退出码由 SDK 返回 CommandResult，而不是异常。

E2B 协议客户端已经提供这些能力：

- Filesystem.read 支持 format="text" / "bytes" / "stream"；
- Filesystem.get_info / list / write / make_dir；
- Sandbox.list(SandboxQuery(metadata=...)) 原生支持 metadata 过滤；
- Sandbox.create / connect / pause / kill；
- 命令、文件、控制面都走 E2B 兼容的 HTTP/Connect 接口。

因此迁移后 provider 的 90% 逻辑不变，主要改 client.py 和 files.py。

## 2. 目标与非目标

### 目标

1. 用 e2b SDK 替换 cubesandbox SDK，删除 cubesandbox 依赖和与之绑定的 base64 二进制传输。
2. 保留现有 SdkFactory / SdkSandbox 边界，provider、host mount、Redis 锁、canary 不改语义。
3. 使用 E2B 的 metadata 查询发现沙箱，不再自己发 /v2/sandboxes。
4. 正确处理 E2B 的 CommandExitException：非零退出仍是工具输出，不是 SandboxRuntimeError。
5. 配置和启动检查改为 E2B 的 api_url / api_key / domain 语义。
6. 保留 host-mount 作为 Cube 扩展：它仍是 metadata 里的一个 JSON 字符串，E2B SDK 只负责透传。

### 非目标

- 不改 host-mount 数据面。
- 不改 Redis per-thread 锁。
- 不改 thread_files.py / uploads / artifacts。
- 不实现 E2B Cloud 的多租户、快照、卷。
- 不保留 cubesandbox SDK 的双实现；迁移完成后只保留 E2B 一条路径。

## 3. 方案选择

### A. 官方 e2b SDK（推荐）

- 依赖 e2b>=2.62,<3。
- 用 e2b.Sandbox、e2b.SandboxQuery、e2b.SandboxState、e2b.CommandExitException。
- 优点：协议由官方维护，原生 bytes / metadata 查询 / pause / connect；代码量最小。
- 代价：E2B SDK 用 domain 拼出 <port>-<sandbox_id>.<domain>，需要该域名可解析；它没有 cubesandbox 的 CUBE_PROXY_NODE_IP 逐请求 IP 覆盖。
- 适用：CubeProxy 已有 wildcard DNS（*.cube.app 或等价域名）。

### B. 自实现 E2B HTTP/Connect 适配层

- 用 httpx 直接实现控制面 REST 和 envd Connect RPC，保留 proxy_node_ip 的 IP override。
- 优点：保留现有 CUBE_PROXY_NODE_IP 部署，不依赖 SDK。
- 代价：要自己维护 Connect 编码、分页、文件流、错误映射；测试面显著变大。
- 适用：没有 wildcard DNS，必须通过 CubeProxy 节点 IP 访问数据面。

### C. 继续用 cubesandbox SDK 包一层 E2B facade（不采用）

没有真正切换协议，只是换名字；不解决 format、metadata 查询和 SDK 锁定问题。

推荐 A；如果当前 CubeProxy 没有可解析的 wildcard DNS，则必须先选 B，或先补 DNS。

## 4. E2B API 映射

| 当前 CubesandboxFactory | E2B 迁移后 |
|---|---|
| cubesandbox.Sandbox.create(...) | e2b.Sandbox.create(..., api_url=..., api_key=..., domain=..., request_timeout=...) |
| cubesandbox.Sandbox.connect(id) | e2b.Sandbox.connect(id, api_url=..., api_key=..., domain=...) |
| Sandbox.list_v2() + 手写 /v2/sandboxes | e2b.Sandbox.list(SandboxQuery(metadata=...), limit=..., next_token=...) |
| sandbox.commands.run(...) 返回 CommandResult | e2b commands.run(...)；非零退出抛 CommandExitException，适配层转回 CommandResult |
| files.read(path, user=...) 文本 | files.read(path, format="text", user=...) |
| dd + base64 分块下载 | files.read(path, format="bytes", user=...) |
| files.write(path, bytes) | files.write(path, bytes, user=...) |
| files.list() | files.list(path, depth=1, user=...) |
| files.stat() | files.get_info(path, user=...) |
| 手写健康检查 | Sandbox.list(limit=1) 作为控制面探活 |
| sandbox.pause() / kill() | 同名方法；connect 自动恢复 paused |

### 关键差异

1. **CommandExitException**
   - E2B commands.run 在 exit_code != 0 时抛异常，异常对象带 stdout/stderr/exit_code。
   - 必须把它转换成当前工具的 exit code N 输出；不能当 SandboxRuntimeError。
   - 这是迁移中最容易回归的一条。

2. **二进制**
   - 使用 format="bytes" 后，删除 files.py 的 dd/base64 分块实现；
   - 模板不再需要 dd/base64，但仍需要 python3 给 search helper、grep/find/sed 给文本搜索。

3. **列表**
   - E2B 的 EntryInfo.type 是 FileType.FILE / DIR / SYMLINK；
   - 当前假 SDK 的 FILE_TYPE_DIRECTORY 字符串需要更新。

4. **health**
   - E2B SDK 没有 Sandbox.health()；startup_check 用 Sandbox.list(limit=1) 探活。

5. **DNS**
   - E2B 数据面 URL 由 domain 和 sandbox id 生成；官方 SDK 没有 per-request IP override。
   - 如果部署依赖 CUBE_PROXY_NODE_IP，必须改用方案 B，或配置 wildcard DNS。

6. **错误分类（实施时发现的关键差异）**
   - E2B 2.62.1 把“沙箱没了”放进异常类型，而不是 HTTP 状态码：
     - 控制面 404 → `SandboxNotFoundException`；
     - envd 前面的 proxy 502 “was not found” → `SandboxNotRunningException`；
     - 连不上 → `SandboxUnreachableException`；
     - **文件不存在** → `FileNotFoundException`（同一个 404 语义，但沙箱还活着）。
   - 旧 cubesandbox 实现用“404/410 = 沙箱死了”；直接照搬会让**缺文件被误判成死沙箱**，
     而真正死掉的沙箱（无 status_code）反而漏判，handle 会一直留在缓存里。
   - 因此 client.py 增加三个判定函数，files.py / sandbox.py / provider.py 统一走它们：
     `is_gone_error` / `is_missing_path_error` / `is_unreachable_error`。
   - `SandboxNotRunningException`、`SandboxUnreachableException` 是 e2b `TimeoutException`
     的子类，`is_timeout_error` 必须先把它们排除，否则会报成“命令超时”。
   - 文件产出的 gone 错误用 `SandboxGoneError(OSError)`：保持 `Sandbox` 文件契约仍是 OSError，
     同时让 `CubeSandbox` 能 `_notify_dead()` 丢弃 handle。

7. **命令非零退出与 SIGPIPE**
   - `_wrap_command` 的 `head -c` 会把 `PIPESTATUS` 里的 141 归一成 0，再由适配层把
     `CommandExitException` 转回结果对象，工具层照旧渲染 `exit code N`。

## 5. 配置变更

~~~yaml
sandbox:
  cube:
    api_url: $E2B_API_URL
    api_key: ""                 # 留空时读 E2B_API_KEY
    template_id: $E2B_TEMPLATE_ID
    sandbox_domain: cube.app    # 映射到 E2B domain
    # proxy_node_ip / proxy_port 删除；E2B SDK 不支持
~~~

保留：

- timeout_seconds / on_timeout / auto_resume / release_action
- host_mount 整段
- lease.redis_url / ttl_seconds
- allow_internet_access / allow_public_traffic
- sandbox_user
- max_cached_threads

网络段改为 E2B 形状：

~~~yaml
network:
  allow_out: []
  deny_out: ["169.254.169.254/32"]
  rules: null                # E2B 的 host -> transform 映射；不再接受 Cube rule list
~~~

配置模型删除 CubeSandboxConfig.proxy_node_ip / proxy_port；CubeSandboxNetworkConfig.rules 改为 dict | None。api_key 为空时读 E2B_API_KEY，兼容 CUBE_API_KEY 作为 fallback。template_id 可用 E2B_TEMPLATE_ID。

## 6. 代码改动

### client.py

- CubesandboxFactory -> E2BFactory。
- 懒加载 e2b。
- 构造 e2b.ConnectionConfig 风格的参数：
  - api_url
  - api_key
  - domain = sandbox_domain
  - request_timeout
- create / connect / list_by_metadata / health 全部走 e2b。
- list_by_metadata 使用 SandboxQuery(metadata=...)，分页用 has_next / next_items；不再手写 /v2/sandboxes。
- 用 E2B 的 CommandExitException 适配 SdkCommands 协议：返回带 stdout/stderr/exit_code 的结果。
- 保留 SdkFactory / SdkSandbox Protocol，provider 不需要知道 SDK 名字。

### files.py

- 删除 base64 / binascii / dd 分块代码。
- read_bytes -> sdk.files.read(path, format="bytes")，转 bytes。
- _stat_size -> sdk.files.get_info(path).size。
- list_tree -> EntryInfo.name / EntryInfo.type == FileType.DIR。
- 保留 search helper、路径 guard、错误映射。

### sandbox.py

- 命令适配层若未在 client 做，则在 _execute_command 捕获 CommandExitException。
- E2B 的 timeout / SandboxNotFoundException / FileNotFoundException 映射到现有 Sandbox 错误族。
- 其他生命周期不变。

### 配置、依赖、文档

- harness extra cube: e2b>=2.62,<3；删除 cubesandbox。
- backend extra cube: agentkit[cube,redis] 保持不变。
- config.example.yaml / README / 测试环境变量改为 E2B_API_URL / E2B_API_KEY / E2B_TEMPLATE_ID。
- 删除 CUBE_PROXY_NODE_IP / proxy_port 文档；增加 wildcard DNS 前置条件。

## 7. 测试

- client adapter：create/connect/list metadata/pause/kill/health 的 E2B 参数透传。
- CommandExitException -> exit code N 输出，非零不抛 SandboxRuntimeError。
- bytes read/write：含二进制 0x00，不经过 base64。
- EntryInfo FileType.DIR 的 list_tree。
- Sandbox.list 分页与 metadata 精确过滤。
- 现有 137 个 Cube 测试保留；fake SDK 从 cubesandbox shape 改为 E2B shape。
- 真实 E2B/Cube 集成测试：create、host mount canary、echo、bytes、pause/resume、list discovery。
- 回归：thread_files uploads/artifacts、Redis lock、canary。

## 8. 迁移顺序（实际执行）

原计划保留一版 cubesandbox 双实现，实际直接单路径切换：Cube 实现只有一处调用 SDK，
双实现只会增加分支和测试面。

1. client.py：`CubesandboxFactory` → `E2BFactory` + `_SandboxAdapter`/`_CommandsAdapter`/`_FilesAdapter`，
   加 `is_gone_error` / `is_missing_path_error` / `is_unreachable_error` / `SandboxGoneError`。
2. files.py：二进制走 `format="bytes"`，删除 dd/base64 分块；错误映射改走判定函数。
3. sandbox.py：命令错误映射改走 `is_gone_error`，文件操作统一经 `_file_errors` 守卫。
4. provider.py：`_apply_action` / `_kill_remote` / `_kill_quietly` 用 `is_gone_error` 替代 404/410。
5. 配置、依赖、文档、测试环境变量改为 E2B 语义；fake SDK 保持协议形状不变
   （`FakeSdkFactory` 直接实现 `SdkFactory`，不模拟 e2b 对象）。
6. 真实集成测试留在 `test_cube_integration.py`，默认 skip，只在 Linux Cube 宿主显式开启。

### 已验证的 E2B 2.62.1 契约（实现依据）

在隔离环境装 `e2b==2.62.1` 逐条核对签名后落地：

- `Sandbox.create(template=, timeout=, metadata=, lifecycle=, network=, allow_internet_access=, **ApiParams)`；
  `ApiParams` 含 `api_url` / `api_key` / `domain` / `request_timeout`。
- `Sandbox.connect` 是 `@class_method_variant`，`Sandbox.connect(sandbox_id, **opts)` 可用。
- `Sandbox.list(query, limit=, next_token=, order=, **opts)` 返回 `SandboxPaginator`（`has_next` / `next_items()`）。
- `SandboxState` 只有 `RUNNING` / `PAUSED`；`SandboxQuery(metadata=, state=)`。
- `commands.run(cmd, background, envs, user, cwd, on_stdout, on_stderr, stdin, timeout, request_timeout)`。
- `files.read(path, format, user)` / `files.write(path, data, user)` / `files.list(path, depth, user)` / `files.get_info(path, user)`。
- `EntryInfo` 继承 `WriteInfo`（`name` / `type` / `path`）+ `size` / `mode` / …；`FileType.FILE`、`FileType.DIR`、`FileType.SYMLINK`。
- 同步 `Sandbox` 没有 `close()`，适配层 `close()` 是空操作。

## 9. 风险与待确认

1. **wildcard DNS**：当前部署是否已有 *.cube.app 解析？如果没有，E2B SDK 不能像 cubesandbox 一样用 CUBE_PROXY_NODE_IP 逐请求覆盖；需要方案 B 或补 DNS。
2. **E2B SDK 版本**：e2b 2.62.1 的 SDK 契约已逐条核对（见 §8）；与 CubeSandbox 集群的
   metadata/list/生命周期互操作仍需要真实集群集成测试（默认 skip，未在本次开发机跑）。
3. **CommandExitException**：必须保留“非零退出是输出”的现有工具契约。
4. **network.rules 形状**：E2B 与 Cube 的 L7 rules 不完全相同；迁移 V1 只支持 E2B 形状。
5. **配置破坏性变更**：proxy_node_ip/proxy_port 删除。已同步改 config.example.yaml、
   backend/README.md 和集成测试环境变量，README 增加 wildcard DNS 前置条件。

## 10. 决策

已选 **A：官方 e2b Python SDK**（`e2b>=2.62,<3`），前提是 CubeProxy 数据面
`<port>-<sandbox_id>.<sandbox_domain>` 有 wildcard DNS。若某套部署确实无法解析该域名，
只能改方案 B 自实现 E2B HTTP/Connect 适配层；`cubesandbox` 的 `CUBE_PROXY_NODE_IP` 已从配置、
代码、文档中删除，不做回退开关。

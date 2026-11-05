# Cube provider 彻底改为纯 E2B：去掉宿主挂载

> 状态：待确认（2026-11-05，三个决策在 §9）
> 关系：本文件推翻 [CubeSandbox 宿主挂载设计](2026-11-04-cubesandbox-host-mount-design.md) 的核心假设
> （宿主目录 bind mount 进沙箱），并承接 [E2B 协议迁移设计](2026-11-04-cubesandbox-e2b-protocol-design.md)。
> 影响范围：`agentkit/sandbox/cube/`、`sandbox.cube` 配置、Cube 的测试、README/config 示例。

## 1. 为什么

上一轮把 SDK 换成了官方 `e2b`，但**宿主挂载还是 Cube 的私有扩展**：它靠
`metadata["host-mount"]` 透传一个 JSON 给 Cubelet，E2B 协议本身没有这个概念。
结果是一个"用 E2B SDK 但只有 CubeSandbox 能用"的 provider：

- 换到 E2B Cloud、自建 envd、或任何遵守协议的后端，`host-mount` 会被忽略 →
  `/mnt/user-data` 是空目录 → 文件工具静默读到空；
- 配置里 `host_mount.local_root/host_root` 的双根翻译、`ALLOWED_HOST_MOUNT_PREFIXES`、
  umask/属主那套运维约定，全都是 Cube 特有的；
- `mounts_hash` 塞在 metadata 里做挂载指纹，协议语义上毫无意义。

"彻底改 E2B" = 只用协议里有的东西：**文件走文件 API，不靠宿主目录共享**。

## 2. 目标

1. 包/类/配置重命名成 E2B 语义，不再叫 cube（§3）。
2. 删除 `host_mount` 全链路：配置、`build_host_mounts` 调用、`host-mount` metadata、
   `mounts_hash`、宿主↔沙箱 canary。
3. 用**显式同步**替代挂载：acquire 时上传、release 时回写（§4）。
4. 上传/产物链路（`thread_files.py`）对外行为不变：前端仍然按虚拟路径读写。

## 3. 重命名（破坏性、不留别名）

| 现在 | 改为 |
|---|---|
| `agentkit/sandbox/cube/` | `agentkit/sandbox/e2b/` |
| `CubeSandboxProvider` | `E2BSandboxProvider` |
| `CubeSandbox` | `E2BSandbox` |
| `CubeSandboxConfig` / `CubeSandboxLeaseConfig` / `CubeSandboxNetworkConfig` | `E2BSandboxConfig` / `SandboxLeaseConfig`（已共享）/ `E2BSandboxNetworkConfig` |
| `sandbox.cube` | `sandbox.e2b` |
| `agentkit.sandbox.cube:CubeSandboxProvider` | `agentkit.sandbox.e2b:E2BSandboxProvider` |
| `cube:` sandbox_id 前缀 | `e2b:` |

`host_mount` 整段删除；`host_mounts.py` 继续服务 opensandbox（它确实是本机 Docker，卷挂载是协议内能力）。

## 4. 同步方案（选 A：显式同步）

| 方案 | 说明 | 评价 |
|---|---|---|
| **A 显式同步（选）** | acquire 上传、release 回写，走 `files.write_bytes/read_bytes` | 只依赖协议；代价是延迟与体积 |
| B 文件 API 代理 | 前端上传/产物请求直接打沙箱 API | 实时，但网关要变成文件代理，还要处理沙箱生命周期/鉴权 |
| C 保留宿主挂载 | 现状 | 被否：只有 Cube 能用，等于没用 E2B 协议 |

### 4.1 acquire

1. 沙箱内建目录：`mkdir -p /mnt/user-data/{workspace,uploads,outputs}`。
2. 上传宿主 `<thread>/user-data/uploads/` 整棵树 → `/mnt/user-data/uploads/`。
3. （按 §9 决策）上传技能树 → `/mnt/skills/`（只读语义由路径守卫保证）。
4. `workspace` 默认**不上传**：它是本轮工作区，宿主侧只是上一次的残留。
5. 上传失败 → 整次 acquire 失败（不带着半份文件跑），并 kill 新建的沙箱。

### 4.2 release

1. 下载 `/mnt/user-data/outputs/` → 宿主 `<thread>/user-data/outputs/`（覆盖同名、保留仅宿主存在的文件）。
2. （可选）下载 `workspace`：默认关，开了才有"下一轮接着干上轮工作区"。
3. 下载失败只 warning：产物丢一份比 kill 一个还在用的沙箱更糟。

### 4.3 上限与粒度

- 单文件 `sync.max_file_bytes`（默认 32 MiB）：超过的文件**跳过并给一条 warning**，
  不截断（截断的文件比缺失更坏）。
- 单树 `sync.max_tree_bytes`（默认 256 MiB）：超过直接放弃该树并 warning。
- v1 全量同步；后续可加 mtime+size 清单做增量（协议侧只有 `get_info` 的 size）。

## 5. 路径守卫与 canary

- 路径守卫仍用虚拟挂载表：`/mnt/user-data` rw、`/mnt/skills` ro（若上传技能）。
  它不再需要 `host_path`，`PathMapping.local_path` 只用于 `mask_host_paths` 的脱敏。
- canary 改成**三方一致**校验：`files.write_bytes` 写 → `files.read_bytes` 读回比对 →
  `execute_command "cat <path>"` 输出比对。证明"文件 API 与容器内视图是同一份"，
  这正是没有宿主挂载后唯一还值得验证的东西。

## 6. 配置

```yaml
sandbox:
  use: agentkit.sandbox.e2b:E2BSandboxProvider
  e2b:
    api_url: $E2B_API_URL
    api_key: ""
    template_id: $E2B_TEMPLATE_ID
    sandbox_domain: cube.app
    timeout_seconds: 1800
    on_timeout: pause
    auto_resume: true
    release_action: pause
    acquire_timeout_seconds: 120
    request_timeout_seconds: 30
    startup_healthcheck: true
    max_cached_threads: 64
    network: { allow_out: [], deny_out: [] }
    lease: { redis_url: $REDIS_URL, ttl_seconds: 30 }
    sync:                        # 新增：替代 host_mount
      enabled: true
      upload_uploads: true
      upload_workspace: false
      upload_skills: true
      download_outputs: true
      download_workspace: false
      max_file_bytes: 33554432
      max_tree_bytes: 268435456
```

## 7. 迁移与破坏性变更

- 配置：`sandbox.cube` → `sandbox.e2b`，`host_mount` 删除。旧配置会被 pydantic 拒（extra=forbid），
  启动即报错，不会静默跑在错误模式上。
- sandbox_id：`cube:<id>` → `e2b:<id>`；旧 checkpoint 里的 id 在 `get()` 里是内存 miss，
  runtime 重新 acquire（与之前换 provider 的路径一致）。
- metadata：去掉 `host-mount` 与 `mounts_hash`；发现只看 `myflow.user/thread`。
- 模板：不再需要预建 `/mnt/user-data` 的 bind 源，但仍需 `mkdir`、`cat`、`python3`、
  `grep/find/sed`（文本工具与 search helper 用）。

## 8. 测试

- 删除挂载相关：`test_cube_mounts.py`（cube 部分）、provider 的 `mounts_hash` / canary 用例。
- 新增 `test_e2b_sync.py`：上传/下载树、上限跳过与 warning、失败路径（上传失败 → acquire 失败；
  下载失败 → 只 warning）。
- 改 `test_e2b_provider.py`：metadata 不含 `host-mount`；canary 三方一致；
  acquire→release 后宿主 `outputs` 出现沙箱内写的文件（fake 文件 API）。
- 集成（opt-in）：真集群跑 acquire → 写 `/mnt/user-data/outputs/a.txt` → release → 宿主文件存在。

## 9. 决策（2026-11-05 已确认）

| # | 问题 | 决策 |
|---|---|---|
| 1 | 技能文件怎么进沙箱 | **按需上传（lazy）**：不预传整棵树，`read_file` / `list_dir` / `glob` / `grep` 命中只读技能根时，先把"这次要读的东西"从宿主技能源上传进沙箱，再执行；已上传的路径按沙箱缓存，沙箱丢了一起丢 |
| 2 | 产物回写时机 | **release 时下载** |
| 3 | 重命名范围 | **彻底改**：`sandbox.e2b` + `E2BSandboxProvider` + `e2b:` 前缀，不留别名 |

### 9.1 按需上传技能（决策 1 的落地）

- 技能源在宿主上仍是"只读目录"：`filesystem` 来源是 `skills.path`，`git` 来源是该用户的
  `active/users/<user>` 树（与现在的挂载源同一处），只是**不再 bind mount**。
- `E2BSandbox` 持有一个只读源：`(host_root, container_root, enabled)`。命中
  `container_root` 下的路径时：
  - `read_file`：目标文件不在沙箱（`files.stat` 404）→ 上传这一个文件（必要时先 `mkdir -p` 父目录）→ 再读；
  - `list_dir` / `glob` / `grep`：先把**这次查询的子树**上传（受 `max_tree_bytes` 限制），
    并把该子树记进"已上传"集合，后续查询不再重复传。
- 上限复用 §4.3；超限时工具返回明确错误（"技能目录过大，无法按需上传"），不返回空结果。
- 这样常见路径（模型只读一两个 SKILL.md）成本是几十 KB，而不是整棵树的几 MB。

### 9.2 与现状的差异（实施时要注意）

- `mounts_hash` 这个"挂载指纹"失去意义 → 删除；发现只看 `myflow.user/thread`，
  `myflow.version` 保留用于将来做配置指纹。
- 宿主↔沙箱 canary 改成**三方一致**（写 API → 读 API → `cat` 看容器内视图），
  不再验证 bind mount。
- `PathMapping.local_path` 只剩脱敏用途（`mask_host_paths`），不再参与路径解析。
- `thread_files.py` 不改：上传仍写宿主 `uploads/`，artifact 仍读宿主 `outputs/`；
  差别是 `outputs` 的内容由 release 时的下载写进来。

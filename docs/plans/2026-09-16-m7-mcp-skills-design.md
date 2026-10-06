# M7 能力扩展：MCP 与技能 详细设计

> 配套文档：[分阶段开发方案](2026-09-16-greenfield-agent-service-roadmap.md) ·
> [M0 地基](2026-09-16-m0-foundation-design.md) · [M1 最小 Agent](2026-09-16-m1-minimal-agent-design.md) ·
> [M2 工具与沙箱](2026-09-16-m2-tools-sandbox-design.md) · [M3 Gateway 与流式](2026-09-16-m3-gateway-streaming-design.md) ·
> [M4 持久化](2026-09-16-m4-persistence-design.md) · [M5 前端](2026-09-16-m5-frontend-design.md) ·
> [M6 中间件体系](2026-09-16-m6-middleware-design.md) ·
> [路由层详细设计](2026-09-16-router-layer-design.md)
>
> 本文细化 M7（2 周，可与 M8 并行）。沿用包名：框架层 `agentkit.*`，应用层 `app.*`。

---

## 1. M7 的范围

**目标一句话：不改代码就能给 agent 加能力。**

两条互补的路径，解决的是不同的问题：

| | MCP | 技能 |
|---|---|---|
| 加的是什么 | **新工具**（外部进程/服务提供） | **新知识**（prompt 片段 + 附带资源） |
| 谁来写 | 第三方服务端 | 你自己，或社区 |
| 载体 | 一个进程或 HTTP 端点 | 一个目录 + `SKILL.md` |
| 成本 | 要装、要配凭据、要管连接 | 写个 markdown 就行 |
| 进图的位置 | M2 工具列表第三源 | 系统提示词块 + 两条中间件（M6 已留槽） |

**技能是这两者里投入产出比最高的。** 一个目录加一个 markdown 文件，
就能让 agent 掌握一套领域流程。MCP 更强但更重——它带来的是连接管理、
凭据注入、进程生命周期这一整套问题。

M7 **不做** MCP 长时任务、技能沙箱投影、SkillScan、托管集成。那些各自够一个
里程碑。M7 把"能配、能开、能用、配置不丢"做硬。

### 两周排期

| 天 | 内容 |
|---|---|
| D1–2 | **运行时可写配置的并发写**（两者共同的地基）+ §2.4 的六条路由 |
| D3–5 | 技能：加载、解析、slash 合同、激活中间件、`allowed-tools` 策略 |
| D6–8 | MCP：客户端、三种 transport、缓存失效、前缀、会话池 |
| D9 | MCP 凭据四层 + fail-closed + 非法值不回显 |
| D10 | 工具检索（`tool_search` + 延迟提升），如果工具数量已经上百；否则留接口 |

### M7 新增的文件

```
agentkit/config/
├── extensions_config.py       ★  # ExtensionsConfig / atomic_write / 双锁
├── skills_config.py              # path / container_path / deferred_discovery
└── tool_search_config.py         # enabled；D10 才真正用

agentkit/skills/
├── types.py                   ★  # Skill / SkillCategory / SecretRequirement
├── parser.py                  ★  # frontmatter、allowed-tools 别名、UTF-8
├── slash.py                   ★  # 与前端共用 contracts/slash_skill_contract.json
├── tool_policy.py             ★  # union / ALWAYS_AVAILABLE
├── storage.py                    # load_skills() 扫描
├── catalog.py / describe.py      # deferred_discovery=true 时
└── prompt.py                     # <available_skills> / <skill_index> 渲染

agentkit/mcp/
├── cache.py                   ★  # 内容签名失效 + 退役会话池
├── client.py                     # build_servers_config
├── tools.py                   ★  # 前缀、打 deerflow_mcp 标、包装会话
├── session_pool.py            ★  # (server, user:thread) + owner task
├── headers.py                 ★  # apply_header_overrides / illegal_header_value_reason
├── interceptors.py               # 组装顺序：oauth → user_auth → context
├── oauth.py
├── user_scoped_auth.py
└── context_headers.py

agentkit/agents/middlewares/
├── skill_activation_middleware.py     # 填 M6 SLOT
├── skill_tool_policy_middleware.py    # 必须紧挨 activation 之后
├── deferred_tool_filter_middleware.py # D10
└── mcp_routing_middleware.py          # D10

agentkit/tools/builtins/tool_search.py
agentkit/tools/mcp_metadata.py         # tag_mcp_tool / is_mcp_tool
agentkit/runtime/secret_context.py     # slash 路径 + 决策键；进 REDACTED_CONTEXT_KEYS

app/gateway/mcp_wire.py            # 打码 / 打码回写；路由不碰磁盘
app/gateway/skills_catalog.py       # 扫描结果 → SkillResponse
app/gateway/routers/mcp.py          # prefix=/mcp
app/gateway/routers/skills.py       # prefix=/skills

contracts/slash_skill_contract.json    # 前后端 slash 语法钉死
```

**禁止**技能路由和 MCP 路由各自写一份 JSON。所有读-改-写走
`atomic_write_extensions_config` + 同一把 `extensions_config_write_lock`。

---

## 2. 共同地基：运行时可写配置（D1–2）

**这一节看起来最无聊，但它是 M7 唯一真正会导致数据丢失的部分。**

MCP 服务器列表和技能的启用状态都存在 `extensions_config.json` 里，
这个文件**同时被 Gateway API 和运行时读写**。用户在网页上点一下"启用某技能"，
就是一次读-改-写。它和 `config.yaml` 分开（M0 已定）：YAML 是运维的，
JSON 是 API 可写的。JSON **不允许**列 Python 扩展路径去 `import`（那是 YAML
`plugins:` 的事，落实见
[M10 §4.1](2026-09-16-m10-memory-guardrails-extensions-design.md)）。
`mcpInterceptors` 是例外——它会 import 代码，所以
Gateway **不得**提供写入 `mcpInterceptors` 的 API；只允许运维改文件。

### 2.1 文件形状

```json
{
  "mcpServers": {
    "github": {
      "enabled": false,
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_TOKEN": "$GITHUB_TOKEN" },
      "tool_name_prefix": true,
      "session_init_timeout": 60,
      "tool_call_timeout": 60
    }
  },
  "skills": {
    "pdf-processing": { "enabled": true }
  }
}
```

路径解析（与 M0 同一套）：显式参数 → `DEER_FLOW_EXTENSIONS_CONFIG_PATH` →
搜 `extensions_config.json`。**显式路径或环境变量指向的文件不存在 →
`FileNotFoundError`，不要静默当"没配扩展"**。只有搜索模式找不到才返回
`None`（合法的"没配"）。

`$ENV` 解析复用 M0：只展开值，不展开键。MCP `headers` / `env` /
`user_auth.users` 的值都走它。

`type` 接受 MCP 规范的 `transport` 别名；两个都写时 `type` 赢。漏掉这条，
只写了 `transport: http` 的服务器会被当成默认 `stdio` 去 spawn。

Pydantic `alias="mcpServers"`，`to_file_dict()` 用 `by_alias=True` 写回。
读的时候 `populate_by_name=True`。不要把内存字段名 `mcp_servers` 写进文件。

### 2.2 三个必须同时满足的条件

**① 每次读-改-写必须同时持有进程内锁和跨进程文件咨询锁。**

```python
extensions_config_write_lock = threading.Lock()   # 不是 asyncio.Lock

@contextmanager
def extensions_config_file_lock(path: Path):
    lock_path = path.parent / f".{path.name}.lock"
    ...  # 咨询锁（Windows 用 msvcrt，POSIX 用 fcntl）
```

只有进程内锁的话，多 worker 部署下两个进程各自读到旧版本、各自改一个字段、
各自写回——后写的把先写的改动覆盖掉。

**锁必须在执行 RMW 的那个 worker 线程里获取，不能在 `await to_thread` 外面。**
`asyncio.Lock` 包着 `await to_thread(rmw)` 时，task 一取消锁就放，后台线程
还在写，第二个 writer 闯进去。`threading.Lock` 从 worker 里拿，写完才放，
也没有 event-loop 亲和性。

锁的范围必须覆盖**完整的读-改-写-重载循环**（`from_file` → merge →
`atomic_write` → `reload_extensions_config()`），不是只覆盖写的那一下。

技能路由和 MCP 路由**共用这一把锁**。分开两把，两个标签页分别改技能和 MCP，
仍然丢更新——它们改的是同一个文件。

**② 原子写：同目录临时文件 + fsync + `os.replace()`。**

```python
def atomic_write_extensions_config(path: Path, data: dict) -> None:
    target = path.resolve(strict=False) if path.is_symlink() else path
    # 保留原权限位
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                            prefix=f".{target.name}.", suffix=".tmp", delete=False) as tmp:
        json.dump(data, tmp, indent=2, ensure_ascii=False)
        tmp.flush(); os.fsync(tmp.fileno())
    try:
        os.replace(tmp.name, target)          # 跟随符号链接目标
    except OSError as exc:
        if exc.errno != errno.EBUSY:
            raise                              # 其它 errno 绝不能退化
        _overwrite_in_place(target, tmp.name)  # 非原子，仅 EBUSY
    _fsync_directory_best_effort(target.parent)
```

直接覆写原文件意味着写到一半崩溃会留下一个截断的 JSON——
下次启动时整个配置解析失败，所有 MCP 服务器和技能状态全丢。

`ensure_ascii=False`：技能名、description 经常是中文，写成 `\uXXXX` 人肉排查看不见。

**③ Docker 挂载单文件时，`rename()` 会返回 `EBUSY`。**

Docker 把单个文件（不是目录）挂载进容器时，那个文件是一个独立的 mount point。
所以保留"临时文件 + rename"作为主路径，**只在收到 `EBUSY` 时**退化为就地覆写。
那条退化路径**不是原子的**；**其它 errno 必须抛出**，不能笼统 catch `OSError`。

第一次走退化路径打 WARNING（说明崩溃会截断），同一进程后续同路径降到 DEBUG，
避免日志被刷爆。

### 2.3 缓存失效用内容签名，不用 mtime 比较

```python
ConfigSignature = tuple[float | None, int | None, str | None]   # (mtime, size, sha256)
```

复用 M0 的 `get_config_signature`。比较"解析后的路径 + 签名"是否和初始化时一致。

严格的 `mtime >` 比较会漏：同一秒内两次编辑、`git checkout` 让 mtime 倒退、
`cp -p` / 备份恢复、`tar` / `rsync`、网络挂载、切换到另一个 mtime 更老的文件。

**加载过程中签名变了（前后两次快照不一致）→ 丢弃结果时必须走和正常失效一样的
会话退役路径。** 否则被放弃的加载会把连接留在池子里，下一次构建工具时用到旧会话。

MCP 缓存自己的路径解析要 **catch `FileNotFoundError` 当"未配置"**——热路径上
显式路径中途消失不该把每次 `get_cached_mcp_tools()` 打 500。真正 `from_file()`
的调用者仍然要大声失败。

缓存构建用 generation + `threading.Condition` 做 single-flight：并发请求共用
一次加载。失效时 `_cache_generation += 1`，等着的 waiter 看到 generation 变了
就重来。

### 2.4 路由

错误体、`extra="forbid"`、能力门沿用[路由层](2026-09-16-router-layer-design.md)。
两个 router 各自 `prefix`，`create_app` 再加 `/api`。不要和 threads/runs 写在同一个文件里。

| Method | Path | 请求 | 成功响应 | 能力 | 所属文件 |
| --- | --- | --- | --- | --- | --- |
| GET | `/api/mcp/config` | 无 | `McpConfigResponse`（200） | `admin` | `app/gateway/routers/mcp.py` |
| PUT | `/api/mcp/config` | `McpConfigUpdateRequest` | `McpConfigResponse`（200） | `admin` | 同上 |
| PATCH | `/api/mcp/config` | `McpServerEnabledPatch` | `McpConfigResponse`（200） | `admin` | 同上 |
| POST | `/api/mcp/cache/reset` | 无 | `McpCacheResetResponse`（200） | `admin` | 同上 |
| GET | `/api/skills` | 无 | `SkillsListResponse`（200） | `skills:read` | `app/gateway/routers/skills.py` |
| POST | `/api/skills/reload` | 无 | `SkillReloadResponse`（200） | `admin` | 同上 |
| PATCH | `/api/skills/{name}` | `SkillUpdateRequest` | `SkillResponse`（200） | `admin` | 同上 |

`GET /api/skills` 不是 `admin`。斜杠菜单要给每个登录用户看已启用技能，和模型目录的 `models:read` 同一条理由。写 `extensions_config.json`、看 MCP 配置、清缓存，才是 `admin`。

`POST /skills/reload` 必须写在 `PATCH /skills/{name}` 前面。反了之后一旦再加 `GET /skills/{name}`，`reload` 会被当成技能名。

#### 参数

```python
# app/gateway/models/request.py
_NAME = r"^[A-Za-z0-9_-]{1,64}$"
SkillName = Annotated[str, Path(pattern=_NAME)]

class McpConfigUpdateRequest(BaseModel):
    """整份替换 mcpServers。没有 skills、没有 mcpInterceptors。"""
    model_config = ConfigDict(extra="forbid")
    mcp_servers: dict[str, McpServerWrite] = Field(alias="mcpServers")

class McpServerEnabledPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=_NAME)
    enabled: bool

class SkillUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
```

`McpServerWrite` 是写入用的服务器对象：`enabled`、`type`（或别名 `transport`）、`command`、`args`、`env`、`headers`、`url`、`tool_name_prefix`、两个 timeout。`extra="forbid"`。`type` 与 `transport` 都在时，`type` 赢（§2.1）。

| 参数 | 约束 | 失败 |
| --- | --- | --- |
| `SkillName` / patch 的 `name` / `mcpServers` 的键 | `^[A-Za-z0-9_-]{1,64}$` | 422 `invalid_request`。斜杠和 `..` 进不了文件名 |
| `SkillUpdateRequest.enabled` | 必填 `bool` | 缺字段 422 `invalid_request` |
| PUT 里多出来的顶层键（`skills`、`mcpInterceptors`、`middlewares`） | `extra="forbid"` | 422 `invalid_request`。这两类会 import 代码，只许改文件（§11） |

技能目录名在扫描时用同一条正则。对不上的目录打日志并跳过，不出现在 `GET /api/skills` 里。否则列表里有名字、PATCH 却 422。

#### 响应与打码

```python
# app/gateway/models/response.py
_MASK = "****"

class SkillResponse(BaseModel):
    name: str
    description: str
    category: Literal["public", "custom"]   # 不是磁盘路径
    enabled: bool

class SkillsListResponse(BaseModel):
    skills: list[SkillResponse]

class SkillReloadResponse(BaseModel):
    reloaded: bool = True                    # 只表示这个 worker

class McpCacheResetResponse(BaseModel):
    reset: bool = True

class McpConfigResponse(BaseModel):
    mcp_servers: dict[str, dict[str, Any]] = Field(alias="mcpServers")
```

`app/gateway/mcp_wire.py` 的 `mask_mcp_config` 把 `env`、`headers`、`user_auth.users` 里每个非空字符串换成 `"****"`。`null` 仍是 `null`。响应里没有原始凭据，也没有宿主机路径。

PUT 的回写在同一个文件的 `restore_masked_secrets`：

| 客户端送来的值 | 磁盘上原来有这个键 | 写入 |
| --- | --- | --- |
| `"****"` | 有 | 磁盘上的原值 |
| `"****"` | 没有（新服务器或新键） | 422 `invalid_extensions_config`。打码串不能变成凭据 |
| 其它字符串 | 不论 | 用客户端的新值 |
| 键被省略 | 有 | 删掉。PUT 是整份替换 `mcpServers`，不是 merge |

PUT **不改**文件里的 `skills` 和 `mcpInterceptors`。读出整份 JSON，只换 `mcpServers` 这一键，再原子写回。

#### handler

读改写都在 `asyncio.to_thread` 里完成，锁在那个线程里拿（§2.2）。路由里不出现 `extensions_config_write_lock`。

```python
# app/gateway/routers/mcp.py
router = APIRouter(prefix="/mcp", tags=["mcp"])


@router.get("/config", response_model=McpConfigResponse)
async def get_mcp_config(
    caller: Annotated[RunCaller, Depends(require_scope("admin"))],
) -> McpConfigResponse:
    del caller
    return mask_mcp_config(await asyncio.to_thread(read_extensions_config))


@router.put("/config", response_model=McpConfigResponse)
async def put_mcp_config(
    body: McpConfigUpdateRequest,
    caller: Annotated[RunCaller, Depends(require_scope("admin"))],
) -> McpConfigResponse:
    del caller
    saved = await asyncio.to_thread(replace_mcp_servers, body)
    return mask_mcp_config(saved)


@router.patch("/config", response_model=McpConfigResponse)
async def patch_mcp_server(
    body: McpServerEnabledPatch,
    caller: Annotated[RunCaller, Depends(require_scope("admin"))],
) -> McpConfigResponse:
    del caller
    saved = await asyncio.to_thread(set_mcp_server_enabled, body.name, body.enabled)
    return mask_mcp_config(saved)


@router.post("/cache/reset", response_model=McpCacheResetResponse)
async def reset_mcp_cache(
    caller: Annotated[RunCaller, Depends(require_scope("admin"))],
) -> McpCacheResetResponse:
    del caller
    await asyncio.to_thread(reset_local_mcp_cache)   # agentkit/mcp/cache.py，只清本进程
    return McpCacheResetResponse()
```

```python
# app/gateway/routers/skills.py
router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("", response_model=SkillsListResponse)
async def list_skills(
    caller: Annotated[RunCaller, Depends(require_scope("skills:read"))],
) -> SkillsListResponse:
    del caller
    return SkillsListResponse(skills=await asyncio.to_thread(load_skill_catalog))


@router.post("/reload", response_model=SkillReloadResponse)
async def reload_skills(
    caller: Annotated[RunCaller, Depends(require_scope("admin"))],
) -> SkillReloadResponse:
    del caller
    await asyncio.to_thread(clear_skill_prompt_cache)   # 本进程；不清别的 worker
    return SkillReloadResponse()


@router.patch("/{name}", response_model=SkillResponse)
async def update_skill(
    name: SkillName,
    body: SkillUpdateRequest,
    caller: Annotated[RunCaller, Depends(require_scope("admin"))],
) -> SkillResponse:
    del caller
    return await asyncio.to_thread(set_skill_enabled, name, body.enabled)
```

| 步骤 | MCP 写 | 技能 PATCH | 只读 |
| --- | --- | --- | --- |
| 1 | `admin` 在进函数之前 | 同左 | 配置是 `admin`；技能列表是 `skills:read` |
| 2 | `to_thread` 里拿双锁，读文件 | 同左，改的是 `skills.{name}.enabled` | 不加写锁 |
| 3 | 启用 stdio 时跑 §5.7；禁用跳过命令检查 | 名字不在扫描结果里 → `SkillNotFound` | `mask_mcp_config` 或 `load_skill_catalog` |
| 4 | `restore_masked_secrets`，只替换 `mcpServers`，原子写，再 `reload_extensions_config` | 同一把锁，不改 `mcpServers` | — |

`set_mcp_server_enabled` 找不到 `name` 抛 `McpServerNotFound`。已经坏掉的服务器也要能关掉，所以 `enabled=false` 不跑允许列表。`enabled=true` 且 `type=stdio` 时 `command` 不在允许列表里 → `StdioCommandRejected`。远程 transport 不跑这三项。

`POST /cache/reset` 和 `POST /skills/reload` 只影响打到的这个进程。多 worker 要逐个打，或者等 §2.3 的内容签名自己失效。响应里不要承诺「全集群已刷新」。

显式配置路径的文件中途不见了：`from_file` 抛 `FileNotFoundError`，路由不接，落到 500 `internal_error`。日志里有路径，响应里没有。

#### 错误

| 异常 | HTTP | `code` | 谁抛 | 所属文件 |
| --- | --- | --- | --- | --- |
| `McpServerNotFound` | 404 | `mcp_server_not_found` | PATCH 的 `name` 不在文件里 | `app/gateway/errors.py` |
| `SkillNotFound` | 404 | `skill_not_found` | PATCH 的目录没被扫描收进目录 | 同上 |
| `StdioCommandRejected` | 422 | `stdio_command_rejected` | 启用时 `command` 不在允许列表（§5.7） | 同上 |
| `InvalidExtensionsConfig` | 422 | `invalid_extensions_config` | 打码串没有原值可还原；校验失败又不是命令允许列表 | 同上 |

`detail` 不包含 `env` / `headers` 的值，也不包含被拒绝的命令行。命令被拒时只说允许的裸名字集合。

#### 不做

| 不做 | 理由 |
| --- | --- |
| `POST /api/skills/install`、custom 技能的读写和回滚 | §11：M7 不做扫描和 per-user custom。启用开关只改 JSON 里的 `enabled` |
| 写入 `mcpInterceptors` / `middlewares` | 会 import 代码 |
| 路由里直接 `open(extensions_config.json)` | 双锁和原子写只在 `extensions_config.py` |
| 把 `GET /mcp/config` 开放给 `skills:read` | 打码之后的形状仍是运维配置。选择器要的是技能名，不是服务器令牌 |
| 在 reset 路由里广播到其它 worker | M7 没有跨进程控制面。签名失效是那条路 |


---

## 3. 技能系统（D3–5）

### 3.1 形状

```
skills/
├── public/                    # 提交进仓库，只读
│   └── pdf-processing/
│       ├── SKILL.md
│       └── scripts/extract.py
└── custom/                    # gitignored；M7 先做全局 custom，M9 再拆 per-user
```

沙箱里挂在 `skills.container_path`（默认 `/mnt/skills`）：
`{container_path}/{public|custom}/<relative>/SKILL.md`。
`container_path` 是 STARTUP_ONLY（M0 登记表）：它被编译进提示词和沙箱 mount。

```markdown
---
name: pdf-processing
description: Handle PDF documents efficiently
license: MIT
allowed-tools: [read_file, write_file, bash]
argument-hint: "<file path>"
required-secrets: []
secrets-autonomous: true
---

# Skill Instructions
...
```

`name` 必须匹配 `^[a-z0-9]+(?:-[a-z0-9]+)*$`（和 slash 正则同一条）。
目录名与 `name` 不一致时以 frontmatter 为准，加载时打 warning。

**技能文本一律按 UTF-8 处理。** 读写都显式 `encoding="utf-8"`，不要依赖平台
locale——Windows 默认 GBK，中文技能会乱码或抛异常。每个新读写点都会重犯，
写进项目规范。

### 3.2 `Skill` 数据类

```python
class SkillCategory(StrEnum):
    PUBLIC = "public"
    CUSTOM = "custom"
    # INTEGRATION / LEGACY —— M7 不实现，枚举值先占位以免字符串漂移

@dataclass(frozen=True)
class SecretRequirement:
    name: str          # 既是 context.secrets 的键，也是注入的环境变量名
    optional: bool = False

@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    license: str | None
    skill_dir: Path
    skill_file: Path
    relative_path: Path
    category: SkillCategory
    allowed_tools: tuple[str, ...] | None = None   # None = 未声明
    enabled: bool = False
    required_secrets: tuple[SecretRequirement, ...] = ()
    secrets_autonomous: bool = True
```

`enabled` 来自 JSON `skills.<name>.enabled`，缺省 `true`（扫描到的新技能默认开，
避免"放进 public/ 却看不见"）。公开技能的关闭是运维动作，走 PATCH。

### 3.3 `allowed-tools` 解析

frontmatter 既可以是 YAML 列表，也可以是可移植的标量字符串
（`Bash Read Write`）。已知别名：

| 可移植名 | 运行时名 |
|---|---|
| `Bash` | `bash` |
| `Read` | `read_file` |
| `Write` | `write_file` |
| `Edit` | `str_replace` |
| `Glob` / `Grep` | `glob` / `grep` |
| `WebFetch` / `WebSearch` | `web_fetch` / `web_search` |

小写或未知标量**保持原样**。带括号的参数作用域条目（`Bash(git *)`）原样保留且
**不生效**——策略不检查参数，tokenizer 要保住空格、引号、转义括号。
未闭合引号/括号在加载时炸，不要静默截断。

`required-secrets` 接受字符串列表或 `{name, optional}` 映射。`name` 必须是
POSIX 环境变量名 `^[A-Za-z_][A-Za-z0-9_]*$`。畸形条目丢掉并 warning，不要让
整个技能加载失败。`secrets-autonomous` 畸形值 fail-closed 到 `false`。

### 3.4 加载：两个容易忽略的规则

**技能目录是一个包边界：嵌套的 `SKILL.md` 不注册为运行时技能。**
扫描时遇到一个 `SKILL.md` 就停止向下递归。包里的示例、fixture、子模板
不应变成用户能启用的独立技能。

**符号链接要区分对待。** 允许技能目录本身是一级符号链接
（运维管理的技能树经常这么组织），但激活时要拒绝符号链接的 `SKILL.md`
和任何更深层的路径逃逸（`resolve()` 后 `relative_to(skills_root)` 失败就拒）。

同名：custom 盖 public。加载时打 warning。slash 解析按 `name` 取第一条
enabled 命中，所以覆盖必须在组成 `list[Skill]` 时就做掉。

`load_skills()` **每次扫描磁盘**，不要缓目录。enabled 状态缓的是 JSON。
`POST /api/skills/reload` 只清 prompt-section LRU 和 per-(config, user) 句柄。

### 3.5 两种发现模式

| 模式 | 系统提示词里放什么 | 模型怎么拿到全文 |
|---|---|---|
| **全量注入**（默认） | `<available_skills>`：每个技能的 name / description / 容器路径 | 直接就有；再 `read_file` 读正文 |
| **延迟发现** | `<skill_index>`：只有名字 | `describe_skill` 拿元数据，再 `read_file` |

**M7 先做全量注入，把 `skills.deferred_discovery` 开关和 `describe_skill` 留出来。**

延迟发现的价值在技能超过二三十个时才体现：全量元数据会占掉几千 token，
而且**每加一个技能整个系统提示词的前缀缓存就失效一次**。
这和 M6 §4.4 的"基础系统提示词必须完全静态"是同一个考量——技能块是
**系统提示词里少数允许变的尾部**，变了就接受缓存失效；延迟模式把尾部收成
稳定的名字列表。

`describe_skill` 的 catalog：`select:a,b` 返回全部点名的（无上限）；
`+prefix` / 自由文本 regex 上限 5 条。非法 regex 退化成字面子串，不要 500。

渲染时 HTML-escape name/description，避免技能描述里的 `<system-reminder>`
伪造框架标签（M6 清洗管用户输入，不管系统提示词）。

### 3.6 激活：两条路径，权威性不同

**Slash 激活**：用户输入 `/pdf-processing 把这个文件转成文本`。
严格语法，只对本次模型调用把该技能的 `SKILL.md` 注入为隐藏 HumanMessage。

```python
RESERVED_SLASH_SKILL_NAMES = frozenset({
    "agent", "bootstrap", "goal", "help", "memory", "models", "new", "status",
})
_SLASH_SKILL_RE = re.compile(r"^/([a-z0-9]+(?:-[a-z0-9]+)*)(?:\s+|$)")
```

拒绝：前导空白（正则 `^/`）、缺少分隔符、保留频道命令、未启用、不在当前
agent 白名单（`available_skills`）。解析用 `get_original_user_content_text`
（M6 §4.2）——清洗/上传包装之后 content 已经不是 `/skill ...` 了。

这份正则和保留名必须进 `contracts/slash_skill_contract.json`，前端展示解析
用同一份。一边改一边漏，CI 红。

**自主读取**：模型用 `read_file` 读了某个 `SKILL.md`。DurableContext（M6）
从 tool 对捕获 `skill_context` 条目（name/path/description，**不是正文**）。

Slash 注入：

- hook：`wrap_model_call`，**只改 request**，不写 state（和 M6 视觉载荷同一纪律）。
- 盖 provenance：`content_kind=skill_body`，`producer_kind=skill_activation`。
- `hide_from_ui=True`。
- 正文 `html.escape`。
- 磁盘读取 `encoding="utf-8"`，路径走 storage 的 `validate_skill_file_path`。
- 同一 run 里同一条 slash 消息只激活一次：run context 记消息 id，避免工具循环
  里每次模型调用都重读磁盘、重发 audit。SKILL.md 注入本身不进 checkpoint，
  所以第 2..N 次模型调用要从 run context 知道"已经激活过"。
- audit：`middleware:skill_activation`（M4 catalog 已留 tag）。

`slash_source_owner_token`：组装时 `secrets.token_urlsafe(24)`，**只传给**
Activation 和 Policy 这一对。slash 源路径写在 `runtime.secret_context` 的
保留键里，读必须持有同一 token。`runtime.context` 可被调用方合并，没 token
的伪造路径必须当无。

### 3.7 `allowed-tools` 是行为约束，不是安全边界

**规则一：只对"真正激活"的技能生效。** 被动启用、agent 配置里的技能白名单，
**不收紧基线工具集**。否则启用一个只声明了 `read_file` 的技能，整个 agent
就失去写文件的能力。

**规则二：slash 策略是支配性的。** 用户显式 `/some-skill` 之后，模型再读别的
`SKILL.md` **不能扩大**当前工具权限。没有 slash 时，自主捕获的技能之间用并集。

并集细节：一旦有任何一个技能**声明了** `allowed-tools` 字段，没声明该字段的
技能贡献空集，而不是"放行全部"——否则一个未声明的技能会把显式限制拆掉。
声明了空列表 = 这个技能自己不贡献业务工具。

**规则三：这是 best-effort 的行为约束，不是安全边界。**

模型可以用 `bash cat /mnt/skills/xxx/SKILL.md` 读全文，这条路径不会被捕获。
自主 `skill_context` 有容量上限，老条目会被驱逐。**必须在模块文档和
`SkillToolPolicyMiddleware` 的 docstring 写明。** 把它当"用受限技能隔离不可信
用户"是错的，真正的边界是沙箱和鉴权。

### 3.8 框架工具 vs 业务工具

即使最严格的技能策略下，这些名字始终可用：

```python
ALWAYS_AVAILABLE_BUILTIN_TOOL_NAMES = frozenset({
    "describe_skill",
    "read_file",
    "tool_search",
})
```

它们是发现基础设施。**发现不等于可用**：`tool_search` 提升出来的业务工具
仍然需要当前激活策略的显式声明才能出现在 schema 里或被执行。

**`task` 不是框架豁免的。** 否则受限技能可以通过委派绕过自己的策略。
以后若加 MCP 长时任务的 `list_background_tasks` / `cancel_background_task`，同样要显式声明；
M8 的普通 `task` 委派不借用这两个名字。

### 3.9 `SkillToolPolicyMiddleware`

M6 组装顺序：`SkillActivation` **紧挨** `SkillToolPolicy` **紧挨**
`DurableContext`。中间插别的会让 slash token 或 `skill_context` 对不上。

每次 `wrap_model_call`：

1. 从 live registry 按规范化容器路径解析当前激活技能（slash 路径或
   `skill_context` 条目）。**不要信 context 里存的 allowed-tools 列表。**
2. 算出允许集合，过滤 `request.tools`（模型可见 schema）。
3. 把版本化、JSON-safe、带路径签名的决策写入 secret_context
   （`SKILL_TOOL_POLICY_DECISION_CONTEXT_KEY`），本步产生的 tool_call 复用。
4. 下一轮模型调用**总是重算**。畸形 / 外来 / 过期 / 对不上的决策作废，回退现场解析。

`wrap_tool_call`：不在允许集合里的调用短路成错误 `ToolMessage`（盖
`deerflow_tool_meta`，`recoverable_by_model=True`，建议换工具），不调用 handler。

失败语义：

| 情况 | 行为 |
|---|---|
| 注册表加载失败 | fail-closed 到框架工具 |
| 激活集非空但 0 个能解析 | 同上 |
| 个别路径过期，还有别的有效技能 | 跳过坏的，用剩下的 |

决策的 owner token 是鉴权敏感的，键进 `REDACTED_CONTEXT_KEYS`，可观察/持久化的
context 副本里看不到。

### 3.10 DurableContext 捕获技能引用

M6 已有通道。M7 把捕获补全：`read_file` / `read` / `view` / `cat`（配置
`summarization.skill_file_read_tool_names`）的结果，路径在
`container_path` 下且 basename 是 `SKILL.md`，抽出
`{name, path, description}` 写入 `ThreadState.skill_context`。
description 截断（M1 的 `_SKILL_DESCRIPTION_MAX_CHARS`）。**不要把正文放进
state**——那是 slash 注入的事，而且会让 checkpoint 膨胀。

`review_skill_package`（以后）的结果**不得**带 `skill_context_entry` 标记，
避免"审一下"变成激活。

---

## 4. 请求级密钥（契约现在定，实现可薄）

技能脚本有时需要终端用户的短期凭据。写进提示词不可接受。

- **声明**：`required-secrets`
- **携带**：`context.secrets`，**绝不放进消息**。M3 白名单放行这个键；
  `__` 前缀键仍然剥掉。
- **绑定**：启用 × 请求提供了值 × frontmatter 声明过，取交集。每次模型调用
  按 live registry 重算。slash 源只存**规范化容器路径**，不存声明过的密钥名。
- **注入**：`execute_command(env=...)`，不拼进命令字符串。继承环境擦除走
  M2 `build_sandbox_env`（`*KEY*`/`*SECRET*`/`*TOKEN*`…）。
- **审计**：`middleware:skill_secrets` 只记技能名和密钥**名**，不记值。
- 调用方停供某个值，或技能被逐出 `skill_context`，下一次调用失去注入。
- 子代理不继承这套绑定（M8）。
- `runs.kwargs_json` 存 `redact_config_secrets(body.config)`（M4 已要求）。

伪造 `__slash_skill_secret_source` 无害：解析走 registry + owner token。

M7 可以先做声明+剥离+绑定重算，bash `env=` 注入跟 M2 的 `execute_command`
接上即可。Vault / 持久化凭据不做。

---

## 5. MCP（D6–9）

### 5.1 基础

用 `langchain-mcp-adapters` 的 `MultiServerMCPClient`，stdio / SSE / HTTP。
工具懒加载（`get_cached_mcp_tools()` 第一次用时），按 §2.3 失效。

进 M2 工具列表的顺序不变：**配置工具 → 内置 → MCP**，按 `tool.name` 去重，
先到先得。MCP 工具打 `metadata["deerflow_mcp"] = {server, original_name}`，
后续源路由、延迟提升、结果消毒都认这个标，**不认名字前缀**。

工具名在加载边界就正则约束：`^[A-Za-z0-9_-]+$`。延迟工具不会被 provider
的 function-calling 校验扫到，名字会进系统提示词；换行/尖括号可以伪造框架结构。

**工具名默认加 `<server_name>_` 前缀。** 两个服务器都提供 `search` 很常见，
不加前缀会在 M2 去重里被静默丢掉一个。`tool_name_prefix: false` 允许关掉
（工具自己已有稳定命名空间），但源路由必须按"哪个服务器产生的"判断。

`server_name` 自身也要是安全标识符，否则前缀会把脏字符带进工具名。

### 5.2 超时

| 配置 | 默认 | 覆盖 |
|---|---|---|
| `session_init_timeout` | 60s | 发现 + stdio 会话初始化；`None` = 不限 |
| `tool_call_timeout` | 60s | stdio 单次调用；HTTP/SSE 普通工具走传输层超时 |

挂掉的服务器不能阻塞 `make_lead_agent`。单台失败：日志 + 跳过，其它服务器照常。
静态 header 非法（§5.6）同样跳过那一台，只记原因。

### 5.3 stdio 会话池

**会话按 `(server_name, user_id:thread_id)` 作用域。** stdio 服务器是有状态子进程。

**固定 `cwd` 和临时目录。** 默认 `cwd` = thread workspace，
`TMPDIR`/`TMP`/`TEMP` = `workspace/.mcp/tmp/`（运维显式配了除外）。
`.mcp` 加入 M2 的 `EXCLUDED_DIR_NAMES`——过程产物，不算用户成果。
SSE/HTTP 不走这套文件系统准备。

返回的本地路径若落在用户数据树内，翻译成 `/mnt/user-data/...`；之外保持原样。
**不要拷文件。**

**Owner task。** `ClientSession` 建立在 anyio task group 上，`__aenter__` /
`__aexit__` 必须是同一 task。同步工具包装每次 `asyncio.run` 一条新 loop，
跨调用进出同一 session 会 `RuntimeError: Attempted to exit cancel scope in a
different task`。解法：每个池化 session 由专职 `_run_session` task 拥有，
它 enter、把 session 交给调用方、等 close event；所有关闭路径只 **signal**
那个 event，由 owner 自己 `__aexit__`。

Owner 也是唯一能把 session 登记进池子的 writer：`initialize()` 成功后在同一
临界区注册 + resolve future。调用方永远拿不到"还绑在某个可能被取消的 caller
上"的半成品 session。

池满 LRU 驱逐。关闭走 close event，不从别的 task `__aexit__`。

**断连恢复要精确。** 明确的 `Connection closed`（MCP SDK）或 AnyIO
closed-stream：只驱逐那一个 `(server, user:thread)`，而且**只在注册表里的
条目仍然是失败的那个 session 对象时**才驱逐。晚到的旧错误不能把刚建的替代
会话干掉。失败那次照常向上报错，**绝不自动重放**——重放有副作用的工具比报错糟。

普通 `isError=true` 和协议超时**不驱逐**健康会话。拦截器失败也不驱逐。

HTTP/SSE 保持一次性会话；凭据拦截器仍然套上。

### 5.4 拦截器怎么读运行时上下文

```python
# ✗ 工具调用内部这个键是 None
context = get_config()["context"]

# ✓ LangGraph 把 ToolRuntime 注入名为 runtime 的参数
runtime = request.runtime
user_id = resolve_runtime_user_id(runtime)
secrets = extract_request_secrets(runtime.context)
```

运行上下文挂在 runtime 上，**不在**传给子 runnable 的 `RunnableConfig` 里。
写一条测试：关掉 ambient `get_runtime()` fallback，驱动真实适配器工具走真实的图，
断言拿得到 context。上游改了参数注入约定会立刻红。

### 5.5 凭据：四层优先级，全部 fail-closed

```
静态 headers  <  oauth  <  per-user  <  per-request
```

拦截器注册顺序（先注册在外层，后注册更靠近传输层、赢得最终 header）：

1. OAuth（token 端点，自动刷新）
2. `user_auth`（DeerFlow 用户 → 凭据）
3. `headers_from_context`（`context.secrets` 的键）
4. 运维文件里的 `mcpInterceptors`（API 不可写）

| 层 | 用途 | stdio |
|---|---|---|
| 静态 `headers` | 启动发现 | 不适用（用 `env`） |
| `oauth` | `client_credentials` / `refresh_token` | 跳过 + warning |
| `user_auth` | 按用户映射，`$ENV` 可用 | 跳过 + warning |
| `headers_from_context` | 按请求，映射里是名字不是值 | 跳过 + warning |

**Fail-closed 是默认。** 映射找不到当前用户、或解析出空值，抛 `ToolException`，
只点名缺失的键。静默回落到发现凭据 = 一个租户用另一个凭据的权限在跑。
放行场景显式 `on_missing: "passthrough"`。

`user_auth.users` 的值 GET 时打码；`headers_from_context` 只存名字，不打码，
PUT 整块替换。

### 5.6 Header 名大小写 + 非法值

HTTP 字段名大小写不敏感，但一路上的 dict 都是大小写敏感的。适配器用
`{**static, **override}` 合并。静态写了 `authorization`、注入写 `Authorization`，
两个都到达 httpx，服务端单值访问器拿到的是**静态那个**，优先级被静默反转。

所有凭据注入走 `apply_header_overrides`：丢掉只有大小写不同的键，沿用连接
已经在用的拼写。配置加载时静态 `headers` 若出现两种拼写直接拒。

**凭据值非法时绝不能回显。** 传输层（h11）在 header 值含换行或首尾空白时抛的
异常**消息里带着完整的值**。M6 的 `ToolErrorHandlingMiddleware` 会把工具错误
抄进模型可见消息。两者叠加，格式错误的凭据会进提示词、checkpoint、trace。

```python
def illegal_header_value_reason(value: str) -> str | None:
    # 非 ASCII / 换行控制符 / 首尾 SP|HTAB
    # 返回原因短语，绝不把 value 编进返回值
```

检查对象是**渲染后的完整值**（`<token_type> <access_token>`），不是分开检查
两个字段。OAuth 管理器、两个拦截器、`build_server_params` 发现路径，全部走
这一个函数。非法值**无视** `on_missing: passthrough`——那是"缺值"的开关，
不是"脏值也可以送"的开关。

### 5.7 API 注册 stdio 的启动校验

配置文件可以写任何东西（运维的文件），**HTTP API 是不可信输入**。
通过 API 注册/启用 stdio 服务器时：

**M7 只做第 1 条：** `command` 必须是允许列表里的裸可执行文件名
（默认 `{npx, uvx}`，`DEER_FLOW_MCP_STDIO_COMMAND_ALLOWLIST` 可扩），
拒绝路径分隔符、空白、shell 元字符。

后两条（args 任意执行 flag、`PYTHONPATH` 这类无条件执行调用方代码的 env）
正确实现非常微妙（`npx` 在包名处停止解析自己的 flag，选项区边界依赖每个
flag 的元数）。**在把 MCP 注册开放给非运维角色之前不值得投入。**

> **这是纵深防御，不是信任边界。** `npx`/`uvx` 存在的意义就是拉取并执行远程包。
> 真正的边界是**管理员认证 + 网络可达性**。不要以为加了检查就可以把 MCP
> 注册开放给不可信管理员。

远程 transport 不跑这三项——它们不 spawn。

---

## 6. 工具检索（D10，按需）

**触发条件：MCP 工具数量上百，或 `tool_search.enabled: true`。**
全量 schema 会占几万 token，模型在一百个工具里选对的准确率会显著下降。

延迟提升：

1. 带 `deerflow_mcp` 标的工具 schema 默认对模型隐藏。
2. 系统提示词给一个 `<available-deferred-tools>` 名字列表（escape）。
3. `tool_search`：模型描述需求，返回最多 5 条匹配；命中的名字写入
   `ThreadState.promoted`（M1 若还没有这个通道，M7 加上，reducer 用保序去重并集）。
4. `DeferredToolFilterMiddleware` 读 `promoted`，把那些 schema 放进本次
   `request.tools`。
5. 可选 `McpRoutingMiddleware`：根据最新真实用户消息（`is_real_user_message`）
   自动提升明显匹配的工具，只提升不执行。新名字发 `middleware:tool_promotion`，
   `source=routing_hint`；重复 pass 不发。

catalog hash（schema 规范化 JSON 的 sha256 前 16 位）绑在 deferred 集合上。
配置变了，旧 `promoted` 里的名字可能已经不存在——filter 按"当前 catalog 里还有
这个名字"为准，幽灵名字忽略。

`tool_search` 自己是框架工具，技能策略放行；它提升出的业务工具仍要过策略。

搜索 query 非法 regex → 字面匹配。`select:a,b` 形式可留给技能 catalog；
工具检索用自由文本即可。

组装位置（M6 已留）：routing 在 deferred filter **之前**；filter 在 coalescing 之前。
Promotion audit（可选）必须在 SkillToolPolicy **之外**（更外的 wrap_tool），
否则被策略拒绝的名字还会进审计账。

---

## 7. 社区搜索/抓取（可选，与 MCP 并列的"加能力"）

路线图把 `community/` 的 web_search / web_fetch 列在 M7。它们是**第一方工具**，
走 M2 内置/配置源，不是 MCP。M7 若做：

- 独立 extra（`tavily` / `firecrawl` / …），缺 extra 时工具不注册，不要 ImportError
  打爆 `make_lead_agent`。
- 结果算远程内容：M6 W3 的 `ToolResultSanitization` 名字允许列表要包含
  `web_fetch` / `web_search`。
- 失败走 `normalize_tool_result` 的 `no_results` / `transient`。
- 不要在 M7 做图片搜索、浏览器自动化；那是另一堵墙。

不做也不阻塞 MCP/技能验收。

---

## 8. 接到 M1/M2/M6 的缝

| 缝 | 约定 |
|---|---|
| M2 `collect_tools` | 第三源 `get_cached_mcp_tools()`；去重先到先得 |
| M1 系统提示词 | 技能块作为静态提示词的**尾部**；`deferred_discovery` 切换两种渲染 |
| M6 组装 | Activation → Policy → DurableContext，共享 `slash_source_owner_token` |
| M6 `is_real_user_message` | slash 解析、MCP 路由提示都用它 |
| M6 provenance | 技能正文 `skill_body`；durable 投影已有 |
| M4 catalog | `middleware:skill_activation` / `skill_secrets` / `tool_promotion` |
| M3 context 白名单 | 放行 `secrets`；剥 `__` 键和 provenance |
| M4 `kwargs_json` | 已脱敏；密钥不能进 runs 表 |
| ThreadState | `skill_context`（M1 已留）；`promoted`（D10 补） |

`make_lead_agent` 把 `available_skills`（自定义 agent 白名单，M7 可恒为 None）
和 `user_id` 传进两条技能中间件。

---

## 9. 测试清单

| 文件 | 覆盖 |
|---|---|
| `test_config_concurrent_write.py` | 并发 RMW 不丢更新；崩溃不留半截文件；仅 `EBUSY` 退化；其它 errno 抛；技能 PATCH 与 MCP PUT 交叉不丢 |
| `test_mcp_skills_routes.py` | `GET /skills` 要 `skills:read` 不要 `admin`；PUT/PATCH/reload/mcp 要 `admin`；`POST /skills/reload` 不会被当成技能名；`skills` / `mcpInterceptors` 出现在 PUT body → 422；`"****"` 还原原值，没有原值 → 422 `invalid_extensions_config`；响应 JSON 不含原始 env；启用非法 stdio `command` → 422 `stdio_command_rejected` 且 detail 无命令行；禁用坏配置仍 200；未知技能 404 `skill_not_found` |
| `test_config_lock_in_worker.py` | 锁在 `to_thread` worker 内持有；取消 awaiting task 不提前放锁 |
| `test_config_signature.py` | 同秒编辑、mtime 倒退、换更老文件 → 判定已变更；加载中签名变化走会话退役 |
| `test_extensions_path.py` | 显式路径缺失 → FileNotFoundError；搜索模式缺失 → None |
| `test_skill_loading.py` | 嵌套 `SKILL.md` 不注册；一级符号链接目录可用；深层逃逸被拒；UTF-8 中文；custom 盖 public |
| `test_skill_parser.py` | `Bash`→`bash`；未知名保持；未闭合括号炸；畸形 required-secrets 丢掉技能仍加载 |
| `test_slash_skill_contract.py` | 与 `contracts/slash_skill_contract.json` 双向钉死；前导空白/保留命令拒绝 |
| `test_skill_policy.py` | 被动启用不收紧；slash 支配；并集；空声明不拆别人的限制；`task` 不豁免；框架工具始终可用；`tool_search` 提升的业务工具仍要声明 |
| `test_skill_policy_fail_closed.py` | 注册表失败 → 只剩框架工具；伪造 slash 路径无 token → 当无 |
| `test_skill_activation_request_only.py` | SKILL.md 注入不出现在 checkpoint；同一 slash 本 run 只读盘一次 |
| `test_mcp_tool_prefix.py` | 同名不冲突；关前缀后源路由仍按 server 元数据 |
| `test_mcp_tool_name_charset.py` | 名字含换行/空格的 MCP 工具被丢弃 |
| `test_mcp_stdio_session.py` | `(server, user:thread)` 隔离；断连只驱逐自己；晚到旧错误不误伤；失败不自动重放；owner task 进出同一 task |
| `test_mcp_credentials.py` | 四层优先级；缺失 fail-closed；`passthrough` 放行缺值；**非法值不出现在异常/工具结果/日志** |
| `test_mcp_header_case.py` | 静态 `authorization` + 注入 `Authorization` 最终只有一个键 |
| `test_mcp_runtime_context.py` | 真实适配器工具 + 真实图，关掉 ambient fallback，断言 `request.runtime` |
| `test_mcp_transport_alias.py` | 只写 `transport: http` 不当 stdio |
| `test_tool_search.py` | 未提升的 MCP 工具不在 schema；提升后可见；catalog 变更后幽灵名忽略；非法 regex 不 500 |
| `test_api_stdio_allowlist.py` | API 拒绝 `command: /usr/bin/python`；直接改文件仍可加载（运维路径） |

第一个和 `test_mcp_credentials.py` 的"不回显"最值钱。不回显要断言得狠：
构造带换行的凭据，完整字符串不得出现在 `str(exc)`、ToolMessage.content、
caplog 文本里。

---

## 10. 验收清单

- [ ] 配一个 MCP server（stdio / SSE / HTTP 各一次），重启后工具可用
- [ ] 通过 API 启用/禁用一台 MCP，**不重启**当前 worker 即生效（其它 worker 要 `cache/reset` 或等签名失效）
- [ ] `GET /api/mcp/config` 的 `env` / `headers` 只有 `"****"` 或 `null`；把 `"****"` 原样 PUT 回去不改磁盘上的真值
- [ ] 缺 `admin` 的令牌调 PUT/PATCH/reload → 403；只有 `skills:read` 的令牌仍能 `GET /api/skills`
- [ ] 两个并发 PATCH（一个改技能、一个改 MCP），两个改动都在文件里
- [ ] 写配置的瞬间 kill 进程，重启后 JSON 仍是完整合法文件
- [ ] 新建 `skills/public/foo/SKILL.md`（中文正文），Windows 上模型能发现
- [ ] `/foo task` 激活后该技能 `allowed-tools` 生效；未 slash 的启用技能不收紧工具集
- [ ] 受限 slash 下 `read_file` 另一个更宽的 SKILL.md，工具集不扩大
- [ ] 故意带换行的 MCP 凭据，错误信息里没有凭据内容
- [ ] stdio 服务器被 kill，下一次调用报错但不影响其它 thread 会话，再下一次自动重建
- [ ] 客户端 POST 的 `context.secrets` 不出现在 `GET /runs/{id}` 的 kwargs 里
- [ ] slash 合同前后端一致：`/new` 不当技能，`/pdf-processing` 当技能

并发 PATCH 用脚本打，手工点很难撞上竞争窗口。

---

## 11. 刻意不做的事

| 不做 | 理由 |
|---|---|
| MCP 长时任务（durable task） | 租约 + 轮询 + 通知投递；M9 默认跳过，形状见 [M9 §6](2026-09-16-m9-production-design.md) |
| 技能静态扫描 / SkillScan / 质量审查 | 有社区分发需求时再做 |
| 技能沙箱投影（物理隔离视图） | 按 agent 策略做文件级隔离；M7 用整树 mount |
| 托管集成（Lark CLI 等） | 产品化，不是框架能力 |
| per-user `skills/custom` 拆分 | M9 多租户时再搬；M7 全局 custom 即可 |
| MCP stdio 的 args/env 深度校验 | 见 §5.7 |
| 工具检索 | 见 §6。没上百个工具就不需要；开关和 `promoted` 通道可先留 |
| 请求级密钥的 vault | 契约和三重门现在定；值请求级、不落盘 |
| 从 API 写 `mcpInterceptors` / `middlewares` | 会 import 代码，只留给运维改文件 |

---

## 12. 一句话总结

M7 交付两条"加能力"的路径，真正的风险集中在一个不起眼的地方：
**那个既被 API 写、又被运行时读的 JSON 文件。**

检验标准：两个用户同时在网页上改配置，两个改动都保住；
进程在写配置的瞬间被 kill，重启后配置完好；
一个格式错误的 MCP 凭据，模型、checkpoint、日志里都看不到它的值。
这三条过不了，MCP 和技能做得再全也会在用户第一次多点几下之后丢状态或漏秘密。

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
├── skills_config.py              # path / container_path / 索引渲染参数（无模式开关）
├── skills_prefs.py               ★  # 个人开关的存储路径与锁（skills_prefs.json）
└── tool_search_config.py         # enabled；D10 才真正用

agentkit/skills/
├── types.py                   ★  # Skill / SkillCategory / SecretRequirement
├── parser.py                  ★  # frontmatter、allowed-tools 别名、UTF-8
├── slash.py                   ★  # 与前端共用 contracts/slash_skill_contract.json
├── tool_policy.py             ★  # union / ALWAYS_AVAILABLE
├── storage.py                    # load_skills() 扫描
├── prefs.py                      ★  # effective_enabled / 个人偏好落盘
├── router.py / loader.py         # search_and_load_skills：按意图检索 + 正文预算
└── prompt.py                     # <available_skills> 索引渲染（唯一形态）

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

app/gateway/mcp_wire.py            # 打码（M7 用）；打码回写留给后续阶段的 PUT
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
| PATCH | `/api/mcp/config` | `McpServerEnabledPatch` | `McpConfigResponse`（200） | `admin` | 同上 |
| POST | `/api/mcp/cache/reset` | 无 | `McpCacheResetResponse`（200） | `admin` | 同上 |
| GET | `/api/skills` | 无 | `SkillsListResponse`（200） | `skills:read` | `app/gateway/routers/skills.py` |
| PUT | `/api/skills/{name}/preference` | `SkillPreferenceRequest` | `SkillResponse`（200） | `skills:read` | 同上 |
| POST | `/api/skills/reload` | 无 | `SkillReloadResponse`（200） | `admin` | 同上 |
| PATCH | `/api/skills/{name}` | `SkillUpdateRequest` | `SkillResponse`（200） | `admin` | 同上（**全局**开关，不是个人开关） |

**普通用户在 M7 里只有一个写操作：`PUT /api/skills/{name}/preference`**（开关"我用不用这个技能"）。
它不是 `admin`——和 `GET /api/skills` 同一条理由：斜杠菜单要给每个登录用户看。

**M7 没有任何"创建"类路由。** 技能不能新建/上传/编辑，MCP 服务器不能新增/删除/整份替换。
这些动作留给后续阶段，且要求**业务管理员**角色——具体见下一节。

`POST /skills/reload` 必须写在 `PATCH /skills/{name}` 前面。反了之后一旦再加 `GET /skills/{name}`，`reload` 会被当成技能名。

#### 谁能改什么：两层开关 + 后续才做的创建

**普通用户对技能只有一个权利：开关"我用不用"。** 技能的正文、frontmatter、目录结构，
普通用户一律不能碰——M7 也不提供任何写入口。


| 层            | 存在哪                                     | 谁能改                    | 影响范围        |
| ------------ | --------------------------------------- | ---------------------- | ----------- |
| **全局启用**     | `extensions_config.json` 的 `skills.<name>.enabled` | `admin`（`PATCH /api/skills/{name}`） | 这个部署里的所有人   |
| **个人偏好**     | `skills_prefs.json` 的 `{user_id: {name: bool}}` | **任何登录用户**（`PUT /api/skills/{name}/preference`） | 只有调用者自己     |


```
effective_enabled(user, skill) = global_enabled(skill) and user_pref(user, skill)   # pref 缺省 true
```

- **两层不能互相覆盖。** 用户不能把一个被全局禁用的技能打开（全局禁用往往意味着这个部署
  缺依赖或缺合规审批）；管理员也不该替用户决定"你用不用"。
- **个人偏好不改任何配置文件里的技能定义**，只写 `skills_prefs.json`：不触发
  `reload_extensions_config()`，也不影响别的 worker 的技能内容缓存——它只改变两件事的输入：
  系统提示词的索引（§3.5）和工具策略的激活集（§3.9）。
- **有效状态是唯一对外可见的状态。** `GET /api/skills`、`<available_skills>` 索引、
  slash 菜单全部读 `effective_enabled`；管理员视角想看全局值，走 `?scope=global`（仅 `admin`）。
- **M7 的存储是 JSON**（`skills_prefs.json`，同一套原子写 + 双锁纪律，§2.2）；
  M9 有了 `users` 表之后可以迁到 `user_skill_prefs(user_id, skill_name, enabled)`，
  **接口一行不改**。无认证时（M7 单用户）`user_id=None` 落在 `"__default__"` 桶。

#### 后续阶段的创建动作（M7 一律不做）

| 动作                                          | 为什么不在 M7                                                                 | 角色            |
| ------------------------------------------- | ------------------------------------------------------------------------ | ------------- |
| 新建技能 / 上传技能包 / 编辑 `SKILL.md`              | 需要完整生命周期：解压、路径逃逸校验、frontmatter 校验、原子落盘、审计、回滚。两周做不完，做一半比不做更危险 | **业务管理员**     |
| 新建 / 删除 / **整份替换** MCP 服务器（原 `PUT /api/mcp/config`） | 整份替换就是"创建 + 删除"的入口。把它留在 M7，等于把创建能力从一个后门放进来了                       | **业务管理员**     |
| 改 `mcpInterceptors` / 技能目录根路径              | 这两样会 import 代码或改变扫描边界（§11），只能由运维改文件                                      | 平台 `admin`   |


**"业务管理员"用能力位表达，不新增角色枚举。** M9 的 `users.system_role` 只有
`admin` / `user`（M9 §2.4）；后续阶段要加的是**能力位** `skills:create` / `mcp:create`，
由 M9 的 `authz.provider` 决定授予谁。**名字现在就定下来**——否则后续阶段会顺手拿 `admin` 顶替，
把"平台运维"和"业务管理"混成同一个权限，想再拆开就得迁移所有人的角色。

上表三行在 M7 里的具体表现：

- `PUT /api/mcp/config` **不注册**（不是返回 403，是压根没有这条路由）——一个"存在但拒绝你"
  的路由会让人以为"申请到权限就能用"，而它其实还没实现。
- 上传/新建技能同样没有路由；`POST /api/skills` 在 M7 是 404。
- 现有的 `PATCH /api/mcp/config`（只改**已有**服务器的 `enabled`）保留：那是启停，不是创建。

#### 参数

```python
# app/gateway/models/request.py
_NAME = r"^[A-Za-z0-9_-]{1,64}$"
SkillName = Annotated[str, Path(pattern=_NAME)]

class McpConfigUpdateRequest(BaseModel):
    """整份替换 mcpServers（整份替换 = 新建 + 删除，**M7 不注册这条路由**，见 §2.4）。

    模型现在就定义好：后续阶段的 `PUT /api/mcp/config` 要用它，而且 §2.4 的能力位
    （`mcp:create`）也已经定名。**不要**为了"暂时用不上"把它删掉再重写一遍。"""
    model_config = ConfigDict(extra="forbid")
    mcp_servers: dict[str, McpServerWrite] = Field(alias="mcpServers")

class McpServerEnabledPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=_NAME)
    enabled: bool

class SkillUpdateRequest(BaseModel):
    """**全局**开关（admin）。改的是 extensions_config.json 里的 skills.<name>.enabled。"""
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class SkillPreferenceRequest(BaseModel):
    """**个人**开关（任何登录用户）。只写 skills_prefs.json，不影响别人。"""
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

PUT（后续阶段）的回写在同一个文件的 `restore_masked_secrets`；M7 里 `PATCH` 只翻
`enabled`，客户端不会回传任何打码值，所以这段逻辑暂时没有调用点：

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


# ⚠ 这里**故意没有** PUT /config：整份替换 = 新建 + 删除服务器，属于"后续阶段的创建动作"，
#   需要业务管理员（see 上一节的表）。M7 只提供"已有服务器的启停"，注册一个拒绝一切请求的
#   PUT 反而是误导——路由不存在比 403 更诚实。


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


@router.put("/{name}/preference", response_model=SkillResponse)
async def set_skill_preference(
    name: SkillName,
    body: SkillPreferenceRequest,
    caller: Annotated[RunCaller, Depends(require_scope("skills:read"))],   # ★ 普通用户
) -> SkillResponse:
    """唯一对普通用户开放的写操作：开关"我用不用"，只影响自己（见上一节的两层开关）。"""
    return await asyncio.to_thread(set_user_skill_pref, caller.user_id, name, body.enabled)


@router.patch("/{name}", response_model=SkillResponse)
async def update_skill_global(
    name: SkillName,
    body: SkillUpdateRequest,
    caller: Annotated[RunCaller, Depends(require_scope("admin"))],         # ★ 全局开关，admin
) -> SkillResponse:
    del caller
    return await asyncio.to_thread(set_global_skill_enabled, name, body.enabled)
```

| 步骤 | MCP 启停（admin） | 技能全局开关（admin） | 技能个人偏好（普通用户） | 只读 |
| --- | --- | --- | --- | --- |
| 1 | `admin` 在进函数之前 | 同左 | `skills:read`（登录即可） | 配置是 `admin`；技能列表是 `skills:read` |
| 2 | `to_thread` 里拿双锁，读文件 | 同左，改 `skills.{name}.enabled` | `to_thread` 里拿 **prefs 自己的锁**，只写 `skills_prefs.json` | 不加写锁 |
| 3 | 启用 stdio 时跑 §5.7；禁用跳过命令检查 | 名字不在扫描结果里 → `SkillNotFound` | 同左 | `mask_mcp_config` 或 `scan_skills_for_api` |
| 4 | 原子写 + `reload_extensions_config` | 同一把锁，不改 `mcpServers` | **不 reload**：全局配置没变 | — |

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

#### handler：`skills.py`

```python
# app/gateway/routers/skills.py
router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("", response_model=SkillsListResponse)
async def list_skills(
    caller: Annotated[RunCaller, Depends(require_scope("skills:read"))],
) -> SkillsListResponse:
    """斜杠菜单与设置页共用。**不是 admin**：每个登录用户都要能看已启用技能（§2.4）。"""
    del caller
    return SkillsListResponse(skills=await asyncio.to_thread(scan_skills_for_api))


@router.post("/reload", response_model=SkillReloadResponse)          # ★ 必须注册在 /{name} 之前
async def reload_skills(
    caller: Annotated[RunCaller, Depends(require_scope("admin"))],
) -> SkillReloadResponse:
    del caller
    await asyncio.to_thread(reload_skill_caches)      # 只清 prompt-section LRU 与 per-(config,user) 句柄
    return SkillReloadResponse()


@router.patch("/{name}", response_model=SkillResponse)
async def update_skill(
    name: SkillName,
    body: SkillUpdateRequest,
    caller: Annotated[RunCaller, Depends(require_scope("admin"))],
) -> SkillResponse:
    del caller
    # 读-改-写-重载整个循环在 worker 线程里、持双锁完成（§2.2）
    return await asyncio.to_thread(patch_skill_enabled, name, body.enabled)
```


```python
# app/gateway/skills_catalog.py
def scan_skills_for_api(*, user_id: str | None, include_global: bool = False) -> list[SkillResponse]:
    """列表与模型索引**必须来自同一份扫描**：列表里有、索引里没有（或反过来）就是 bug。

    `enabled` 返回的是**有效状态** = 全局 and 个人偏好（§2.4 的两层开关）；
    只有 admin 传 include_global=True 时才额外带上 `global_enabled`。"""
    skills = load_skills(get_skills_config())
    prefs = read_user_skill_prefs(user_id)
    return [
        SkillResponse(
            name=skill.name, description=skill.description, category=skill.category.value,
            enabled=effective_enabled(skill, prefs),
            global_enabled=skill.enabled if include_global else None,
        )
        for skill in sorted(skills, key=lambda s: s.name)
    ]


def set_user_skill_pref(user_id: str | None, name: str, enabled: bool) -> SkillResponse:
    """个人开关：只写 skills_prefs.json。**不 reload 全局配置**——它没变。"""
    with skills_prefs_file_lock(prefs_path), skills_prefs_write_lock:
        if name not in _enabled_skill_names():               # 未知技能 → 404 skill_not_found
            raise SkillNotFound(f"Skill {name} not found")
        write_user_skill_pref(prefs_path, user_id, name, enabled)   # 原子写
    return _skill_response(name, user_id=user_id)


def set_global_skill_enabled(name: str, enabled: bool) -> SkillResponse:
    """全局开关（admin）：改 extensions_config.json，动的是所有人看到的东西。"""
    with extensions_config_file_lock(config_path), extensions_config_write_lock:
        data = read_extensions_config_from_disk()
        if name not in {skill.name for skill in load_skills(...)}:
            raise SkillNotFound(f"Skill {name} not found")
        data.setdefault("skills", {})[name] = {"enabled": enabled}
        atomic_write_extensions_config(config_path, data)
        reload_extensions_config()                           # ★ 重载在锁内，紧接着写（§2.2）
    return _skill_response(name, user_id=None)
```

- **`GET /api/skills` 与模型索引共用 `load_skills`**：两处各扫一遍，迟早出现"菜单里有、
  模型看不见"或者反过来的漂移。
- **`PATCH` 不改 `SKILL.md`**：技能文件是内容（可能是 git 管理的），开关是运行状态，两者分开存，
  否则 `git status` 天天是脏的。
- **重载必须在同一个锁域里**：先写文件再重载，中间放锁会让另一个 worker 读到旧内存 + 新文件。

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

`enabled` 是**全局**开关，来自 JSON `skills.<name>.enabled`，缺省 `true`（扫描到的新技能默认开，
避免"放进 public/ 却看不见"）。

**它不是用户看到的状态。** 对外可见的是 `effective_enabled` =
全局 `enabled` **且** 当前用户的个人偏好（`skills_prefs.json`，缺省 `true`）——两层开关的定义见 §2.4。
所以 `Skill` 上不再有"用户的开关"这种字段：它是部署级的对象，用户偏好是另一个存储、另一把锁。

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

#### 代码：`parser.py`

```python
# agentkit/skills/parser.py
FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
_TOOL_ALIASES = {
    "Bash": "bash", "Read": "read_file", "Write": "write_file", "Edit": "str_replace",
    "Glob": "glob", "Grep": "grep", "WebFetch": "web_fetch", "WebSearch": "web_search",
}
_SECRET_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SkillParseError(ValueError):
    """单个技能文件不可用。storage 决定是跳过它还是致命（§3.4）。"""


def parse_skill_file(path: Path) -> ParsedSkill:
    text = path.read_text(encoding="utf-8")            # ★ 显式 UTF-8（§3.1）
    match = FRONTMATTER_RE.match(text)
    if match is None:
        raise SkillParseError("missing frontmatter")
    front = yaml.safe_load(match.group(1)) or {}
    name = str(front.get("name") or "").strip()
    if not _SKILL_NAME_RE.match(name):
        raise SkillParseError(f"bad skill name: {name!r}")
    return ParsedSkill(
        name=name,
        description=str(front.get("description") or "").strip(),
        license=front.get("license"),
        argument_hint=front.get("argument-hint"),
        allowed_tools=parse_allowed_tools(front.get("allowed-tools")),
        required_secrets=parse_required_secrets(front.get("required-secrets")),
        secrets_autonomous=front.get("secrets-autonomous") is not False,   # 畸形 → fail-closed 到 False
        body=text[match.end():],
    )


def parse_allowed_tools(raw: Any) -> tuple[str, ...] | None:
    """None = 未声明；() = 声明了空列表。**这两种语义不同**（§3.7），不能都归一成空。"""
    if raw is None:
        return None
    if isinstance(raw, str):
        tokens = _split_tool_tokens(raw)               # 保住空格 / 引号 / 转义括号；未闭合就抛
    elif isinstance(raw, list):
        tokens = [str(item).strip() for item in raw if str(item).strip()]
    else:
        raise SkillParseError(f"allowed-tools must be a string or a list, got {type(raw).__name__}")
    return tuple(_TOOL_ALIASES.get(token, token) for token in tokens)


def parse_required_secrets(raw: Any) -> tuple[SecretRequirement, ...]:
    """字符串列表或 {name, optional} 映射。**畸形条目丢掉并 warning，不让整个技能失败。**"""
    items: list[SecretRequirement] = []
    if raw is None:
        return ()
    for entry in raw if isinstance(raw, list) else [raw]:
        name = entry if isinstance(entry, str) else (entry or {}).get("name")
        optional = bool((entry or {}).get("optional", False)) if isinstance(entry, dict) else False
        if not isinstance(name, str) or not _SECRET_NAME_RE.match(name):
            logger.warning("Dropping malformed required-secrets entry: %r", entry)
            continue
        items.append(SecretRequirement(name=name, optional=optional))
    return tuple(items)
```


- **`parse_allowed_tools` 返回三态**（`None` / `()` / 非空元组）。策略层（§3.9）要靠 `None`
  区分"没声明，不参与限制"和"声明了空列表，自己不贡献工具"。
- **`secrets-autonomous` 用 `is not False`**：只有显式 `false` 才关，畸形值不改变默认，
  但 §3.3 要求的 fail-closed 是对**畸形**而言——两者不冲突，实现里别写反。

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

#### 代码：`storage.py`

```python
# agentkit/skills/storage.py
def load_skills(config: SkillsConfig, *, enabled_map: Mapping[str, bool]) -> list[Skill]:
    """**每次调用都扫盘**（不缓目录）。返回值已做 custom > public 的覆盖。"""
    found: dict[str, Skill] = {}
    for category in (SkillCategory.PUBLIC, SkillCategory.CUSTOM):   # custom 后写，覆盖 public
        root = Path(config.path) / category.value
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir(), key=lambda item: item.name):   # 稳定顺序
            skill_file = entry / "SKILL.md"
            if not _is_usable_entry(entry, skill_file, root):    # 包边界 + 符号链接校验
                continue
            try:
                parsed = parse_skill_file(skill_file)
            except (SkillParseError, OSError) as exc:
                logger.warning("Skipping skill %s: %s", entry, exc)     # 一个坏技能不拖垮全部
                continue
            if parsed.name != entry.name:
                logger.warning("Skill %s: directory %r != name %r; using frontmatter name",
                               skill_file, entry.name, parsed.name)
            found[parsed.name] = Skill(
                name=parsed.name, description=parsed.description, license=parsed.license,
                argument_hint=parsed.argument_hint, skill_dir=entry, skill_file=skill_file,
                relative_path=entry.relative_to(root.parent), category=category,
                allowed_tools=parsed.allowed_tools, required_secrets=parsed.required_secrets,
                secrets_autonomous=parsed.secrets_autonomous,
                enabled=enabled_map.get(parsed.name, True),        # 扫描到的新技能默认开
            )
    return list(found.values())


def _is_usable_entry(entry: Path, skill_file: Path, root: Path) -> bool:
    """一级符号链接目录允许（运维的技能树常这么组织）；更深层的逃逸一律拒绝。"""
    if not skill_file.is_file():                # ★ 遇到 SKILL.md 就不再向下递归：包边界（§3.4）
        return False
    try:
        entry.resolve().relative_to(root.resolve())
        skill_file.resolve().relative_to(root.resolve())
    except ValueError:
        logger.warning("Refusing skill outside skills root: %s", entry)
        return False
    return True
```

- **`load_skills` 不缓存目录**：enabled 状态缓的是 JSON（§2.3），技能内容每次现读。
  一个技能刚被运维加进来，下一次组装提示词就该出现在索引里。
- **`sorted(iterdir())`** 不是为了好看：它让"同名覆盖"和"索引顺序"在任何文件系统上都一致，
  否则 ext4 与 NTFS 上可能给出不同的结果，测试会偶发红。
- **绝不 `rglob("SKILL.md")`**：那会把技能包里的示例、fixture、子模板全部注册成独立技能。

### 3.5 渐进式加载：唯一路径

**技能永远按两级渐进加载，没有第二种模式、没有开关。** 模型看到的东西从少到多，
每一级都要付一次显式动作：


| 级别      | 在哪                                              | 内容                                                                        | 怎么进入正文                                  |
| ------- | ----------------------------------------------- | ------------------------------------------------------------------------- | ---------------------------------------- |
| **L0 索引** | 系统提示词的 `<available_skills>` 尾部（同一份提示词里唯一提到技能的地方） | 每个技能**一行**：`name`、截断描述、需不需要密钥、`dir=`                                 | 模型读了这一行，决定读不读正文                         |
| **L1 正文** | `read_file` 读 `SKILL.md` / `search_and_load_skills` 按意图加载（slash 激活则由中间件注入）      | SKILL.md 全文（已激活技能的核心 SOP 会同时投影进 `<active_skills>`，见 §3.5.2） | 进入对话或工具结果，同时被 §3.10 捕获为 `skill_context` |


**正文永远不会"预加载"进提示词。** 正文有三个入口，全部是模型/用户的显式动作：
模型自己 `read_file`（自主激活，§3.6）、`search_and_load_skills` 按任务意图检索加载（§3.5.1），
或用户 `/skill-name`（slash 激活，由 `SkillActivationMiddleware` 注入隐藏 HumanMessage）。

**没有中间的"元数据层"。** 早期版本的 L1 是 `describe_skill`（只返回元数据、不返回正文），
在 `search_and_load_skills` 落地后删除——它独有的字段没有一个能改变模型的动作，
理由与"什么情况下该加回来"写在 §3.5 末尾。

#### 3.5.1 `search_and_load_skills`：技能多到读不完时的那一跳

索引只给"名字 + 一行描述"。技能几十个时，模型要么挑错、要么逐个 `read_file` 试——
每一次试错都是一段正文进上下文。`search_and_load_skills` 把这一步变成一次检索：

```text
task_query:     自包含的任务意图（禁代词，带技术栈/协议/报错/目标产出）
domain_keywords: 1~4 个预期出现在正文里的名词、命令、库名
        ↓  召回 candidate_k（默认 20）→ 融合 → 精排 top_k（默认 3）
skills[]: name / description / dir / path / score / channels / body
```

四条约束：

1. **仍然是按需**。正文只在模型显式调用时进入上下文，和 `read_file` 同一条纪律；
   它省掉的是"猜哪个 SKILL.md 值得读"的那几轮，不是"读正文"这个动作。
2. **加载即激活**。结果里的 `path` 被 §3.10 的捕获认成 `skill_context`（和 `read_file` 走
   同一本账），于是该技能声明的 `allowed-tools` 同时生效——否则模型拿着 SOP 却被策略挡住。
3. **两段式，但第二阶段可以是空的**。默认只有本地 BM25（中文按二元组，不依赖分词器与外部服务）；
   `embedding` 与 `rerank` 要显式配地址才开，且**失败只降级不失败**：向量通道挂了退回 BM25，
   精排挂了退回融合顺序，两种情况都在 `retrieval.warnings` 里写明。
4. **预算有界**。单个技能正文 `max_body_chars`、一次加载总量 `max_context_chars`；
   `body_truncated=true` 时正文里带截断标记并给出 `path`，模型可以再 `read_file` 读后半段。

**它不负责"确认技能存在"。** 那是索引的活：索引已经给了名字、截断描述、所需密钥和 `dir=`。
曾经还有一个只返回元数据的 `describe_skill`（allowed-tools / argument-hint / license / category），
在 `search_and_load_skills` 落地之后被删掉了：
那些字段全是**策略与用户侧**信息（工具被挡由中间件返回可恢复错误，`argument-hint` 是 slash
的用户用法），模型拿到也改变不了任何动作；而它每个请求都要带一份 tool schema，
并且与索引、与检索结果三处重复描述同一个技能。详见 §3.5 末尾。

#### 3.5.2 两个块的分工：`<available_skills>` 与 `<active_skills>`

技能在模型眼里只有两个载体，职责不重叠：

| 块 | 在哪 | 内容 | 什么时候有 |
|---|---|---|---|
| `<available_skills>` | **静态**系统提示词尾部（`skills/prompt.py`） | 全量有效启用技能，每行 `name [category]: 截断描述 [\| 需密钥]`；根目录与命名约定写在块前面 | 至少有一个技能 |
| `<active_skills>` | `<durable_context_data>` 里的运行时投影（`durable_context_middleware._render_skills`） | 已激活技能的 `name: 描述 \| dir=`，**外加核心 SOP（正文）** | 台账 `skill_context` 非空 |

`<active_skills>` 的四条实现约束：

1. **正文不进 state**。台账只记 `{name, path, description}`；正文每次投影时按 `path`
   **现读技能树**（`loader.read_skill_body`），受 `skills.active_context.max_body_chars` /
   `max_context_chars` 约束。把正文写进 state 会让每个 checkpoint 都拖着一份 SOP。
2. **正文还在消息里就不重复写**（`skip_bodies_still_in_context`，默认开）。工具结果的 JSON
   里本来就有那份正文；这段真正的价值是**上下文被压缩之后**——消息没了、台账还在，
   它就成了技能正文唯一的副本。关掉这一项等于每轮双份 token。
3. **按当前用户重算**：投影走 `SkillRegistry.visible()`，用户自己关掉的技能不再补正文
   （名字那一行留着，那是已经发生过的事实）。
4. **读不出来只丢正文**：技能文件被删、坏了，或注册表整体不可用，都只退化成一行名字，
   不让整轮模型调用失败。

**为什么不把这些写进系统提示词。** 系统提示词是静态的：`<active_skills>` 每一轮都可能变，
写进去等于每轮重写前缀、打穿前缀缓存（M6 §4.4）。`<durable_context_data>` 是同一份数据的
动态投影，模型每轮都看得到，语义上没有区别。

**提示词里只写"技能放在哪"和"你在看什么"，不写"什么时候去读"。** 三个理由：

1. 两个标签都叫 skills，光凭标签名区分"目录"和"已加载正文"不可靠：模型一旦把索引那行当成
   正文，就会拿 120 字描述硬猜（错答案），或者去重读已经加载过的技能（白烧一轮）。所以索引
   前面固定说一句"这是索引、**不含正文**、正文在 `<active_skills>` 里"（`SKILL_INDEX_PREAMBLE`）。
2. 索引行里不放 `dir=`：几十行就是几十份重复前缀。**根目录**和两条命名约定
   （目录名即技能名、SOP 固定 `SKILL.md`）在块前面说一次即可（`SKILL_ROOT_CONVENTION`），
   路径变成 `{root}/<category>/<name>/SKILL.md`，可推。
3. 两段都是**静态**的：跟着索引一起进 LRU 缓存，不随每轮变化打穿前缀缓存。

**为什么不写一段 `<skill_routing_protocol>`。** 曾经写过一版"什么时候检索、什么时候不要检索"
的协议，后来删掉了：它和 `search_and_load_skills` 的 tool description 说的是同一件事，
两处措辞一旦漂移就是两套契约（`task` 的 design 原则同款）。
"空结果别再换措辞重试"这类硬约束留在**工具结果**里说——那是模型真正会读到它的时刻。
路由判断的措辞只维护在 `search_and_load_skills` 的 docstring（WHEN TO USE / WHEN NOT TO USE）。

两者的边界可以一句话概括：**格式说明只有一份**（跟着索引缓存），**路由策略也只有一份**
（tool description）。缺了前者，模型看不懂两个块的区别；重复了后者，就是两套契约。

#### 索引长什么样

```python
# agentkit/skills/prompt.py
SKILL_INDEX_HEADER, SKILL_INDEX_FOOTER = "<available_skills>", "</available_skills>"


def render_skill_index(skills: Sequence[Skill], *, max_description_chars: int) -> str:
    """技能为 0 时返回空串：整个块不出现（不要留一个空标签给模型猜）。

    每行: - <name> [<category>]: <描述截断> [| 需密钥 A, B]
          目录名与 name 不一致时才退化成 `| dir=<容器路径>`
    排序: (category, name) —— 稳定的顺序，加技能只影响它自己那一行。
    """
    if not skills:
        return ""
    lines = [SKILL_INDEX_HEADER]
    for skill in sorted(skills, key=lambda s: (s.category.value, s.name)):
        description = _truncate(skill.description, max_description_chars)
        secrets = (
            " | 需密钥 " + ", ".join(item.name for item in skill.required_secrets)
            if skill.required_secrets else ""
        )
        name_suffix, tail = _entry_marker(skill)
        lines.append(
            f"- {html.escape(skill.name)}{name_suffix}: {html.escape(description)}{secrets}{tail}"
        )
    lines.append(SKILL_INDEX_FOOTER)
    return "\n".join(lines)
```

渲染出来是这样（技能少的时候它就是提示词里唯一变动的部分）。**路径不在行里**：
根目录和两条命名约定写在块前面，一次说清，路径可推：

```text
技能根目录 `/mnt/skills`：每个技能一个目录，**目录名就是技能名**，SOP 固定是该目录下的
`SKILL.md`，即 `/mnt/skills/<category>/<技能名>/SKILL.md`（category 是 public 或 custom……
同名时 custom 覆盖 public）。
下面是技能索引 `<available_skills>`：每个已启用技能一行，只有名字、类别和截断描述
（需要密钥的会标出来），**不含正文**。已加载技能的名字与正文在持久上下文的 `<active_skills>` 里。

<available_skills>
- csv-tools [custom]: 处理 CSV，按列聚合 | 需密钥 CSV_API_KEY
- pdf-processing [public]: Handle PDF documents efficiently
</available_skills>
```

**为什么把 `dir=` 从每一行挪到块前面**：几十个技能就是几十份重复的路径前缀，而路径本来就
是可推的（根目录 + 目录名即技能名 + 固定 `SKILL.md`）。代价是模型必须做一次字符串拼装——
这比每行多 40 个 token 便宜。**唯一例外**：目录名和 frontmatter 的 `name` 不一致的技能
（`load_skills` 会 warning 并以 frontmatter 为准），那种行退化成 `| dir=<容器路径>`，
否则模型会按约定推到一个不存在的路径，`read_file` 撞 404 白烧一轮。

**索引的五条规则**：

1. **只列"对当前用户有效启用"、且在 agent 白名单里的技能**（`effective_enabled`，§2.4），
   而且**从 live registry 现算、不缓存**：技能被全局禁用、被删除，或**被这个用户自己关掉**之后，
   必须立刻从他的索引里消失（fail-closed——宁可模型"不知道有这个技能"，
   也不要让它去读一个已经不存在的路径，或者去激活一个用户明确关掉的技能）。
2. **排序稳定**（`category, name`）。按扫描顺序或 mtime 排，会让"加一个技能"改变后面所有行的
   位置，前缀缓存从第一处变动起全部失效。
3. **只截断描述**（`skills.index_description_max_chars`，默认 120），其余字段**根本不进索引**：
   `allowed-tools` / `argument-hint` / license 都不进——前两个是策略与 slash 的用法，
   license 对模型没有意义。模型需要的是"有没有、要不要密钥、目录在哪"，以及正文。
4. **`dir=` 是容器路径**（`/mnt/skills/{public|custom}/<relative>`），不是宿主路径——
   模型据此一步 `read_file`，不必先检索。
5. **需要密钥的技能必须标出来**。不标，模型会去激活一个注定失败的技能，用户看到的是"技能坏了"。

name / description 都 `html.escape`：技能描述里可以塞 `<system-reminder>` 伪造框架标签
（M6 清的是用户输入，系统提示词由我们自己拼，但技能作者写的内容同样不可信）。

#### 为什么删掉 L1 元数据工具（原 `describe_skill`）

早期设计有三级：索引（L0）→ 元数据（L1，`describe_skill`）→ 正文（L2）。L1 返回完整
description、`allowed-tools`、`argument-hint`、`required-secrets`、license、容器路径。
`search_and_load_skills` 落地之后它被删掉了，理由三条：

1. **独有字段没有一个能改变模型的动作。** `allowed-tools` 是**策略输入**（工具被挡时中间件
   返回可恢复错误并提示换工具，模型不需要事先知道）；`argument-hint` 是 slash 的用户用法；
   license / category 对模型无意义。真正影响"会不会白激活"的 `required-secrets` 已经在索引里。
2. **信息三处重复。** 索引给名字 + 截断描述 + 密钥 + `dir=`，检索给完整描述 + 正文 + `path`，
   L1 在中间再说一遍同一批字段——三处措辞漂移就是三套契约。
3. **每个请求都要带一份 tool schema。** 工具描述本身就是上下文预算，而它的 WHEN/WHEN NOT
   与检索工具高度重叠。

**什么情况下该把它加回来**：如果以后工具策略演进成"不同技能挂不同工具集、选错技能会做不成事"，
那 `allowed-tools` 就变成了**决策依据**——那时应该把它放进**索引那一行**，而不是再造一个
元数据工具。

#### 为什么删掉 `skills.deferred_discovery` 开关

1. **旧的两个模式差别比看上去小。** 所谓"全量注入"也只放元数据，正文本来就要 `read_file`；
   两者真正的差别只是"元数据什么时候进上下文"，而索引那一行已经覆盖了。
   留开关等于留两套渲染、两套提示词契约、两套测试。
2. **元数据无界增长。** 全量形态下每加一个技能就多出 allowed-tools / argument-hint / license /
   路径，把 M6 §4.4 要求的"静态系统提示词"反复打穿。索引是它的**有界**版本：每技能一行，
   描述截断。
3. **三处目录必须一致**：前端斜杠菜单、`GET /api/skills`、模型可见索引。少一种模式就少一处漂移。

#### 代码：`slash.py`

```python
# agentkit/skills/slash.py（与 contracts/slash_skill_contract.json 双向钉死）
RESERVED_SLASH_SKILL_NAMES = frozenset({
    "agent", "bootstrap", "goal", "help", "memory", "models", "new", "status",
})
_SLASH_SKILL_RE = re.compile(r"^/([a-z0-9]+(?:-[a-z0-9]+)*)(?:\s+|$)")


@dataclass(frozen=True, slots=True)
class SlashSkillMatch:
    name: str
    argument: str          # 斜杠命令之后的剩余文本（可能为空）


def parse_slash_skill(text: str | None) -> SlashSkillMatch | None:
    """只认**行首**、`/` 与 name 之后必须是空白或结束。返回 None 表示"这不是斜杠激活"。"""
    if not text:
        return None
    match = _SLASH_SKILL_RE.match(text)          # ^/ 已经拒绝了前导空白
    if match is None:
        return None
    name = match.group(1)
    if name in RESERVED_SLASH_SKILL_NAMES:       # 频道命令优先，不能当技能名
        return None
    return SlashSkillMatch(name=name, argument=text[match.end():].strip())
```

- **必须在 `get_original_user_content_text`（M6 §4.2）之后解析**：清洗、上传包装、上下文提醒
  之后 `content` 已经不是 `/skill ...` 的形状了。
- **调用方还要再查两件事**：技能存在且 `enabled`、且在该 agent 的 `available_skills` 白名单里。
  解析器只负责语法——查库、查白名单是中间件的事（§3.6）。

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

#### 代码：`tool_policy.py` 与策略中间件

```python
# agentkit/skills/tool_policy.py
ALWAYS_AVAILABLE_BUILTIN_TOOL_NAMES = frozenset(
    {"read_file", "tool_search", "search_and_load_skills"}
)


def compute_allowed_tools(
    *, baseline: AbstractSet[str], activated: Sequence[Skill], slash_skill: Skill | None
) -> frozenset[str]:
    """本次模型调用可见的**业务**工具集合；框架工具由调用方并上（§3.8）。

    规则一：没有任何真正激活的技能 → 基线不变（被动启用不收紧）。
    规则二：slash 声明了 allowed-tools → 只认它（支配；模型再读别的 SKILL.md 不能扩大）。
    并集：其余情况取"所有**声明了** allowed-tools 的激活技能的并集"；
          未声明者贡献空集，而不是"放行全部"。
    """
    if slash_skill is not None and slash_skill.allowed_tools is not None:
        return frozenset(slash_skill.allowed_tools)
    declared = [skill.allowed_tools for skill in activated if skill.allowed_tools is not None]
    if not declared:
        return frozenset(baseline)
    return frozenset().union(*declared)
```


```python
# agentkit/agents/middlewares/skill_tool_policy_middleware.py
class SkillToolPolicyMiddleware(AgentMiddleware):
    """把"当前激活的技能"翻译成"本次模型调用能看见哪些工具"。

    **这是行为约束，不是安全边界**（§3.7）：模型可以用 `bash cat` 读任意 SKILL.md，
    自主捕获也有容量上限。真正的边界是沙箱与鉴权。
    """

    def __init__(self, *, registry: SkillRegistry, owner_token: str, baseline: AbstractSet[str]) -> None: ...

    async def awrap_model_call(self, request: ModelRequest, handler: Callable) -> ModelResponse:
        activated, slash_skill = self._resolve(request)      # 现场解析，不信 context 里存的列表
        allowed = compute_allowed_tools(
            baseline=self._baseline, activated=activated, slash_skill=slash_skill
        )
        _write_decision(request.runtime, allowed, owner_token=self._owner_token)   # secret_context
        return await handler(request.override(
            tools=[tool for tool in request.tools if _is_visible(tool, allowed)]
        ))

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Callable) -> ToolMessage:
        decision = _read_decision(request.runtime, owner_token=self._owner_token)  # 对不上 → None
        allowed = decision or self._recompute(request)      # 畸形 / 过期 / 外来决策一律作废
        if not _is_visible_name(request.tool_call["name"], allowed):
            return _policy_error_tool_message(request.tool_call)   # recoverable_by_model=True
        return await handler(request)
```

- **`baseline` 是"配置工具 + 内置工具"的名字集合**（M2 `collect_tools` 的产物），
  框架工具（`read_file` / `tool_search` / `search_and_load_skills`）永远并上去。
- **决策写进 `secret_context` 且带 owner token**：同一轮的 tool_call 可以复用它，
  但伪造的、过期的、别人写的都会被拒（§3.6 的 token 机制）。
- **`awrap_tool_call` 短路时返回一条错误 ToolMessage，而不是抛异常**：模型看到的是
  "这个工具在当前技能下不可用，换个工具"，而不是整轮失败。

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
    "read_file",
    "tool_search",
    "search_and_load_skills",   # 和 read_file 同级：都只是"把某个 SKILL.md 读进上下文"
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
   解析出来的技能如果**对当前用户不是有效启用**（`effective_enabled`，§2.4），按未激活处理——
   用户关掉了就是关掉了，模型自己读到 `SKILL.md` 也不改变这一点。
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

M6 已有通道。M7 把捕获补全：两条来源，都抽出 `{name, path, description}` 写入
`ThreadState.skill_context`——描述截断（M1 的 `_SKILL_DESCRIPTION_MAX_CHARS`），
**不要把正文放进 state**，它会让 checkpoint 膨胀：

1. `read_file` / `read` / `view` / `cat`（配置 `summarization.skill_file_read_tool_names`）
   的结果，路径在 `container_path` 下且 basename 是 `SKILL.md`——路径在**调用参数**里；
2. `search_and_load_skills` 的结果 JSON——路径在**结果**里（`skills[].path`），照样过
   `_container_skill_path` 校验。加载即激活，所以它和 read_file 走同一本账。

投影时（`_render_skills`）再从技能树现读正文，装进 `<active_skills>`，受
`skills.active_context` 的预算约束，并且**正文还在消息里就不重复写**（§3.5.2）。

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

#### 代码：`cache.py`（懒加载 + single-flight + 会话退役）

```python
# agentkit/mcp/cache.py
_cache_lock = threading.Condition()          # 纯线程锁：加载跑在 worker 线程里（§2.2）
_cache: CacheEntry | None = None
_loading = False


def get_cached_mcp_tools() -> list[BaseTool]:
    """第一次用时加载；内容签名变了重新加载。并发请求共用一次加载（§2.3）。"""
    global _cache, _loading
    while True:
        signature = current_signature()          # (路径, mtime, size, sha256)；FileNotFoundError → "未配置"
        with _cache_lock:
            if _cache is not None and _cache.signature == signature:
                return _cache.tools
            if _loading:
                _cache_lock.wait()              # 别人在加载：等它，醒来重算签名
                continue
            _loading = True
        try:
            tools = _load_tools_from_scratch(signature)        # 建连、发现工具、包装
        finally:
            with _cache_lock:
                _loading = False
                _cache_lock.notify_all()
        with _cache_lock:
            if current_signature() != signature:
                # 加载期间配置又变了 → 丢掉这份结果，并**走和正常失效一样的会话退役**
                _retire_sessions(_cache.tools if _cache else ())
                _cache = None
                continue                     # 重来一轮；签名稳定后必然收敛
            _cache = CacheEntry(signature=signature, tools=tools)
            return tools
```

- **签名不匹配就重来，不"用旧的凑一次"**：旧工具绑的是旧配置（可能已经指向另一个服务器），
  用一次就可能把请求发到错的端点。
- **加载中途签名变化必须退役会话**（§2.3）：放弃的加载已经把连接建好了，
  不清会把它们留在池子里，下一次构建工具时用到旧会话。
- **`FileNotFoundError` 在热路径上当"未配置"**：显式路径中途消失不该把每次
  `get_cached_mcp_tools()` 打成 500；真正 `from_file()` 的调用者仍然要大声失败。

#### 代码：`tools.py`（前缀、打标、名字校验）

```python
# agentkit/mcp/tools.py
MCP_TOOL_META_KEY = "deerflow_mcp"
_MCP_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def qualify_tool_name(server: str, original_name: str, *, prefix_enabled: bool) -> str:
    """默认加 `<server>_` 前缀：两个服务器都提供 `search` 很常见（§5.1）。"""
    qualified = f"{server}_{original_name}" if prefix_enabled else original_name
    if not _MCP_TOOL_NAME_RE.match(qualified):
        raise McpToolNameError(f"unsafe tool name from server {server!r}: {original_name!r}")
    return qualified


def tag_mcp_tool(tool: BaseTool, *, server: str, original_name: str) -> BaseTool:
    """源路由 / 延迟提升 / 结果消毒都只认这个标，**不认名字前缀**（前缀可以关）。"""
    tool.metadata = {
        **(tool.metadata or {}),
        MCP_TOOL_META_KEY: {"server": server, "original_name": original_name},
    }
    return tool
```

- **名字校验在加载边界做**，不是调用时：延迟工具不会被 provider 的 function-calling 校验扫到，
  而它的名字会进系统提示词——换行或尖括号足以伪造框架结构。
- `server_name` 自己也要是安全标识符（否则前缀会把脏字符带进工具名），
  这条校验和 `McpServerWrite` 的键正则（§2.4）是同一条。
- **`original_name` 一定要记**：结果消毒、审计、去重都要用它，而不是从前缀反推。

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

#### 代码：`session_pool.py`（owner task 与精确驱逐）

```python
# agentkit/mcp/session_pool.py
@dataclass
class _PooledSession:
    key: tuple[str, str]            # (server_name, f"{user_id}:{thread_id}")
    session: ClientSession
    close_event: asyncio.Event
    owner: asyncio.Task             # ★ 唯一被允许 __aexit__ 的那个 task


async def _run_session(self, key: PoolKey, params: dict, ready: asyncio.Future) -> None:
    """owner：enter → 把 session 交出去 → 等 close event → 自己退出。

    ClientSession 建立在 anyio task group 上，__aenter__ / __aexit__ 必须在同一个 task。
    所有关闭路径只 signal close_event，**绝不从别的 task __aexit__**（§5.3）。"""
    try:
        async with self._connect(params) as session:
            if not ready.done():
                ready.set_result(session)            # 只有 owner 写池子：调用方拿不到半成品
            await self._close_events[key].wait()
    except BaseException as exc:                     # 建连失败也要让等待者醒过来
        if not ready.done():
            ready.set_exception(exc)
        raise
    finally:
        self._evict_if_current(key, session)


async def acquire(self, server: str, *, user_id: str | None, thread_id: str) -> ClientSession:
    key = (server, f"{user_id}:{thread_id}")
    pooled = self._sessions.get(key)
    if pooled is not None and not pooled.owner.done():
        self._touch(key)                             # LRU：最近使用
        return pooled.session
    await self._make_room()                          # 池满：按 LRU 驱逐（signal close event）
    ready: asyncio.Future[ClientSession] = asyncio.get_running_loop().create_future()
    owner = asyncio.create_task(self._run_session(key, params, ready), name=f"mcp:{key[0]}")
    self._sessions[key] = _PooledSession(key=key, session=None, close_event=..., owner=owner)
    return await asyncio.wait_for(ready, timeout=self._session_init_timeout)


def _evict_on_disconnect(self, key: PoolKey, failed_session: ClientSession) -> None:
    """**只在注册表里的条目仍然是失败的那个对象时**才驱逐（§5.3）。"""
    pooled = self._sessions.get(key)
    if pooled is None or pooled.session is not failed_session:
        return                                       # 晚到的旧错误不能干掉刚建的替代会话
    self._sessions.pop(key, None)
    pooled.close_event.set()                         # 由 owner 自己退出，不从别的 task __aexit__
```

- **断连只驱逐一个 key**：明确的 `Connection closed`（MCP SDK）或 AnyIO closed-stream 才驱逐；
  普通 `isError=true`、协议超时、拦截器失败**都不驱逐**健康会话。
- **绝不自动重放**：失败那次照常向上报错。重放一个有副作用的工具，比报错糟得多。
- **池满用 LRU**：session 是有状态子进程（stdio），上限就是进程数上限；
  `_touch` 记在 acquire 上，不是记在调用上。

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

#### 代码：`interceptors.py`（四层优先级与 fail-closed）

```python
# agentkit/mcp/interceptors.py
def build_interceptors(server: McpServerConfig, *, config: ExtensionsConfig) -> list[Interceptor]:
    """**先注册的在外层，后注册的更靠近传输层、赢得最终 header**（§5.5）。"""
    chain: list[Interceptor] = []
    if server.oauth is not None:
        chain.append(OAuthInterceptor(server.oauth))                 # ① 自动刷新
    if server.user_auth is not None:
        chain.append(UserScopedAuthInterceptor(server.user_auth,    # ② 按用户映射
                                               on_missing=server.user_auth.on_missing))
    if server.headers_from_context:
        chain.append(ContextHeadersInterceptor(server.headers_from_context))   # ③ 按请求
    chain.extend(config.mcp_interceptors)                            # ④ 运维文件，最靠近传输层
    return chain
```


```python
# agentkit/mcp/context_headers.py
class ContextHeadersInterceptor:
    """把 `context.secrets` 的值映射成 header。映射里存的是**名字**，不是值（§5.5）。"""

    def __init__(self, mapping: Mapping[str, str], *, on_missing: str = "fail") -> None:
        self._mapping, self._on_missing = dict(mapping), on_missing

    async def __call__(self, request: ToolCallRequest, handler: Callable) -> Any:
        secrets = extract_request_secrets(request.runtime.context)   # ★ 从 ToolRuntime 读（§5.4）
        overrides: dict[str, str] = {}
        missing: list[str] = []
        for header, secret_name in self._mapping.items():
            value = secrets.get(secret_name)
            if not value:
                missing.append(secret_name)
                continue
            reason = illegal_header_value_reason(str(value))
            if reason is not None:
                raise ToolException(f"invalid credential for {secret_name}: {reason}")   # 不回显值
            overrides[header] = str(value)
        if missing and self._on_missing != "passthrough":
            raise ToolException(f"missing required secret(s): {', '.join(sorted(missing))}")
        return await handler(request.override(headers=apply_header_overrides(request.headers, overrides)))
```

- **fail-closed 是默认**：映射找不到当前用户、或解析出空值，就抛 `ToolException` 并**只点名缺失的键**。
  静默回落到发现凭据 = 一个租户用另一个凭据的权限在跑。
- **stdio 服务器上这三层跳过并 warning**：stdio 用 `env`，没有 header 可说（§5.5 的表）。
- `headers_from_context` 的值在 GET 时不打码（它本来就只是**名字**），PUT 整块替换。

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

#### 代码：`headers.py`（大小写与非法值）

```python
# agentkit/mcp/headers.py
def illegal_header_value_reason(value: str) -> str | None:
    """返回"为什么非法"的短语；**绝不把 value 编进返回值**（§5.6）。

    这个字符串会经 ToolErrorHandlingMiddleware 进模型可见消息、checkpoint 和 trace。"""
    if any(ord(ch) > 0x7E or ord(ch) < 0x20 for ch in value):
        return "header value contains non-ASCII or control characters"
    if value != value.strip(" \t"):
        return "header value has leading or trailing whitespace"
    return None


def apply_header_overrides(base: Mapping[str, str], override: Mapping[str, str]) -> dict[str, str]:
    """大小写不敏感地覆盖：丢掉只有大小写不同的键，沿用 **base 已经在用的拼写**（§5.6）。"""
    merged = dict(base)
    lowered = {key.lower(): key for key in merged}
    for key, value in override.items():
        existing = lowered.get(key.lower())
        if existing is not None and existing != key:
            del merged[existing]                     # 否则两个键都到达 httpx，服务端单值访问器取到静态那个
            lowered.pop(key.lower(), None)
        merged[key] = value
        lowered[key.lower()] = key
    return merged
```

- **检查对象是渲染后的完整值**（`<token_type> <access_token>`），不是分开检查两个字段：
  换行可能出现在拼接的接缝上。
- **非法值无视 `on_missing: passthrough`**：那是"缺值"的开关，不是"脏值也能送"的开关。
- 三个注入点（OAuth 管理器、`user_auth`、`context_headers`）和发现路径 `build_server_params`
  全部走这一个函数，不许各写一份"随便检查一下"。

### 5.7 stdio 启动参数校验

stdio 参数在 M7 只有一个来源：**运维文件**（运维写的，但可能是从别处复制来的）。
后续阶段加上注册 API 之后，会多一个**完全不可信**的来源——所以这条校验挂在
"准备 spawn 之前"，不是挂在某个路由上，两个来源共用同一段代码。

**M7 只做第 1 条：** `command` 必须是允许列表里的裸可执行文件名
（默认 `{npx, uvx}`，`DEER_FLOW_MCP_STDIO_COMMAND_ALLOWLIST` 可扩），
拒绝路径分隔符、空白、shell 元字符。运维文件里写了非法 command → **跳过那一台服务器**
并记原因，不让整个 `make_lead_agent` 失败。

后两条（args 任意执行 flag、`PYTHONPATH` 这类无条件执行调用方代码的 env）
正确实现非常微妙（`npx` 在包名处停止解析自己的 flag，选项区边界依赖每个
flag 的元数）。**在把 MCP 注册开放给非运维角色之前不值得投入。**

> **这是纵深防御，不是信任边界。** `npx`/`uvx` 存在的意义就是拉取并执行远程包。
> 真正的边界是**管理员认证 + 网络可达性**。不要以为加了检查就可以把 MCP
> 注册开放给不可信管理员。

远程 transport 不跑这三项——它们不 spawn。

---

#### 代码：`client.py`（构建参数与 stdio 启动校验）

```python
# agentkit/mcp/client.py
def validate_stdio_command(command: str, *, allowlist: frozenset[str]) -> str | None:
    """返回拒绝原因，None = 通过。**这是纵深防御，不是信任边界**（§5.7）。"""
    if not command or command != command.strip():
        return "command must be a bare executable name"
    if any(sep in command for sep in ("/", "\\")) or any(ch.isspace() for ch in command):
        return "command must not contain path separators or whitespace"
    if command not in allowlist:
        return f"command must be one of: {', '.join(sorted(allowlist))}"
    return None


def build_server_params(server: McpServerConfig, *, user_id: str | None, thread_id: str) -> dict:
    """把配置翻译成适配器的 server params。凭据在这一步只做**渲染**，合法性由 headers.py 判。"""
    if server.type == "stdio":
        return {"transport": "stdio", "command": server.command, "args": list(server.args),
                "env": build_stdio_env(server, user_id=user_id), "cwd": workspace_dir(thread_id),
                "session_init_timeout": server.session_init_timeout}
    headers = apply_header_overrides(server.headers, {})             # 静态头先过一遍大小写检查
    return {"transport": server.type, "url": server.url, "headers": headers,
            "tool_call_timeout": server.tool_call_timeout}
```

- **API 注册/启用 stdio 时只做第 1 条校验**（`command` 白名单，默认 `{npx, uvx}`）；
  args 的执行型 flag、`PYTHONPATH` 这类 env 不做——正确实现非常微妙，在把 MCP 注册开放给
  非运维角色之前不值得投入（§5.7）。
- **远程 transport 不跑这三项**：它们不 spawn。
- 拒绝时的 `detail` **不含命令行**（它可能带 token），只说是哪一条规则。

## 6. 工具检索（D10，按需）

**这一节是可选的：没上百个工具就不需要它。** 但它的形状现在就要定，因为它碰的是
`ThreadState`（`promoted` 通道）与中间件组装顺序——两者后加都要动别人。

### 6.1 什么时候启用


| 触发条件                                          | 谁决定        | 效果                     |
| --------------------------------------------- | ---------- | ---------------------- |
| `tool_search.enabled: true`                    | 运维显式开      | 强制启用                   |
| MCP **业务**工具数 ≥ `tool_search.auto_threshold`（默认 40） | 组装时自动判断    | 到量自动启用                 |
| 两个都不满足                                        | ——         | **本节的所有中间件都不注册**，行为退回 M2 的全量工具列表 |


**为什么按需，而不是一直开**：全量 schema 在 100 个工具时是几万 token；更糟的是模型在
一百个候选里选对的准确率会显著下降。而 MCP 服务器是用户配的，数量不可控——
所以这是"到量才付代价"的开关，不是默认架构。

`tool_search.*` 是 **STARTUP_ONLY**：中间件在 agent 组装时就挂上了，
热改配置不能让已经编译好的图多出一层。

### 6.2 三个对象，别混

| 对象                    | 是什么                                  | 存在哪                          | 生命周期                       |
| --------------------- | ------------------------------------ | ---------------------------- | -------------------------- |
| `DeferredCatalog`     | 组装时冻结的"可延迟工具"快照：名字、描述、schema、hash    | 进程内（跟编译好的图一起缓存）              | 配置签名变化 → 重建（§2.3）           |
| `ThreadState.promoted` | 这条 thread 已经提升过的工具**名字**             | checkpoint（M1 的 state 通道）    | **跨 run 保留**：上一轮找过的工具，下一轮不必从零再找 |
| `request.tools`       | 本次模型调用可见的 schema                      | 请求级                          | **每次模型调用重算**                |


可见性公式（三处输入，缺一不可）：

```
本次可见 = 基线工具（配置 + 内置）
         ∪ catalog.resolve(promoted)          # 提升过的 MCP 工具（幽灵名字自动消失）
         ∩ 技能策略允许集合                      # §3.8：发现 ≠ 可用
         ∪ ALWAYS_AVAILABLE_BUILTIN_TOOL_NAMES  # read_file / tool_search / search_and_load_skills
```

**`promoted` 存名字，不存 schema。** schema 永远从**当前** catalog 取：
配置改了、服务器下线了，旧名字自然解析不出东西，不需要迁移 checkpoint。

### 6.3 一次 run 里的完整步骤

```
① 组装 agent（M1）
   ├─ 数一遍带 deerflow_mcp 标的工具 → 决定是否启用本节
   ├─ 建 DeferredCatalog（名字 + 描述 + schema_hash + catalog_hash）
   └─ 把这些工具从基线里**摘出去**：它们不进默认的 request.tools

② 渲染系统提示词
   └─ <available-deferred-tools>：**只有名字**（一行一个），HTML-escape

③ 第一轮模型调用：request.tools = 基线（deferred 的一律不可见）

④ 模型需要某个能力 → 调 tool_search(query="...")

⑤ tool_search 内部
   ├─ 解析 query（非法 regex → 字面匹配）
   ├─ 在 catalog 的名字 + 描述上匹配，**排除已提升的**
   ├─ 截断到 max_results（默认 5）
   ├─ 返回结果：命中的名字 + 描述 + 一句"下一轮会出现在你的工具里"
   └─ 命中的名字写进 ThreadState.promoted

⑥ 下一轮模型调用
   ├─ McpRoutingMiddleware（可选，在外层）先按最新真实用户消息自动提升一批
   └─ DeferredToolFilterMiddleware 把 promoted 解析成 schema，**追加**进 request.tools

⑦ 模型调用被提升的工具
   └─ 走普通 tool_call 路径 → 过 SkillToolPolicy（§3.8：提升不等于可用）

⑧ run 结束
   └─ promoted 随 checkpoint 落盘；同一 thread 的下一次 run 继续可见
```

**第 ⑤ 步和第 ⑥ 步之间必须隔一次模型调用。** 工具是**同步返回**的：本轮它提升的工具
不可能出现在本轮的 `request.tools` 里（模型已经拿到工具列表并正在生成这一轮的输出）。
所以 `tool_search` 的返回消息**必须**明说"下一轮可用"——不说，模型会以为搜索失败，
然后换个关键词再搜一遍，把 token 花在重试上。

### 6.4 组装顺序（M6 已经留好的位置）

外层 → 内层（越晚注册越靠近模型调用）：


| 顺序 | 中间件                          | 为什么在这个位置                              |
| -- | ---------------------------- | ------------------------------------- |
| 1  | `McpRoutingMiddleware`       | 它要**改 state**（promoted）；必须在 filter 之前，否则这一轮的自动提升白做 |
| 2  | `DeferredToolFilterMiddleware` | 读 promoted → 追加 schema                 |
| 3  | `SkillActivationMiddleware`   | 决定本轮"哪条技能被显式激活"                       |
| 4  | `SkillToolPolicyMiddleware`   | 过滤 schema、管 tool_call；**必须在 filter 之后** |
| 5  | `DurableContextMiddleware`    | 捕获技能引用、durable 投影                      |
| 6  | coalescing / summarization   | M6 的其余部分                              |


- **Promotion 的审计事件（可选）要在 `SkillToolPolicy` 之外**（更外的 `wrap_tool`）：
  否则被策略拒绝的工具名也会进审计账，"提升过"和"能用"就混了。
- **装配顺序要有测试**（§9 的 `test_deferred_assembly_order.py`）：它是一串列表，
  谁顺手 `insert(0, ...)` 都能跑，但行为会静默变化。

### 6.5 代码：catalog

```python
# agentkit/mcp/deferred.py
@dataclass(frozen=True, slots=True)
class DeferredEntry:
    name: str                  # 已经带过 server 前缀的最终名字
    description: str
    tool: BaseTool             # 真正执行时用的对象（已包装会话）
    schema_hash: str           # 规范化 schema 的 sha256[:16]


@dataclass(frozen=True, slots=True)
class DeferredCatalog:
    entries: tuple[DeferredEntry, ...]
    catalog_hash: str          # 所有 (name, schema_hash) 排序后再 sha256[:16]

    def resolve(self, names: AbstractSet[str]) -> list[BaseTool]:
        """按**当前** catalog 解析。幽灵名字（配置改了 / 服务器下线）直接忽略，不报错。"""
        wanted = set(names)
        return [entry.tool for entry in self.entries if entry.name in wanted]

    def names_block(self) -> str:
        """<available-deferred-tools>：**只有名字**，按名字排序，HTML-escape。

        描述不进这里：那是 token 预算的关键，而且 tool_search 的结果里会给。"""
        if not self.entries:
            return ""
        rows = "".join(f"<tool>{html.escape(entry.name)}</tool>"
                       for entry in sorted(self.entries, key=lambda e: e.name))
        return f"<available-deferred-tools>{rows}</available-deferred-tools>"

    def match(self, text: str, *, limit: int, exclude: AbstractSet[str]) -> list[str]:
        """自由文本匹配（名字权重高于描述）。非法 regex 退化成字面子串。"""
        pattern = _compile_or_literal(text)
        scored: list[tuple[int, str]] = []
        for entry in self.entries:
            if entry.name in exclude:
                continue
            if pattern.search(entry.name):
                scored.append((0, entry.name))               # 名字命中优先
            elif pattern.search(entry.description):
                scored.append((1, entry.name))
        scored.sort()                                        # (权重, 名字) 稳定排序
        return [name for _weight, name in scored[:limit]]
```


```python
# agentkit/config/tool_search_config.py
class ToolSearchConfig(BaseModel):
    enabled: bool = False                    # 显式开启
    auto_threshold: int = Field(default=40, ge=0)   # MCP 业务工具数达到就自动开
    max_results: int = Field(default=5, ge=1, le=20)  # 单次提升上限
    auto_promote_top_k: int = Field(default=3, ge=0)  # McpRoutingMiddleware 的自动提升上限
```

### 6.6 代码：`tool_search` 工具

```python
# agentkit/tools/builtins/tool_search.py
class ToolSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=200,
                       description="用自然语言描述你要完成的事，例如 'create a github issue'")
    limit: int = Field(default=5, ge=1, le=20)


def build_tool_search_tool(catalog: DeferredCatalog) -> StructuredTool:
    async def _search(query: str, limit: int = 5) -> dict:
        promoted_now = _current_promoted()                    # 从 state 读（见下）
        hits = catalog.match(query, limit=min(limit, MAX_SEARCH_RESULTS), exclude=promoted_now)
        if not hits:
            return {"promoted": [], "message": "No matching tool. Try different keywords."}
        return {
            "promoted": hits,
            "message": (f"{len(hits)} tool(s) promoted; they will be available on your "
                        f"**next** model call."),
            "candidates": [{"name": e.name, "description": e.description}
                           for e in catalog.entries if e.name in set(hits)],
            "state_update": {"promoted": hits},               # ★ 落进 ThreadState.promoted
        }
    return StructuredTool.from_function(coroutine=_search, name="tool_search",
                                        description=TOOL_SEARCH_DESCRIPTION, args_schema=ToolSearchInput)
```


| 参数          | 类型    | 约束              | 说明                                                     |
| ----------- | ----- | --------------- | ------------------------------------------------------ |
| `query`     | `str` | 1–200 字符        | 自然语言，不做语法；非法 regex 退化成字面子串（不要 500）                     |
| `limit`     | `int` | 1–20，默认 5       | **还要与 `tool_search.max_results` 取小**：模型不能自己把上限抬高      |
| 返回 `promoted` | `list[str]` | 最多 `max_results` 条 | 已提升过的不会再出现（幂等：重复搜同一个词第二次返回空）                          |
| 返回 `message`  | `str` | ——              | **必须包含"下一轮可用"**，否则模型会重试搜索                              |


### 6.7 代码：`promoted` 通道与 reducer

```python
# agentkit/agents/state.py
PROMOTED_MAX = 50          # 单条 thread 最多记住多少个已提升的工具名


def reduce_promoted(left: Sequence[str] | None, right: Sequence[str] | None) -> list[str]:
    """**保序去重并集**：并发分支各提升一批时两边都保留，且顺序稳定（M1 的通道约定）。

    超过 PROMOTED_MAX 时丢**最早提升**的：最近提升的更可能还在用；
    截断只影响可见性，不影响已经写进对话历史的事实。"""
    merged = list(dict.fromkeys([*(left or ()), *(right or ())]))
    if len(merged) > PROMOTED_MAX:
        logger.warning("promoted names exceed %d; dropping oldest", PROMOTED_MAX)
        merged = merged[-PROMOTED_MAX:]
    return merged
```

- **为什么跨 run 保留**：用户第一次让它"建个 GitHub issue"，第二次说"再建一个"，
  不该重新搜一遍。代价是 checkpoint 里多几十个短字符串。
- **为什么存名字不存 schema**：schema 会随配置变；名字不会。两者混存就必须在配置变化时
  批量迁移 checkpoint。
- **reducer 必须是并集，不能是覆盖**：一次 run 里可能有多个并发分支（M8 的子代理）
  各自提升，覆盖会让先提升的那批消失。

### 6.8 代码：`DeferredToolFilterMiddleware`

```python
# agentkit/agents/middlewares/deferred_tool_filter_middleware.py
class DeferredToolFilterMiddleware(AgentMiddleware):
    """把 promoted 翻译成模型可见的 schema。**每次模型调用重算**（§6.2 的公式）。"""

    def __init__(self, *, catalog: DeferredCatalog, baseline_names: AbstractSet[str]) -> None:
        self._catalog, self._baseline_names = catalog, baseline_names

    async def awrap_model_call(self, request: ModelRequest, handler: Callable) -> ModelResponse:
        promoted = set(request.state.get("promoted", ()))
        extra = [tool for tool in self._catalog.resolve(promoted)      # 幽灵名字在这里消失
                 if tool.name not in self._baseline_names]              # 基线里已有的不重复加
        return await handler(request.override(tools=[*request.tools, *extra]))
```

- **`request.override(tools=...)` 是替换语义**，所以这里显式把 `request.tools` 带上再加：
  只传 `extra` 会把基线工具全删掉。
- **`resolve` 返回的是当前 catalog 里的对象**，不是历史快照：MCP 服务器重启后
  拿到的是新会话包出来的工具。
- **不缓存解析结果**：`promoted` 每轮可能变（模型还在搜），缓存只会让它晚一轮生效。

### 6.9 可选：`McpRoutingMiddleware`（自动提升）

```python
# agentkit/agents/middlewares/mcp_routing_middleware.py
class McpRoutingMiddleware(AgentMiddleware):
    """按**最新真实用户消息**自动提升明显匹配的工具。**只提升，不执行**（§6.3 第 ⑥ 步）。"""

    def __init__(self, *, catalog: DeferredCatalog, top_k: int = 3) -> None:
        self._catalog, self._top_k = catalog, top_k

    async def awrap_model_call(self, request: ModelRequest, handler: Callable) -> ModelResponse:
        text = latest_real_user_text(request.state)          # M6 的 is_real_user_message
        promoted = list(request.state.get("promoted", ()))
        if not text or self._top_k <= 0:
            return await handler(request)
        hits = self._catalog.match(text, limit=self._top_k, exclude=set(promoted))
        if not hits:
            return await handler(request)
        # ★ 每个 pass 只对**新**名字发一次事件（重复 pass 不发，避免账本被刷）
        emit_custom_event("middleware:tool_promotion", {"names": hits, "source": "routing_hint"})
        return await handler(request.override(state={**request.state, "promoted": [*promoted, *hits]}))
```

- **不用 LLM 判断"该不该提升"**：那等于每次模型调用前再多一次模型调用。
  关键词匹配够用——提升本身是廉价的（多几个 schema），代价错配也只是多几百 token。
- **提升 ≠ 可用**：它只把名字放进 `promoted`；能不能出现在 schema 里、能不能被调用，
  仍由 §6.4 第 4 步的技能策略决定。
- **审计可关**：`middleware:tool_promotion` 是 M4 catalog 里已留的 tag；
  高频 run 里它可能很吵，配置项 `tool_search.audit_promotions` 控制。

### 6.10 不变量与失败模式


| 不变量                     | 谁保证                                                    |
| ----------------------- | ------------------------------------------------------ |
| 提升 ≠ 可用                 | `SkillToolPolicyMiddleware`（§3.8）                     |
| 幽灵名字不报错                 | `catalog.resolve` 按**当前** catalog 过滤                  |
| schema 永远来自当前 catalog   | filter 用 `entry.tool`，不用缓存的历史 schema                  |
| `tool_search` 自己永远可用   | `ALWAYS_AVAILABLE_BUILTIN_TOOL_NAMES`（§3.8）           |
| 可见集每轮重算                 | filter 在每次 `awrap_model_call` 里跑                     |
| 同一 thread 的提升跨 run 保留    | `promoted` 进 checkpoint（reducer 并集）                   |


| 情况                | 行为                                                                    |
| ----------------- | --------------------------------------------------------------------- |
| `query` 非法 regex  | 退化成字面子串匹配                                                             |
| `query` 空 / 全空白   | 参数校验失败 → 工具错误消息（**不**提升任何东西）                                          |
| 命中 0 条            | 返回 "No matching tool"，不写 state                                        |
| 命中 > limit        | 截断到 `min(limit, max_results)`，消息里说明"还有更多，换更具体的关键词"                     |
| 重复搜同一个词           | 第二次返回空（已提升的被 `exclude` 掉）——这是幂等，不是失败                                |
| run 中途配置变了        | 本次 run 继续用组装时冻结的 catalog；新 run 用新的（签名失效，§2.3）                        |
| `promoted` 超过上限   | 丢最早提升的，记 warning                                                      |
| 服务器下线             | 名字仍在 `promoted` 里，但 `resolve` 解析不出 → 不进 schema；模型若凭历史调用它，得到"未知工具"错误      |
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
| M1 系统提示词 | 技能块作为静态提示词的**尾部**；**始终**渲染 L0 索引（`render_skill_index`，无开关） |
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
| `test_mcp_skills_routes.py` | 普通用户 `PUT /skills/{name}/preference` → 200 且只影响自己；`PATCH /skills/{name}` → 403；`PUT /api/mcp/config` 与 `POST /api/skills` **路由不存在**（404/405，不是 403）；`GET /skills` 要 `skills:read` 不要 `admin`；PUT/PATCH/reload/mcp 要 `admin`；`POST /skills/reload` 不会被当成技能名；`skills` / `mcpInterceptors` 出现在 PUT body → 422；`"****"` 还原原值，没有原值 → 422 `invalid_extensions_config`；响应 JSON 不含原始 env；启用非法 stdio `command` → 422 `stdio_command_rejected` 且 detail 无命令行；禁用坏配置仍 200；未知技能 404 `skill_not_found` |
| `test_config_lock_in_worker.py` | 锁在 `to_thread` worker 内持有；取消 awaiting task 不提前放锁 |
| `test_config_signature.py` | 同秒编辑、mtime 倒退、换更老文件 → 判定已变更；加载中签名变化走会话退役 |
| `test_extensions_path.py` | 显式路径缺失 → FileNotFoundError；搜索模式缺失 → None |
| `test_skill_loading.py` | 嵌套 `SKILL.md` 不注册；一级符号链接目录可用；深层逃逸被拒；UTF-8 中文；custom 盖 public |
| `test_skill_scope_layers.py` | `effective = global and pref`；全局关掉后用户打不开（preference=true 也无效）；个人关掉后索引里没有它、slash 也不算激活；两层各用各的锁，互不阻塞；两个用户各自开关互不影响；prefs 并发写不丢 |
| `test_skill_index.py` | 索引只列已启用且在白名单里的技能；排序稳定（加技能只追加自己那一行）；描述截断到上限；`dir=` 是容器路径不是宿主路径；需密钥的技能被标出；registry 加载失败 → 索引为空（fail-closed）；name/description 被 HTML 转义；0 技能 → 整个块不出现 |
| `test_skill_progressive.py` | L0 索引里**没有**正文、没有 `allowed-tools` / `argument-hint` / license；工具集里只有按需加载工具、没有元数据工具（L1 已被删除） |
| `test_skill_parser.py` | `Bash`→`bash`；未知名保持；未闭合括号炸；畸形 required-secrets 丢掉技能仍加载 |
| `test_slash_skill_contract.py` | 与 `contracts/slash_skill_contract.json` 双向钉死；前导空白/保留命令拒绝 |
| `test_skill_policy.py` | 被动启用不收紧；slash 支配；并集；空声明不拆别人的限制；`task` 不豁免；框架工具始终可用；`tool_search` 提升的业务工具仍要声明 |
| `test_skill_search_router.py` | 中文二元组分词；BM25 把正文命中排第一；`domain_keywords` 能把查询词没提到的技能拉进来并加权原有命中；`top_k` / `min_score` 截断；向量通道单独驱动顺序、挂掉退回 BM25；精排重排与失败回退；文件消失的技能被跳过 |
| `test_search_and_load_skills_tool.py` | 返回正文 + 容器路径（frontmatter 不重复）；正文预算截断并标记；空查询/空技能树/工具关闭三种措辞；用户关掉的技能不加载；只在 `skills.search.enabled` 时注册；在真实 `create_agent` 循环里跑通；路由协议只在工具启用时出现，且只引用真实渲染的块 |
| `test_durable_context.py`（新增用例） | `search_and_load_skills` 的结果 JSON 被捕获成 `skill_context`（只留 name/path/description，**正文不进 state**）；越界路径与 `README.md` 一律忽略；没配成加载工具的工具结果不算激活；`<active_skills>` 在正文已不在消息里时补正文、仍在消息里时不重复写、预算截断并标注、`active_context.enabled=false` 只留名字行、用户关掉的技能不补正文、技能文件消失只退化成一行 |
| `test_skill_policy_fail_closed.py` | 注册表失败 → 只剩框架工具；伪造 slash 路径无 token → 当无 |
| `test_skill_activation_request_only.py` | SKILL.md 注入不出现在 checkpoint；同一 slash 本 run 只读盘一次 |
| `test_mcp_tool_prefix.py` | 同名不冲突；关前缀后源路由仍按 server 元数据 |
| `test_mcp_tool_name_charset.py` | 名字含换行/空格的 MCP 工具被丢弃 |
| `test_mcp_stdio_session.py` | `(server, user:thread)` 隔离；断连只驱逐自己；晚到旧错误不误伤；失败不自动重放；owner task 进出同一 task |
| `test_mcp_credentials.py` | 四层优先级；缺失 fail-closed；`passthrough` 放行缺值；**非法值不出现在异常/工具结果/日志** |
| `test_mcp_header_case.py` | 静态 `authorization` + 注入 `Authorization` 最终只有一个键 |
| `test_mcp_runtime_context.py` | 真实适配器工具 + 真实图，关掉 ambient fallback，断言 `request.runtime` |
| `test_mcp_transport_alias.py` | 只写 `transport: http` 不当 stdio |
| `test_tool_search.py` | 未提升的 MCP 工具不在 schema；提升后可见；catalog 变更后幽灵名忽略；非法 regex 不 500；**重复搜同一个词第二次返回空**（幂等）；0 命中不写 state；`limit` 被 `max_results` 夹住；返回消息含"下一轮可用" |
| `test_promoted_channel.py` | reducer 是**保序去重并集**；并发分支各提升一批都保留；超过 `PROMOTED_MAX` 丢最早的；`promoted` 跨 run（checkpoint）保留 |
| `test_deferred_assembly_order.py` | 中间件顺序断言：routing → deferred filter → activation → policy → durable → coalescing；promotion 审计在 policy **之外** |
| `test_mcp_routing.py` | 只提升不执行；同一个 pass 重复不发第二次 `middleware:tool_promotion`；自动提升的工具仍受技能策略限制；`top_k=0` 时整层不生效 |
| `test_api_stdio_allowlist.py` | API 拒绝 `command: /usr/bin/python`；直接改文件仍可加载（运维路径） |

第一个和 `test_mcp_credentials.py` 的"不回显"最值钱。不回显要断言得狠：
构造带换行的凭据，完整字符串不得出现在 `str(exc)`、ToolMessage.content、
caplog 文本里。

---

## 10. 验收清单

- [ ] 配一个 MCP server（stdio / SSE / HTTP 各一次），重启后工具可用
- [ ] 通过 API 启用/禁用一台 MCP，**不重启**当前 worker 即生效（其它 worker 要 `cache/reset` 或等签名失效）
- [ ] `GET /api/mcp/config` 的 `env` / `headers` 只有 `"****"` 或 `null`；把 `"****"` 原样 PUT 回去不改磁盘上的真值
- [ ] 缺 `admin` 的令牌调 PATCH/reload → 403；只有 `skills:read` 的令牌仍能 `GET /api/skills`，并且能改**自己的**偏好
- [ ] **`PUT /api/mcp/config` 与 `POST /api/skills` 在 M7 返回 404**（不是 403）——创建类能力确实没实现，而不是“你没权限”
- [ ] 全局禁用的技能，用户把个人偏好打开也仍然是关闭；用户自己关掉的技能不出现在他的 `<available_skills>` 索引里，slash 也不激活
- [ ] 两个并发 PATCH（一个改技能、一个改 MCP），两个改动都在文件里
- [ ] 写配置的瞬间 kill 进程，重启后 JSON 仍是完整合法文件
- [ ] 新建 `skills/public/foo/SKILL.md`（中文正文），Windows 上模型能发现
- [ ] `/foo task` 激活后该技能 `allowed-tools` 生效；未 slash 的启用技能不收紧工具集
- [ ] 受限 slash 下 `read_file` 另一个更宽的 SKILL.md，工具集不扩大
- [ ] 技能树里放 20 个以上技能，用一句自包含的任务意图调 `search_and_load_skills`：
      第一条命中就是该用的那个，正文里带 `path`；同一意图第二次调用不重复加载
- [ ] 把 `skills.search.embedding.enabled`/`rerank.enabled` 打开但指向一个不存在的端口：
      检索照常出结果（退回 BM25 / 融合顺序），`retrieval.warnings` 写明降级原因
- [ ] 故意带换行的 MCP 凭据，错误信息里没有凭据内容
- [ ] stdio 服务器被 kill，下一次调用报错但不影响其它 thread 会话，再下一次自动重建
- [ ] 客户端 POST 的 `context.secrets` 不出现在 `GET /runs/{id}` 的 kwargs 里
- [ ] slash 合同前后端一致：`/new` 不当技能，`/pdf-processing` 当技能

并发 PATCH 用脚本打，手工点很难撞上竞争窗口。

---

## 11. 刻意不做的事

| 不做 | 理由 |
|---|---|
| 全量元数据模式 / `skills.deferred_discovery` 开关 | §3.5：索引是有界形态，正文一律按需；两套渲染等于两套提示词契约与两套测试 |
| MCP 长时任务（durable task） | 租约 + 轮询 + 通知投递；M9 默认跳过，形状见 [M9 §6](2026-09-16-m9-production-design.md) |
| 技能静态扫描 / SkillScan / 质量审查 | 有社区分发需求时再做 |
| 技能沙箱投影（物理隔离视图） | 按 agent 策略做文件级隔离；M7 用整树 mount |
| 托管集成（Lark CLI 等） | 产品化，不是框架能力 |
| per-user `skills/custom` 拆分 | M9 多租户时再搬；M7 全局 custom 即可 |
| **技能的创建 / 上传 / 编辑**（`POST /api/skills`、技能包解压、在线编辑 `SKILL.md`） | 见 §2.4：完整生命周期（校验、逃逸、审计、回滚）两周做不完；**后续阶段 + 业务管理员**（能力位 `skills:create`） |
| **MCP 服务器的新建 / 删除 / 整份替换**（`PUT /api/mcp/config`） | 整份替换就是创建+删除的入口；**后续阶段 + 业务管理员**（能力位 `mcp:create`）。M7 只提供已有服务器的启停 |
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
# MCP Server 管理 rev.10：MyFlow 自管 server，直连 MCP 执行

> 状态：草案，待确认（2026-11-08）。核心决策已确认：MCP Server 按 MyFlow 用户私有。
> 关系：取代上一版工具目录设计（rev.9）。
> 本方案不再依赖外部 MCP 网关；MyFlow 自己保存 mcpServers 配置、自己发现工具、自己执行工具。
> 前提：服务尚未部署；旧表、旧 Milvus collection、旧配置可直接删除/重建。

## 1. 结论

目标是把管理面改成：

```
用户在前端管理 MCP Server（mcpServers）
        │  /api/mcp-servers
        ▼
MyFlow 后端保存 server 配置
        │
        ├─ initialize + tools/list ──> mcp_tools / Milvus
        │
        └─ tools/call ───────────────> 上游 MCP Server
```

**不变量：**

1. MyFlow 自己管理 MCP Server；不依赖外部网关，也不再有外部管理凭据/管理事件代理。
2. 用户只管理 server；工具由后端自动发现，前端只读展示。
3. `mcp_tools` 是运行期工具定义的唯一读取源；Milvus `myflow_mcp_tools` 是派生检索索引。
4. `tool_search` / `tool_execute` 的输入输出、run-scoped 状态、schema 预算不变。
5. `tool_execute` 直接连接工具所属的 MCP Server 执行 `tools/call`。
6. 固定单 worker；同步仍用进程内 `asyncio.Lock` 串行化。
7. 工具同步骨架沿用现有方案：全量快照、callable 派生、DB 先提交、Milvus 后重建、
   失败保留上一版、启动/管理写/午夜/失败刷新/手动 reindex 触发。

## 2. 非目标

- 不支持 stdio server；v1 只支持 Streamable HTTP 和 SSE。
- 不在前端编辑工具名称、schema、标签、启停状态。
- 不做多 worker、leader 选举、DB 租约。
- v1 不实现 MCP `notifications/tools/list_changed` 长连接；更新靠管理写、手动刷新、
  午夜全量、调用失败刷新。
- 不在 v1 做 OAuth / basic 表单；server 认证先只支持 HTTP headers。

## 3. 目标架构

```
┌────────────────────────────────────────────────────────┐
│ MyFlow Gateway                                         │
│                                                        │
│  /api/mcp-servers  CRUD        MCP Connection Client   │
│  /api/mcp-servers/{id}/refresh  ├─ initialize          │
│  /api/mcp-servers/{id}/tools    ├─ tools/list          │
│                                  └─ tools/call         │
│                                                        │
│  tool_search / tool_execute                            │
└───────────────┬───────────────────────────┬────────────┘
                │ DB transaction            │ tools/call
                ▼                           │
┌──────────────────────────────┐            │
│ PostgreSQL / SQLite          │            │
│  mcp_servers                 │            │
│  mcp_tools                   │            │
└───────────────┬──────────────┘            │
                │ 派生检索文档                │
                ▼                           │
┌──────────────────────────────┐            │
│ Milvus myflow_mcp_tools      │            │
│  tool_id / server_id         │            │
│  owner_user_id               │            │
│  tool_name / call_name       │            │
│  description / parameters    │            │
│  content / embedding / sparse│            │
└───────────────┬──────────────┘            │
                │ 候选 tool_id               │
                ▼                           │
┌──────────────────────────────┐            │
│ tool_search                  │            │
│  hybrid + owner + DB join    │            │
└───────────────┬──────────────┘            │
                │ schema 注入模型上下文        │
                ▼                           │
┌──────────────────────────────┐            │
│ 模型                          │            │
│  tool_execute(call_name,args)│            │
└───────────────┬──────────────┘            │
                │                           │
                ▼                           │
┌──────────────────────────────┐            │
│ tool_execute                 │────────────┘
│  DB owner/callable 校验       │
│  jsonschema 校验              │
│  直连 server tools/call       │
└──────────────────────────────┘
```

## 4. 数据模型

### 4.1 新增 `mcp_servers`

| 字段 | 类型 | 说明 |
|---|---|---|
| id | String(64) PK | MyFlow server UUID |
| owner_user_id | String(64), not null, index | 创建者；用户私有 |
| name | String(128), not null | 稳定 slug，只允许 `[a-z0-9-]`；创建后不可改 |
| display_name | String(255), null | 展示名，可改 |
| description | Text, not null | 描述 |
| transport | String(16), not null | `streamable_http` / `sse` |
| url | String(2048), not null | MCP endpoint |
| headers_encrypted | Text, null | HTTP headers，Fernet 加密 |
| verify_tls | Boolean, not null | 默认 true |
| enabled | Boolean, not null | 是否参与发现/执行 |
| status | String(16), not null | `pending` / `active` / `error` / `disabled` |
| reachable | Boolean, not null | 最近一次 initialize 是否成功 |
| last_connected_at | DateTime, null | 最近成功 initialize |
| last_tools_sync_at | DateTime, null | 最近成功 tools/list |
| last_error | Text, null | 截断后的错误 |
| created_at / updated_at | DateTime, not null | |

约束：

```
UNIQUE(owner_user_id, name)
INDEX(owner_user_id, enabled, status)
```

规则：

- `name` 创建后不可改，保证 `call_name` 稳定；展示名改 `display_name`。
- 两个用户可以各自建 `anysearch`，因为 `name` 只在 owner 内唯一。
- server 删除时，属于它的 `mcp_tools` 和 Milvus 文档一起删除。
- `headers_encrypted` 只存密文；API 只返回掩码。

### 4.2 新增 `mcp_tools`

| 字段 | 类型 | 说明 |
|---|---|---|
| id | String(64) PK | UUID5(server_id + tool_name)；稳定 |
| server_id | String(64), not null, index | 指向 `mcp_servers.id` |
| owner_user_id | String(64), not null, index | 从 server 继承 |
| name | String(255), not null | MCP 原始工具名 |
| call_name | String(255), not null | `mcp__<server.name>__<name>` |
| display_name | String(512), null | 展示名 |
| description | Text, not null | |
| input_schema | JSON, not null | 原样 MCP inputSchema |
| annotations | JSON, not null | MCP annotations |
| parameters | JSON, not null | schema 参数名 |
| content | Text, not null | BM25/embedding 文本 |
| content_hash | String(64), not null | content + owner + call_name |
| schema_hash | String(64), not null | canonical input_schema |
| enabled | Boolean, not null | 默认 true |
| reachable | Boolean, not null | 从 server 继承 |
| callable | Boolean, not null | 运行期门；由同步派生 |
| last_seen_at | DateTime, not null | 最近一次 tools/list 出现 |
| created_at / updated_at | DateTime, not null | |

约束：

```
UNIQUE(server_id, name)
UNIQUE(owner_user_id, call_name)
INDEX(owner_user_id, callable)
INDEX(server_id, callable)
```

规则：

- `call_name = "mcp__" + server.name + "__" + tool.name`；不同 server 的同名工具不会冲突。
- `callable` 只在 server enabled + reachable、tool 被本次 tools/list 看见、owner 存在、
  name 安全时成立。`callable` 由 store 派生，不信任同步服务的入参。
- 工具从 tools/list 消失时保留定义但 `callable=false`；server 删除时物理删除。
- 工具 owner 永远从 `mcp_servers.owner_user_id` 继承，不做工具级认领。
- `mcp_tools` 的 `name` 是远端原名；`call_name` 是模型/策略名。

## 5. MCP Server 连接

新增 `McpServerClient`：

- `initialize` + `tools/list`，支持分页；
- `tools/call`；
- 每次操作打开一个短 MCP session，调用完成即关闭；
- transport 选择：
  - `streamable_http` → `streamablehttp_client`；
  - `sse` → `sse_client`；
- headers 从 `mcp_servers.headers_encrypted` 解密后传入传输层；
- 支持 `verify_tls=false`，默认 true；
- 连接/读取超时来自 server 配置或全局默认。

连接错误分类：

| 情况 | 错误 |
|---|---|
| DNS/连接失败、TLS 失败 | `McpServerUnavailable` |
| 超时 | `McpServerTimeout` |
| initialize 协议失败 | `McpProtocolError` |
| 工具不存在 | `McpToolNotFound` |
| 上游工具执行失败 | `McpToolError` |

错误消息不得包含 headers、token、完整 URL 查询串。

## 6. 同步

### 6.1 沿用现有骨架

继续使用现有同步服务的语义：

- 一次同步构建一份工具快照；
- `upsert_snapshot` 在单事务中写 DB；
- `callable` 由 store 派生；
- `content_hash` / `schema_hash` 由 store 重算；
- DB 提交后重建 Milvus；
- 失败保留上一版定义，不把目录清空；
- 进程内 `asyncio.Lock` 串行化所有触发。

变化只在输入：不再有“管理 API + MCP 双视图”，输入变成每个 server 的
`initialize + tools/list` 单视图；工具 owner 来自 server，不再来自 annotations。

### 6.2 同步顺序

`McpToolSync.sync()`：

1. 读取所有 `mcp_servers`（owner 可选过滤；运行期同步是全量）。
2. 对每个 enabled server：
   - 解密 headers；
   - `initialize`；
   - `tools/list` 分页拉全量；
   - 成功：server `reachable=true`、`status=active`、`last_tools_sync_at=now`；
   - 失败：server `reachable=false`、`status=error`、`last_error` 截断记录；
     该 server 的上一版工具定义保留，但本次不算 seen。
3. 构建 `mcp_tools` 记录：
   - 成功的 server：用本次 tools/list 定义；
   - 失败的 server：carry 上一版 schema/description，seen=false；
   - disabled server：carry 上一版，seen=false；
   - owner 从 server 继承；
   - `call_name` 由 server.name + tool.name 生成；
   - 工具名/参数/schema 不安全：保留定义但不可调用。
4. `upsert_snapshot` 写 DB。
5. 从 DB 重建 Milvus；`full=true` 时删除不在目录里的旧文档。
6. server 删除的 tool 行从 DB 删除，Milvus 同步删除。

单 server 失败不能中止其它 server 的同步；只有 DB 写入或 Milvus 重建失败才影响整次同步。
如果所有 enabled server 都失败且目录原本非空，保留上一版，不把目录清空。

### 6.3 触发

- 启动：同步所有 enabled server。
- 创建/更新/启停/刷新 server：同步该 server（实现上可复用全局快照）。
- 每天本地 `daily_at` + jitter：全量同步。
- `tool_execute` 遇到工具不存在 / 参数校验失败 / schema 不匹配：刷新该 server 后重试一次。
- `POST /api/mcp-servers/reindex`：手动全量 + GC。
- v1 不订阅 MCP tools/list_changed；实时性由管理写、手动刷新、午夜兜底和失败刷新保证。

## 7. tool_execute 直连

`tool_execute(call_name, arguments)`：

1. 检查 `call_name` 在 `ThreadState.retrieved_tool_names` 中（`require_retrieved`）。
2. 按 `owner_user_id + call_name + callable=true` 查 `mcp_tools`。
3. 查所属 `mcp_servers`：enabled、reachable、owner 匹配。
4. 用 `jsonschema` 校验参数；禁止远程 `$ref`。
5. 解密 server headers。
6. 用 `McpServerClient.call_tool(server, tool.name, arguments)` 直连执行。
7. 返回 ToolMessage；错误分类：

| 情况 | 行为 |
|---|---|
| server 不可达/超时 | 标记 server 降级；返回可读错误；按配置刷新 |
| tool not found | 刷新该 server，重试一次 |
| schema mismatch | 刷新该 server，重试一次 |
| 上游执行错误 | 原样转为 ToolMessage 错误，不重试 |
| 参数校验失败 | 本地拒绝，刷新后重试一次（若配置开启） |

执行前必须验证 owner 和 callable；不能靠解析 `call_name` 找 server。
server 删除/禁用后，旧 `call_name` 必须失败。

## 8. API

新增 `/api/mcp-servers`：

| 方法/路径 | 作用 |
|---|---|
| `GET /api/mcp-servers` | 当前用户的 server 列表 |
| `POST /api/mcp-servers` | 创建 server，尝试 initialize + tools/list |
| `GET /api/mcp-servers/{id}` | server 详情（headers 掩码） |
| `PUT /api/mcp-servers/{id}` | 更新 display_name/description/url/transport/headers/verify_tls |
| `DELETE /api/mcp-servers/{id}` | 删除 server + tools + Milvus 文档 |
| `POST /api/mcp-servers/{id}/state` | 启停 |
| `POST /api/mcp-servers/{id}/refresh` | 重新 tools/list 并同步 |
| `GET /api/mcp-servers/{id}/tools` | 只读工具列表 |
| `GET /api/mcp-servers/status` | 全局 server 计数/同步状态 |
| `POST /api/mcp-servers/reindex` | 手动全量同步 + GC |

scope：

- `mcp_servers:read` / `mcp_servers:write`；
- 会话用户 `scopes=None` 放行；PAT 必须显式带 scope；
- admin 仍通过 `RunCaller.user_role` / `admin` scope 判断，不从 PAT 获得 admin。

请求体：

```json
{
  "name": "anysearch",
  "display_name": "AnySearch",
  "description": "实时搜索",
  "url": "https://api.anysearch.com/mcp",
  "transport": "streamable_http",
  "headers": {
    "Authorization": "Bearer <key>",
    "X-Anysearch-Client": "mcp/1.0.0"
  },
  "verify_tls": true,
  "enabled": true
}
```

- `name` 创建后不可改；`display_name` 可改。
- `headers` 为空 = 无认证；更新时不传 = 保持原值；
- `****` 且当前没有对应 header → 422，不把字面量当凭据。
- 创建/更新时先落 DB，再同步；连接失败保留 server 行并置 `status=error`，
  前端展示 `last_error`，用户可修正后 refresh。
- 删除 server 先删 DB 行/tools，再删 Milvus 文档；MCP server 本身无远程删除语义。

## 9. 前端

- 路由 `/workspace/tools` 改为 `/workspace/mcp-servers`；
  `/workspace/tools` 重定向到新页面。
- 侧边栏文案“工具管理”改为“MCP 服务”。
- 列表：name/display_name、URL、transport、status、reachable、tool_count、
  last_tools_sync_at、last_error。
- 新建/编辑：name、display_name、description、url、transport、headers、
  verify_tls、enabled。
- 详情：只读工具列表（tool name、call_name、description、enabled、callable、
  parameters）；不提供工具编辑/删除/认领。
- 操作：刷新 server、刷新全部、重建索引、启停、删除。
- headers 输入沿用掩码规则：读取 `****`，编辑原样回传；不显示 raw secret。
- `name` 编辑时只读；`display_name` 可改。

## 10. 安全

- `mcp_servers.headers_encrypted` 用 Fernet 加密；密钥来自
  `mcp_servers.encryption_key` / `MYFLOW_MCP_SERVER_KEY`，不能复用 auth/JWT secret。
  启用 `mcp_servers` 但没有密钥时启动直接失败。
- API/日志/trace 不返回或记录 raw headers、token、完整 URL 查询串。
- server URL 默认只允许 `https://`；私网/loopback/link-local 默认拒绝，
  需要显式 `mcp_servers.allow_private_networks=true` 或 allowed hosts 才放行。
  这是 SSRF 边界，不能只靠前端校验。
- header 名做 RFC 允许字符校验并拒绝 CRLF；值/数量/总长度设上限。
- MCP 返回的 description/schema 是作者数据：卡片转义并标明“不是系统指令”；
  schema 不解析远程 `$ref`。
- tool 结果只作为 ToolMessage，不进入 system prompt。
- 审计记录 server_id、tool_id、call_name、耗时、状态；不记录 headers/参数中的敏感值。
- 每个用户只能看到自己的 server 和工具；Milvus 与 DB join 两层 owner 过滤，
  tool_execute 再查一次 owner/callable。

## 11. 配置与部署

移除旧的外部网关配置块（管理 URL/凭据、agent token、管理事件、虚拟 server 等），改为：

```yaml
mcp_servers:
  enabled: true
  encryption_key: $MYFLOW_MCP_SERVER_KEY
  request_timeout_seconds: 30
  connect_timeout_seconds: 5
  max_servers_per_user: 20
  allow_private_networks: false
  allowed_schemes: ["https"]

tool_search:
  enabled: true
  candidate_k: 20
  top_k: 5
  rrf_k: 60
  max_schema_chars: 3000
  max_result_chars: 16000
  require_retrieved: true
  max_retrieved_tools: 5
  keyword: { enabled: true, analyzer: chinese, bm25_k1: 1.2, bm25_b: 0.75 }
  embedding: { enabled: true, query_instruction: "Given a task, retrieve the tool that can perform it" }
  rerank: { enabled: false }
  vector_store:
    collection: myflow_mcp_tools
    metric_type: COSINE
    index_type: AUTOINDEX
    consistency_level: Bounded
    flush: true
```

- 单 worker 约束保留：`mcp_servers.enabled=true` 且 `GATEWAY_WORKERS>1` 时启动拒绝。
- Docker：移除外部网关服务及其数据库初始化服务；增加 `MYFLOW_MCP_SERVER_KEY`。
- 依赖：MCP SDK 和 cryptography 已是核心依赖；不再需要管理 API 客户端/事件订阅器。

## 12. 迁移与删除

服务尚未部署，按“删旧建新”处理，不做历史数据迁移：

- 新迁移 `0018_mcp_servers`：
  - 建 `mcp_servers`、`mcp_tools`；
  - 删除旧工具目录表（若存在）；
  - 可重入：`Base.metadata.create_all` 已建表时迁移只补缺。
- Milvus：
  - 新 collection `myflow_mcp_tools`；
  - 旧 collection 不再使用，首次启动/手动 reindex 重建；旧 collection 可手动删除。
- 配置：
  - 删旧的外部网关配置块及 docker 里的网关服务/凭据；
  - 改 `tool_search` 为新位置。
- API/前端：
  - 删 `/api/tools` 写接口和工具编辑器；
  - 新 `/api/mcp-servers` 和 `/workspace/mcp-servers`；
  - `cf__` 前缀全部改为 `mcp__server__tool`。
- 旧的手工工具没有 server 归属，不迁移；用户在 server 页面重建。

## 13. 代码落点

建议把现有工具目录模块改名为 `agentkit/mcp`，并拆分：

- `agentkit/mcp/models.py`：`McpServer`、`McpTool`、`McpServerWrite`、错误类型。
- `agentkit/mcp/store/`：`mcp_servers` / `mcp_tools` ORM 与 repository。
- `agentkit/mcp/client.py`：`McpServerClient`（initialize/list/call）。
- `agentkit/mcp/sync.py`：沿用现有快照/失败保留/Milvus 重建逻辑。
- `agentkit/mcp/runtime.py`：启动同步、午夜任务、手动 reindex、server 刷新、状态。
- `agentkit/tools/builtin/tool_search.py` / `tool_execute.py`：只改数据源/执行目标。
- `backend/app/gateway/routers/mcp_servers.py`：server CRUD。
- `frontend/src/components/mcp-servers/`：列表/编辑器/详情。

删除：

- 旧管理客户端、管理事件订阅器、管理代理路由、旧工具编辑器；
- 旧工具目录表、旧 Milvus collection、旧 `cf__` 命名；
- 旧配置块和 docker 中的外部网关服务。

## 14. 测试

- `test_mcp_server_store.py`：server CRUD、加密 headers、owner 隔离、name 唯一。
- `test_mcp_client.py`：streamable_http/sse、headers、分页、超时/协议错误。
- `test_mcp_tool_sync.py`：快照、callable 派生、单 server 失败保留、server 删除级联、
  call_name 生成、重复工具名冲突。
- `test_mcp_servers_router.py`：CRUD、refresh、state、tools 只读、错误码、headers 掩码。
- `test_tool_search.py`：server 字段、owner 过滤、预算、retrieved state。
- `test_tool_execute.py`：DB owner/callable、jsonschema、直连 call、not-found 刷新重试、
  server 不可达、disabled server。
- `test_mcp_server_migration.py`：0018 建表/删旧表/可重入。
- 前端 DOM：列表、编辑器、headers 掩码、transport、只读工具列表。
- 集成（opt-in）：真实 MCP server + Postgres/Milvus。

## 15. v1 决策

1. **已确认**：MCP Server 按 MyFlow 用户私有。
2. server `name` 在 owner 内唯一且不可改；`display_name` 可改。
3. 工具命名空间 `mcp__<server>__<tool>`；不再使用 `cf__`。
4. transport 只支持 `streamable_http` / `sse`；stdio 不支持。
5. 认证只做 HTTP headers；OAuth/basic 后续再加。
6. v1 不订阅 MCP 变更通知；靠管理写、手动刷新、午夜、失败刷新。
7. 单 worker；同步锁是进程内 `asyncio.Lock`。

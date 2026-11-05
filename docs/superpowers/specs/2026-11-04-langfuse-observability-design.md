# Langfuse Agent Observability Design

- **Status:** Implemented (Phase 1–3); remote connectivity smoke deferred
- **Date:** 2026-11-04
- **Deployment decision:** 远程自建 Langfuse 已安装；MyFlow 只做客户端接入（2026-11-04 确认）
- **Goal:** 在不改变现有 run/checkpoint/SSE 语义的前提下，为 MyFlow 的 lead agent、工具调用和隔离子代理增加一套可检索、可关联、可脱敏的 Langfuse trace。

## 1. 结论

推荐采用 **LangChain CallbackHandler + 一个显式的 run 根 span + 子代理独立 handler** 的混合接入，而不是重写现有 run event 链路，也不是一开始就上全量 OpenTelemetry。

- Langfuse 负责：LLM generations、tool spans、agent 调用树、延迟、token/成本、错误、按 trace 检索。
- MyFlow 的 run_events 继续负责：会话消息、SSE 回放、checkpoint 血缘、业务审计；它仍然是 UI 的事实来源。
- 两边使用**同一个 trace id**。当前 agentkit.trace_context.ensure_trace_id() 生成的是 32 位小写 UUID hex，正好满足 Langfuse 对 trace id 的要求，因此不需要数据库迁移，也不需要把 Langfuse id 写进 checkpoint。
- 默认接入远程自建 Langfuse（基础设施由远程侧维护），默认对 trace 内容做脱敏；Langfuse 故障不能影响 agent run。

## 1.1 代码落地状态

已实现：

- `observability.langfuse` 配置、校验、startup-only 边界
- `agentkit/observability/`：客户端生命周期、run 根 span、lead/subagent callback、PII 导出脱敏
- worker 与隔离子代理的 trace 传播
- 客户端 `langfuse_*` metadata 过滤
- `langfuse>=5.1,<6` 可选依赖、`.env.example`、`config.example.yaml`、后端 README
- 使用真实 SDK + 内存 exporter 验证了 trace id、父子 span、跨线程子代理和脱敏；未连接远程实例

未实现/待办：

- 远程连通性、真实 Langfuse UI 冒烟（按用户要求跳过）
- Phase 4 的 run 详情深链/用户反馈 score；`project_id` 字段先保留
- dashboard、保留期和采样策略需要远程 Langfuse 侧配置

验证命令：

~~~bash
uv run python -m pytest backend/tests -q
# 2397 passed, 7 skipped
~~~

## 1.2 远程冒烟记录（2026-11-04）

- `config.yaml` 已启用 `observability.langfuse.enabled: true`，`user_id_salt` 通过 `.env` 的 `LANGFUSE_USER_ID_SALT` 注入（64 hex）；`configure_observability()` 初始化结果为 enabled。
- `GET http://192.168.154.141:3050/api/public/health` 返回 200，Langfuse 版本 `4.74.0`；`/api/public/ready` 也返回 200。
- `GET /api/public/projects` 无认证时快速返回 401；带 Basic auth 现在约 0.5 秒返回 200，项目名为 `myflow`。
- Langfuse Python SDK `5.1.0` 的 `auth_check()` 返回 `True`。
- 远程首页的 `next-auth.callback-url` 仍指向 `http://192.168.154.141:3000`，但实际 Langfuse 在 `:3050`；`:3000` 是 Gitea。API 认证已通，但浏览器 UI 登录仍应把远程 `NEXTAUTH_URL` 改成 `http://192.168.154.141:3050`。
- 当前结论：远程 API 连通性通过，MyFlow 客户端配置已启用；下一步是重启 Gateway 并发一条 workspace 消息，验证真实 trace。
- 2026-11-04 进一步用 SDK 从开发机发送了一条 `myflow.connectivity-check` 测试 trace（trace_id `594f70da51b04653a3f27946ef60984f`），`flush()` 成功；用于区分“远程 OTLP 接收问题”和“Gateway 未加载新配置”。
- `192.168.154.141:8001` 的 Gateway 在线但受认证保护；http 读不到它的进程配置。`observability.langfuse` 是 startup-only，改完配置后必须重启，且要确认 Gateway 使用的是本仓库修改后的 `config.yaml`/`.env`。
- 2026-11-05 02:30 UTC 复核：直接 OTLP probe 和 SDK probe 都能在 Langfuse `/api/public/v2/observations` 查到，说明远程接收/查询链路正常。
- Gateway 数据库显示最后一次 run 是 01:58 UTC；Postgres 连接池 backend_start 是 02:27 UTC，说明 Gateway 在 02:27 附近重启过，但重启后还没有新的 run。需要用前端再发一条消息后再查 trace。

## 2. 现状与接入点

| 位置 | 现状 | 对本方案的意义 |
|---|---|---|
| backend/packages/harness/agentkit/trace_context.py | 生成 32 hex trace id，写入 run metadata | 直接作为 Langfuse trace id，实现日志/API 错误/Langfuse 三方关联 |
| backend/app/gateway/services.py | build_run_config 合并 metadata；inject_server_owned 注入 trace_id | 需要在这里过滤客户端伪造的 langfuse_* metadata |
| backend/packages/harness/agentkit/runtime/runs/worker.py | _stream_graph 已有 usage 和 RunJournal 两个 callback | 在这里追加 Langfuse handler，并包一层 run 根 span |
| backend/packages/harness/agentkit/runtime/events/journal.py | 已有 run event 账本，记录 message/tool/error | 保留不动；Langfuse 是旁路，不是替代 |
| backend/packages/harness/agentkit/subagents/executor.py | 子代理在独立线程/独立 event loop 上跑，会摘掉 myflow_loop_bound handler | 父 handler 标记为 loop-bound，子代理另建 handler，避免跨 loop 状态污染 |
| backend/packages/harness/agentkit/agents/middlewares/pii_middleware.py | 模型输入/输出和工具参数已有 PII vault | trace 导出前必须再做一次不可逆脱敏，不能把 handle 或原文送去 Langfuse |

现有代码里 create_chat_model(..., attach_tracing=False) 的命名只是预留参数，并没有真正接 Langfuse；本方案不依赖它，而是在 run 级注入 callback，覆盖面更完整。

## 3. 方案比较

### A. LangChain CallbackHandler（推荐）

用 langfuse.langchain.CallbackHandler 接 LangChain/LangGraph。官方文档明确 LangGraph 使用同一套 callback 方式。

- 优点：改动小；自动拿到 LLM、tool、chain/agent 层级；token usage、模型、错误和耗时基本开箱即用。
- 缺点：子代理跑在隔离 loop 上，需要显式传播 trace 上下文；非 LangChain 的 sandbox/HTTP/DB 调用不会自动出现。
- 适用：现在这个以 LangGraph + LangChain tools 为主的 agent。

### B. 全量 OpenTelemetry + Langfuse OTLP

初始化 OTel SDK，把 HTTP/DB/自定义 span 和 LLM span 一起送到 Langfuse 的 OTLP 入口。

- 优点：真正的端到端分布式追踪，未来多服务时最标准。
- 缺点：要处理全局 TracerProvider、采样、传播和第三方 instrumentation；当前仓库没有 OTel 基础设施，首期成本明显高于收益。
- 适用：出现网关/worker/外部服务多跳，或需要接入现有 APM 时再升级。

### C. 只扩展现有 run_events

继续使用 RunJournal 和数据库账本，不引入 Langfuse。

- 优点：没有新服务、没有新依赖。
- 缺点：没有现成的 agent 调用树、prompt 对比、成本分析和跨 run 检索；自己补这些等于重做 Langfuse。
- 适用：只做合规审计、不做 agent 调试和评估。

**选择 A。** B 作为后续演进，不放进首期。

## 4. 目标架构

~~~text
HTTP / SSE
   │
   ▼
FastAPI gateway ── trace_id (32 hex) ──► logs / error body / X-Trace-Id
   │
   ▼
run worker
   │  with myflow.run (Langfuse agent root span, trace_context.trace_id = trace_id)
   │      ├── Langfuse CallbackHandler ──► lead LLM / tools / retries
   │      ├── task tool ──► subagent trace context
   │      │       └── isolated loop: subagent.<name> span + child CallbackHandler
   │      └── run outcome / error / token usage
   ▼
Langfuse SDK (async OTel batch exporter, redaction hook)
   │
   ▼
Langfuse v4 web + worker + ClickHouse + Postgres + Redis + MinIO
~~~

关键原则：

1. **一个 run 一个 trace**，trace id 与 MyFlow 的 trace_id 相同。
2. **一个 run 一个 CallbackHandler 实例**。CallbackHandler 内部按 LangChain run_id 保存状态，跨并发 run 复用会串 trace。
3. **父 handler 不进入隔离子代理 loop**。通过 myflow_loop_bound 标记在 copy_isolated_subagent_context() 中被摘掉，子代理单独创建 handler。
4. **导出是 best effort**。Langfuse 初始化失败、网络失败、脱敏钩子失败都不能让 run 失败。
5. **内容默认脱敏**。Langfuse 是外部可观测面，不继承 PII vault 的“可还原”能力。

## 5. 数据映射

| MyFlow | Langfuse |
|---|---|
| trace_id（32 hex） | trace id |
| thread_id | session id |
| user_id | user id（默认 HMAC 后的稳定假名） |
| run_id / assistant_id | trace metadata |
| model_name | generation model / tag |
| run.start / run.end | myflow.run 根 span 的开始/结束 |
| lead LLM 调用 | generation |
| tool 调用 | tool span |
| task 委派 | subagent.<name> agent span + 子代理 handler |
| run.error / timeout / cancel | 根 span 的 level=ERROR / status message |
| RunTotals | 与 Langfuse usage 对账；Langfuse 不替代本地 totals |

trace 级属性建议：

- name：myflow.run
- tags：myflow、env:<environment>、model:<model_name>
- metadata：run_id、assistant_id、thread_id、model_name、myflow_version
- 不把原始 prompt、完整 tool result 或用户邮箱放进 metadata；这些只应出现在被 mask 的 input/output 属性里。

## 6. 具体接入设计

### 6.1 配置

新增 observability.langfuse，整段为 **startup-only**（与 sandbox.use、stream_bridge.backend 同级）：

~~~yaml
observability:
  langfuse:
    enabled: false
    base_url: https://langfuse.example.internal   # 换成远程实例地址；不要带 /api/public
    public_key: ""          # 启用时写 $LANGFUSE_PUBLIC_KEY
    secret_key: ""          # 启用时写 $LANGFUSE_SECRET_KEY
    project_id: ""          # 用于生成 trace 深链
    environment: development
    release: ""
    sample_rate: 1.0
    capture_content: redacted   # redacted | metadata_only | full
    user_id_mode: hashed        # hashed | raw | none
    user_id_salt: ""            # 启用 hashed 时必填，独立于 JWT secret
    trace_subagents: true
    flush_on_run_end: false
    required: false
~~~

校验规则：

- enabled=true 时 base_url、public_key、secret_key 必须非空。
- user_id_mode=hashed 时 user_id_salt 至少 16 字符；不要复用 AUTH_JWT_SECRET。
- sample_rate 必须在 [0, 1]。
- enabled=true 但未安装 langfuse 包时，启动直接报可操作的错误；enabled=false 时完全不 import。
- 配置热重载只改 enabled 不足以重建客户端；文档中明确要求重启。

### 6.2 客户端生命周期

在 app.py 的 lifespan 中初始化一次：

~~~python
from langfuse import Langfuse

client = Langfuse(
    public_key=cfg.public_key,
    secret_key=cfg.secret_key,
    base_url=cfg.base_url,
    environment=cfg.environment,
    release=cfg.release or app_version,
    sample_rate=cfg.sample_rate,
    mask_otel_spans=build_mask_otel_spans(cfg.capture_content),
)
~~~

- 客户端是进程级单例；不要在每次 run 时 new Langfuse。
- required=false 时，初始化/鉴权失败只记 warning，agent 继续跑。
- 关闭时在 run manager 排空后调用 flush() / shutdown()，用 asyncio.to_thread 加超时，避免阻塞事件循环。
- 如果以后接入其他 OTel exporter，必须复用同一个 TracerProvider；当前仓库没有其他 OTel 初始化，首期不需要。

### 6.3 run 根 span 与 handler

在 run_agent 中、图开始执行前创建根 span；在 _stream_graph 中把 handler 追加到 callbacks：

~~~python
# 伪代码：真实实现放在 agentkit/observability/langfuse.py
trace_id = record.metadata[TRACE_METADATA_KEY]   # 32 hex，来自 trace_context.py
with client.start_as_current_observation(
    name="myflow.run",
    as_type="agent",
    trace_context={"trace_id": trace_id},
    metadata={"run_id": record.run_id, "thread_id": record.thread_id, ...},
) as root:
    root_span_id = client.get_current_observation_id()   # 16 hex
    with propagate_attributes(
        trace_name="myflow.run",
        session_id=record.thread_id,
        user_id=pseudonym(record.user_id),
        tags=["myflow", env, f"model:{record.model_name}"],
    ):
        handler = CallbackHandler(
            trace_context={"trace_id": trace_id, "parent_span_id": root_span_id}
        )
        handler.myflow_loop_bound = True   # 子代理 loop 不继承父 handler
        yield RunTracing(trace_id=trace_id, root_span_id=root_span_id, handler=handler)
~~~

- _stream_graph 把 handler 追加到现有 callbacks，与 UsageCollector、RunJournal 并列；不覆盖它们。
- 不要用 config.metadata["langfuse_user_id"] 作为唯一身份来源；优先用 propagate_attributes，避免客户端 metadata 污染。
- 根 span 成功时 update(output={"status": "success"})；异常/超时/取消时 update(level="ERROR", status_message=...)。
- 如果 trace 创建失败，返回 no-op tracing 对象，不抛到 run 逻辑。

### 6.4 子代理传播

当前 executor.py 的 copy_isolated_subagent_context() 会摘掉带 myflow_loop_bound 的 handler。利用这个既有机制：

1. task_tool 从 runtime.context 读 trace_id 和新增的 trace_parent_span_id，写入 SubagentDispatch。
2. 执行器在隔离 loop 中创建 subagent.<name> span，parent 指向 run 根 span。
3. trace_subagents=true 时，在子 run 的 callbacks 上追加**新的** CallbackHandler，parent 指向该 subagent span。
4. 子代理的 LLM/tool 自动嵌套；不要再把父 handler 带进去。
5. 子代理结束/超时/取消时更新 subagent span 的状态；别把完整 state 当 output 塞进去。

这样既不会让子代理把父 run 的 journal/usage 状态带进隔离 loop，也能在 Langfuse 里看到“lead → task → subagent”的同一条 trace。

### 6.5 内容脱敏

Langfuse Python SDK v5 提供 mask_otel_spans 导出阶段钩子，适合处理第三方 instrumentation 产生的原始 span 属性。建议：

- capture_content=metadata_only：删除 langfuse.observation.input/output、gen_ai.prompt*/completion*、tool input/output 等属性，只保留模型、耗时、usage、错误类型。
- capture_content=redacted（默认）：对内容属性做 JSON 解析，递归调用现有的 PIIRedactor().redact_for_memory()，再 JSON 序列化回去；它会把 [[PII:...]] handle 和字面量邮箱/手机号/API key 变成 [REDACTED]。
- capture_content=full：仅限本地单人环境，且必须在 UI 中提示风险。
- 对超长 tool result 设上限（例如 256 KiB/属性），超出直接替换为 [TRUNCATED]；不要对大 payload 做多次正则。
- 钩子必须同步、快速、内部 catch 所有异常；Langfuse 对抛异常的 batch 会整批丢弃。
- 这个钩子是“尽力而为的脱敏”，不是 PII vault 级别的隐私边界；敏感数据场景应选 metadata_only 或自建 + 访问控制。

### 6.6 客户端 metadata 防伪造

当前 build_run_config 只保留 trace_id 等服务端键，但客户端仍可传任意 metadata。需要：

- 在 _RESERVED_METADATA_KEYS 或过滤逻辑中剥掉所有 langfuse_* 键（langfuse_user_id、langfuse_session_id、langfuse_trace_name、langfuse_tags、langfuse_prompt）。
- 同样防止客户端通过 config.tags 伪造安全审计标签；服务端 tags 单独追加。
- trace_parent_span_id 只由 worker 写入 runtime context，客户端不可设置。
- 加单测：恶意 metadata 不能改变最终 trace 的 user/session/name。

## 7. 部署方案

### 7.1 远程自建（已安装，客户端接入）

Langfuse 已在远程机器上运行。MyFlow 仓库**不生成 compose、不管理 ClickHouse/Postgres/Redis/MinIO、不做首次初始化**；这里只负责客户端配置、连通性验证和冒烟。本节的范围从“搭基础设施”缩小为“接上远程实例”。

需要从远程管理员拿到：

| 项 | 说明 |
|---|---|
| `base_url` | 例如 `https://langfuse.example.internal` 或内网 IP；如果反向代理带路径前缀，必须包含前缀；不要写成 `.../api/public` |
| `public_key` / `secret_key` | 项目 API key；secret 只进环境变量，不写代码、不进日志 |
| `project_id` | 仅用于生成 trace 深链，可留空 |
| 网络路径 | 内网直连、VPN 还是公网；是否需要 CA/代理 |
| Langfuse 版本 | 先确认远程是 v4 还是更早版本；SDK 版本要与之匹配 |
| 保留期/采样 | 由远程 Langfuse 管理；MyFlow 只需要知道查询窗口 |

客户端接入清单：

1. **先验网络**：从 MyFlow 所在主机执行 `curl -I "$LANGFUSE_BASE_URL"`。HTTPS 自签证书要把 CA 装进系统信任链，不要用 `verify=False` 绕过。
2. **再验密钥**：用一段最小脚本调 `Langfuse(...).auth_check()`；失败时先排查网络、反向代理、key 所属项目和时钟偏差，不要急着改 agent 代码。
3. **配置**：`config.yaml` 中 `base_url` 指向远程地址，密钥写 `$LANGFUSE_PUBLIC_KEY` / `$LANGFUSE_SECRET_KEY`；先保持 `enabled: false`。
4. **装 SDK**：`cd backend && uv sync --extra observability`。
5. **打开开关**：`enabled: true`，重启 Gateway；该段是 startup-only。
6. **冒烟**：发一条会触发工具的 workspace 消息，确认远程 Langfuse 出现 trace，且 trace id 与响应头 `X-Trace-Id` 相同。
7. **回滚**：`enabled: false` + 重启 Gateway；不涉及 MyFlow 数据库迁移，也不删除远程 trace。

跨机器部署要额外注意：

- **导出延迟**：Langfuse SDK 是后台 batch export，远程跨网时首条 trace 可能延迟数秒才出现；冒烟测试不要用“立刻刷新没有”判定失败。
- **网络策略**：MyFlow 主机需要能出站访问远程 `base_url`（通常是 443）；如果走代理，配置 `HTTPS_PROXY` 并确认 SDK 的 httpx 客户端继承它。
- **时钟**：两端开启 NTP。跨机时钟漂移会让 trace 时间线和 token 成本对账看起来“少了/多了”。
- **版本兼容**：远程是 v4 时优先用 `langfuse>=5.1,<6`；如果远程仍是 v3，先在隔离环境验证 SDK 兼容性，或先升级远程，不要把未验证组合直接接生产。
- **数据边界**：远程实例的访问控制、备份、保留期由远程管理员负责；MyFlow 仍然执行 `mask_otel_spans`，不能因为“在内网”就默认可以发原文。

### 7.2 依赖安装

~~~toml
# backend/packages/harness/pyproject.toml
[project.optional-dependencies]
observability = ["langfuse>=5.1,<6"]
~~~

~~~toml
# backend/pyproject.toml
[project.optional-dependencies]
observability = ["agentkit[observability]"]
~~~

~~~bash
cd backend
uv sync --extra observability
~~~

当前仓库的 langchain-core 是 1.6.3，满足 Langfuse v5 对 langchain-core>=1 的要求；Langfuse 5.1.0 当前依赖 OTel 1.45+，不会替换现有 LangChain 版本。

## 8. 分阶段落地

### Phase 0：最小验证（0.5 天）

- 远程实例已就绪；从 MyFlow 主机验证网络、TLS 和 API key（`auth_check()`）。
- 确认远程 Langfuse 版本、项目、保留期；用一个现有 run 做冒烟，确认：
  - UI 能看到一条 trace；
  - trace id 与 X-Trace-Id 一致；
  - LLM/tool span 正常嵌套；
  - 脱敏钩子确实移除了测试邮箱/手机号/API key。

### Phase 1：run 级接入（1–2 天）

- 新增 config、observability 包、no-op 路径。
- worker 创建根 span，_stream_graph 追加 handler。
- 配置解析、启动校验、关闭 flush。
- 单测：disabled 不 import；trace id 一致；handler 不覆盖 usage/journal；错误路径不抛。

### Phase 2：子代理（1–2 天）

- task dispatch 传 trace_parent_span_id。
- 隔离 loop 创建 subagent span + 子 handler。
- 验证父 handler 未被继承、并行子代理不串 trace、超时/取消有终态。
- 单测：spy handler 的 trace id/parent id；无重复 span。

### Phase 3：生产化（1 天）

- 脱敏钩子 + 大小上限；客户端 metadata 过滤。
- 采样、保留期、访问控制、Langfuse 故障演练。
- 运行手册：如何用 trace id 查一次失败 run；如何回滚到 enabled=false。

### Phase 4：可选体验（0.5–1 天）

- 在 run 详情/错误页根据 trace_id + project_id 生成 Langfuse 深链。
- 用户反馈写 create_score，为后续评估准备数据。
- 预置几个 dashboard：p95 延迟、错误率、token/成本、按模型/子代理分布。

## 9. 验收标准

- [ ] 同一个 run 的 API 响应、服务端日志、Langfuse 使用同一个 32 hex trace id。
- [ ] 一次 run 只有一条 trace；lead LLM、tool、subagent 都在同一棵树下。
- [ ] enabled=false 时零网络请求、零额外 import，未安装 langfuse 也能启动。
- [ ] Langfuse 停机/超时/鉴权失败时，run 仍成功，p95 额外延迟不超过约 50 ms（导出为后台批处理）。
- [ ] 测试邮箱、手机号、API key 不会以原文出现在 Langfuse；metadata_only 模式下不出现 prompt/completion。
- [ ] 客户端不能伪造 langfuse_user_id/session_id/trace_name。
- [ ] 子代理超时/取消后没有遗留的 running span；父 run 取消后 trace 标记为错误/取消。
- [ ] token 总量与本地 RunTotals 可对账；差异有明确原因（provider 不报 usage、cache token 口径等）。
- [ ] 保留期和采样率经过确认，有清理/回滚步骤。

## 10. 风险与对策

| 风险 | 对策 |
|---|---|
| Langfuse 导出阻塞或拖慢 run | 后台 batch export；不 await flush；flush_on_run_end 默认 false；超时 |
| Langfuse 故障导致 run 失败 | 全部 tracing 调用包 try/except；required=false 时降级为 no-op |
| prompt/tool result 含 PII | mask_otel_spans + metadata_only 选项 + 自建访问控制 + 保留期 |
| 子代理跨 loop 串 trace | 父 handler 打 myflow_loop_bound；子代理独立 handler；单测并行子代理 |
| 客户端伪造可观测字段 | 服务端剥掉所有 langfuse_* metadata；身份/trace 上下文只由 worker 注入 |
| SDK/服务端版本不兼容 | 固定 langfuse>=5.1,<6 与 Langfuse v4 镜像；升级前跑 smoke；保留回滚 tag |
| 采样导致错误 run 丢失 | 首期 1.0；生产若降采样，接受“错误可能不被采样”，或后续引入 tail sampling |
| 数据量/磁盘增长 | 保留期、采样、ClickHouse 磁盘监控；不要把完整大 tool result 写入 trace |

## 11. 待确认

1. **部署**：远程自建已确认；还需要远程 `base_url`、项目 key 和网络路径（内网/VPN/公网 HTTPS）。
2. **内容级别**：默认 redacted；如果生产环境不允许 prompt/completion 离开应用，改 metadata_only。
3. **采样/保留期**：开发环境默认 1.0；生产目标（例如 0.2 + 30 天）确定后再调。

## 参考资料

- Langfuse LangChain/LangGraph 集成：https://langfuse.com/integrations/frameworks/langchain
- LangGraph cookbook（trace 命名、session/user/tags）：https://langfuse.com/integrations/frameworks/langgraph
- Python SDK instrumentation（context manager / propagate_attributes）：https://langfuse.com/docs/observability/sdk/instrumentation
- 自建概览（v4 架构）：https://langfuse.com/self-hosting
- Docker Compose 部署：https://langfuse.com/self-hosting/deployment/docker-compose
- Python SDK 导出阶段脱敏：https://langfuse.com/changelog/2026-06-16-python-sdk-export-stage-masking

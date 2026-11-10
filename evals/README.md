# myflow-evals

MyFlow Agent 的独立黑盒评测工作区。它是仓库 uv 工作区的一员（根 `pyproject.toml` 中的 `evals`），仅通过 HTTP 与 MyFlow Gateway 和 Langfuse 通信，从不导入 `backend.app` 或 `agentkit`。它以固定的测试账户登录，驱动 Gateway 的 thread/run API，读取持久化的运行事件账本，用规则评测器为每个样本评分，并通过 Langfuse 实验写入分数。数据集构建器会读取每个账户自己的 MCP 工具、私有 Skills、知识文档以及失败/低分追踪，并据此合成确定性草稿。

## 安装与运行

```bash
uv sync --project evals
uv run --project evals myflow-evals --help
uv run --project evals pytest evals/tests -v
```

运行一次 CI 形态的实验（所有机密信息都必须已经在环境变量或机密文件中）：

```bash
uv run --project evals myflow-evals run \
  --dataset agent-eval/regression/v1 \
  --mode ci \
  --run-name "ci-$(date +%Y%m%d-%H%M)"
```

该控制台脚本返回进程退出码，而不是抛出异常：

| 退出码 | 含义 |
| --- | --- |
| `0` | 命令完成了它被要求做的事：一次运行通过了所有门禁、一次 build/ingest 完成，或 `--dry-run` 打印了它的计划。 |
| `1` | 实验失败：数据集条目缺失或出现意外条目、任务抛出异常、运行未以 `success` 结束、条目没有产生分数、引用评分为 0.0，或某个阈值未达标。 |
| `2` | 命令无法按配置运行：缺少必需的机密信息、模式未知、条目指定了未登录的账户、数据集为空，或 Langfuse/Gateway 不可用。 |

## 固定账户与身份

每个评测请求都以固定测试账户身份运行；不存在 PAT 或内部 token 捷径。至少配置一个账户，可以放在 `AGENT_USERS` 中，或放在通过 `--accounts-file` 传入的 JSON 文件中：

```json
{
  "analyst_a": {"username": "alice", "password": "<来自机密存储的密码>"}
}
```

对于每个账户，该包会：

1. 创建一个新的 `httpx.Client`（拥有自己的 cookie 容器，因此两个账户绝不会共享会话），并调用 Gateway 登录接口；
2. 要求 `needs_setup=false`——仍需修改密码的测试账户会报错，而不是静默使用默认值；
3. 读取 `csrf_token` cookie，在此后的每个请求中将其作为 `X-CSRF-Token` 发送，并调用 `GET /auth/me` 获取账户原始的 `users.id`；
4. 计算 Langfuse 哈希用户 ID `HMAC-SHA256(USER_ID_SALT, agent_user_id)[:32]`（小写十六进制），并用该值作为已登录会话的键。

`agent_user_id` 是经过 Agent 的存储安全规范化后的原始 `/me.id`（不安全字符变为 `-`，并附加 16 位十六进制 sha256 后缀以保持抗碰撞性；普通 UUID 原样通过）。原始 ID 只存在于内存中：每个数据集草稿都在 `metadata.user_id` 中携带哈希 ID，运行器通过精确的哈希 ID 匹配选择账户，原始 ID 和密码都绝不会写入数据集条目、Langfuse 追踪、日志行或 CLI 摘要。两个账户若哈希到同一 ID，会被拒绝，而不是覆盖。

## 机密信息

| 变量 | 用途 |
| --- | --- |
| `AGENT_API` | Gateway 基础 URL。文档中的形式包含 `/api`（例如 `http://127.0.0.1:8001/api`）；客户端接受带或不带该后缀的基础地址，并相应地追加 `/api` 或 `/api/v1`。 |
| `AGENT_USERS` | 账户标签 -> `{"username": ..., "password": ...}` 的 JSON 映射。每个固定账户一条凭据。 |
| `USER_ID_SALT` | 哈希用户 ID 的盐值。它必须与 Agent 追踪配置（`user_id_salt`）一致；更改它会改变所有哈希 ID，因此现有数据集必须重建/重新标注。 |
| `LANGFUSE_HOST`、`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY` | Langfuse HTTP API：数据集读写、实验，以及失败/低分追踪读取。 |

机密材料应存放在机密存储中（CI secrets、vault，或未跟踪的本地文件）。它绝不能出现在数据集条目、追踪、日志行、README、测试夹具或提交中。该包会从 dataclass repr 中排除密码，绝不回显 Langfuse 密钥，并且不保存任何 MCP 服务器凭据。

MCP 服务器不需要任何机密信息：构建只读取 Gateway 已存储的工具投影，从不直接调用 MCP 服务器。

机密文件是可选的便利方式：

- `--env-file PATH` 读取 `KEY=VALUE` 行（忽略注释和空行）。真实环境变量值优先于文件，因此 checkout 中的文件绝不会覆盖 CI 机密。
- `--accounts-file PATH` 替代 `AGENT_USERS` 作为凭据来源。

## 命令

三个子命令共享以下标志：

| 标志 | 含义 |
| --- | --- |
| `--dataset NAME` | 必填。Langfuse 数据集名称。 |
| `--limit N` | 正整数上限。`build` 限制每种来源类型的草稿数；`run` 在模式选择之后限制所选条目；`ingest` 限制打印的追踪数。 |
| `--dry-run` | 执行除写入之外的所有操作。各命令的具体区别见对应章节。 |
| `--accounts-file PATH` | 替代 `AGENT_USERS` 的固定账户机密文件。 |
| `--env-file PATH` | `KEY=VALUE` 设置文件；真实环境变量值优先。 |
| `--seed N` | 仅 `build`。知识库分块抽样的随机种子；同一种子的两次构建会选到同一批分块，条目 ID 本来就与抽样无关。 |

`build` 和 `ingest` 有意不提供 `--mode`/`--run-name`：这些标志只描述实验，一个无效标志会暗示并不存在的行为。

### `build`

```bash
uv run --project evals myflow-evals build \
  --dataset agent-eval/explore \
  --limit 100 \
  --dry-run
```

`build` 以每个固定账户登录，并以该账户身份读取：`GET /api/mcp-servers`，再对每个**生效**服务器发一次 `GET /api/mcp-servers/{id}/tools?include_schema=true`，一次拿到工具名、`callable`/`enabled` 标记和真实的 `inputSchema`（不再直接开 MCP 会话，也不需要任何服务器凭据）；`GET /api/skills`（仅私有 Skills）；`GET /api/knowledge/bases` -> `/documents` -> 每个文档随机抽取的三个分块中正文最长的一个（见下文）；以及仅在指定 `--include-traces` 时读取该账户自己的失败/低分 Langfuse 追踪。只有**生效**来源会被采样——一个已启用、活跃且可达的 MCP 服务器，其被 Gateway 投影标记为既可调用又已启用的工具，以及已启用且活跃的私有 Skills——因此草稿绝不会来自 Agent 无法访问的内容。每一次因生效性导致的跳过（服务器被禁用、非活跃或不可达；工具未被投影标记为可调用且已启用或没有可用的 `inputSchema`；私有 Skill 被关闭或非活跃）都会在构建报告中以警告打印，而因作用域、内置类别或缺少 `skill_id`/`name` 被丢弃的 Skill 则按设计保持静默。随后它为每个来源合成一个确定性草稿，并且除非指定 `--dry-run`，否则通过 Langfuse 将每个草稿 upsert 到数据集中。

追踪回放不声明任何期望（`expected_output` 为 `{}`），因此没有指标能给它评分，任何针对它的 `run` 都会因 `unscored` 条目失败。因此 `build` 默认排除追踪草稿，也完全不分页读取追踪；传入 `--include-traces` 可构建探索数据集。回归数据集（`agent-eval/regression/v1`）必须在不带该标志的情况下构建，这样它只包含可评分样本。

`--dry-run` 仍会登录，仍会读取账户数据——来源是按账户限定的，因此不读取就无法报告真实数量——但它不会向 Langfuse 写入任何内容，也不需要 Langfuse 凭据。它不是“无网络”模式。

条目 ID 按数据集划分命名空间：Langfuse 条目 ID 在一个项目内唯一，因此 `LangfuseDatasetWriter` 写入 `<数据集名称的 8 位十六进制摘要>-<草稿 ID>`。同一账户可以从相同来源构建 `agent-eval/regression/v1` 和 `agent-eval/explore` 而不会冲突，重建同一个数据集名称会复现其 ID（重建绝不会删除陈旧条目——新的冻结版本需要新的数据集名称）。

### `run`

```bash
uv run --project evals myflow-evals run \
  --dataset agent-eval/regression/v1 \
  --mode ci \
  --run-name "ci-$(date +%Y%m%d-%H%M)" \
  --max-concurrency 2
```

| 标志 | 含义 |
| --- | --- |
| `--mode {ci,full}` | 默认 `ci`。`ci` 执行固定的分层 30 条目子集；`full` 执行全部条目。 |
| `--run-name NAME` | 每次运行的标识。默认 `<mode>-<UTC timestamp>`。实验标签本身始终为 `agent-eval:<dataset>`。 |
| `--max-concurrency N` | 默认 `2`。并行 Agent 运行数；对 Gateway 和子代理运行时而言应保持较小。条目任务在 SDK 事件循环之外运行（`asyncio.to_thread`），因此它们确实会重叠执行，而不是串行化。 |
| `--limit N` | 在模式选择之后限制所选条目。 |

真正的 `run` 需要三个 Langfuse 变量以及 `AGENT_API` 和 `USER_ID_SALT`。它读取冻结的数据集，按模式选择条目，登录每个固定账户，并且只执行选中的条目——运行器将 `data=list(selected)` 传给 Langfuse，因此大型数据集绝不会被静默地全量运行。每个条目都有自己的 thread 和 run（`on_disconnect=continue`），然后运行器轮询运行的终态，读取运行事件账本和 thread checkpoint，将它们规范化为证据，用所有适用的规则评测器为该条目评分，并且只向 Langfuse 上传紧凑输出（答案、状态、追踪 ID、不透明证据 ID）。检索到的分块和引用载荷保留在运行器进程中。

`--dry-run` 打印数据集、模式、运行名称以及将选择多少条目；它不启动任何 thread，不写入任何分数，也不登录任何账户——但它仍会通过 Langfuse 读取数据集，因此需要 Langfuse 变量，而 `AGENT_API` 和 `USER_ID_SALT` 不需要（没有任何登录）。

会导致运行失败的情况：

- 计划中的任何数据集条目未出现在实验结果中，或结果中出现意外条目；
- 任务异常，或终态不是 `success`（`error`、`timeout`、`interrupted`）；
- 已完成的条目完全没有产生评测（`unscored`）；
- 任何 `citation_valid` 分数为 0.0，即使平均值通过；
- 任何 `forbidden_violation` 分数为 1.0，即使平均值通过；
- 某个样本本应需要检索子代理却从未到达它、加载了从未检索过的 Skill，或在没有 KB 检索证据的情况下回答了 KB 问题（`invalid_routing`），即使路由平均值仍高于其门禁；
- 一次运行中没有任何条目产生带阈值的分数（`no scorable samples`）；
- 某个带阈值的指标平均值未达到门禁。

### `ingest`

```bash
uv run --project evals myflow-evals ingest \
  --dataset agent-eval/explore \
  --limit 50
```

`ingest` 登录固定账户，按哈希用户 ID 读取每个账户失败或低分的 Langfuse 追踪，并为每个候选项打印一行：`<category>: <trace id> -> <question preview>`。它不写入任何内容：不创建数据集，也不上传条目。接受 `--dry-run` 标志只是为了与其他命令保持一致；它不改变任何行为，因为 `ingest` 从不写入。

## 评测环境前置条件

被测 Agent 必须按以下方式配置，证据才具有指标所声称的含义：

- `auth.enabled=true`——否则每个请求都会以固定 `default` 用户运行，按账户限定的数据过滤就失去意义。
- Agent 追踪使用 `user_id_mode=hashed`，盐值与 `USER_ID_SALT` 相同。
- `subagent_enabled=true`，且 `skill-finder` 和 `knowledge-researcher` 都可用。如果禁用子代理或某个检索子代理不可用，主 Agent 可以回退到直接 `knowledge_search`；这是评测配置上的退化，绝不能评分为通过。
- `skills.usage.prompt_top_k=0`——私有 Skills 不得进入静态 `<available_skills>` 提示索引。
- 评测样本不使用斜杠激活。每个 `expect_skill`/`expect_kb` 样本都必须通过 `task` 调用到达其子代理；静态 `load_skill` 或未经委托的直接 `knowledge_search` 会使样本无效，路由指标会将其评为 0.0 而不是 `N/A`。
- `run_events.backend=db` 或 `jsonl`，绝不能是 `memory`：事件账本必须在运行结束后仍然存在，因为证据是在运行完成后读取的。

## CI 与夜间任务

`.github/workflows/agent-eval.yml` 在拉取请求和夜间（UTC `0 18 * * *`）运行，作业超时为 45 分钟，Gateway/Langfuse 机密信息由 GitHub secrets 注入。数据集是冻结输入：工作流从不运行 `build`。

| 触发条件 | 命令 | 覆盖范围 |
| --- | --- | --- |
| 拉取请求 | `--mode ci --dataset agent-eval/regression/v1 --run-name ci-${GITHUB_SHA}` | 固定的分层 30 条目子集（在按 ID 排序的 `metadata.category` 分组间轮询；数据集较小时贡献每个条目）。规则指标是硬门禁。LLM 指标在 `ci` 模式下绝不会硬性失败。 |
| 夜间计划 | `--mode full --dataset agent-eval/regression/v1 --run-name nightly-${GITHUB_SHA}` | 每个数据集条目。规则指标是硬门禁；一旦至少有 20 个已评分样本，LLM 指标可能成为硬门禁。 |

零个已评分样本的门禁指标会报告为 `insufficient`，不会使运行失败；它也不计入通过。不要把“PASS”理解为“每个指标都已被测量”——请检查 `samples=` 列。

目前 `THRESHOLDS` 中的每个条目都是规则指标（`kind: rule`）。运行器已经实现了 LLM 指标策略——已评分样本少于 20 个时，LLM 指标为 `insufficient`；达到至少 20 个时，它在 `full` 模式下可以硬性门禁，而在其他模式下保持 `soft`——但尚未有任何 LLM/DeepEval 指标接入 `THRESHOLDS`。

## 结果：门禁、分数与不完整实验

成功的运行会为每个带阈值的指标打印一行摘要，然后打印判定结果：

```text
dataset: agent-eval/regression/v1
run: ci-20261109 (mode ci)
items: 30 of 30 returned
scorable items: 30
retrieval_recall@5: 0.980 min 0.95 samples=30 PASS
tool_hit: 0.933 min 0.90 samples=30 PASS
args_accuracy: n/a min 0.85 samples=0 insufficient scored samples (LLM metrics need 20)
...
result: PASS
experiment: https://langfuse.example/project/p/experiments/<id>
```

`PASS`/`FAIL` 将指标所有非 `None` 样本的平均值与该阈值比较（`min`：平均值必须至少达到阈值；`max`：至多）。`None` 表示“该样本无法在此指标上评分”，不计为零。`insufficient` 表示没有样本为该指标产生数值分数（或某个 LLM 指标少于 20 个样本）；渲染器对两种情况使用同一句面向 LLM 的说明，因此把 `samples=0` 理解为“没有任何内容为该指标评分”。

指标目录（合并后的 `THRESHOLDS`）：

| 分数 | 层级 | 读取的证据 | 门禁 |
| --- | --- | --- | --- |
| `retrieval_recall@5` | T1 工具检索 | `retrieval.tool` -> `tools[].call_name`，前 5 个 | `>= 0.95` |
| `tool_hit` | T2 工具选择 | 业务工具调用，`tool_execute` 已解包，排除基础设施工具 | `>= 0.90` |
| `args_accuracy` | T2 参数 | 用 `expected_calls[].check_keys` 对照实际调用参数 | `>= 0.85` |
| `extra_calls_ratio` | T3 轨迹 | 业务工具调用 / 期望调用 | `<= 1.5` |
| `skill_routing` | K0 路由 | 带 `subagent_type="skill-finder"` 的 `task` 调用 vs `expect_skill` | `>= 0.95` |
| `skill_recall@5` | K1 Skill 检索 | `retrieval.skill` -> `skills[].name`，前 5 个 | `>= 0.95` |
| `skill_hit` | K2 Skill 选择 | `load_skill` 名称：每个期望的 Skill 都已加载，且只加载已检索的 Skill | `>= 0.90` |
| `kb_routing` | R0 路由 | 带 `subagent_type="knowledge-researcher"` 的 `task` 调用 vs `expect_kb` | `>= 0.95` |
| `kb_recall@5` | R1 KB 检索 | `retrieval.kb` -> `items[].document_id`，前 5 个 | `>= 0.85` |
| `citation_valid` | R3 引用 | 将 `additional_kwargs.knowledge_citations` 与本运行检索到的分块对照 | `>= 0.95`；任何 0.0 都会使整个运行失败 |
| `forbidden_violation` | S1 安全 | 业务工具调用 vs `forbidden_tools`；1.0 表示违规 | `<= 0.0`；任何 1.0 都会使整个运行失败 |

当运行的任何 `retrieval.kb` 行被截断时，`citation_valid` 返回 `None`：预算隐藏了分块，因此看起来伪造的引用可能只是不在可见范围内。仅记录、不设数值门禁的指标有：`retrieval_mrr`、`skill_mrr`、`kb_mrr`、`skill_extra`（越低越好）和 `no_tool_ok`（仅对 `metadata.category` 为 `no_tool` 的条目评分）。

一个完成时完全没有评测的条目是 `failed item`，状态为 `unscored`，而不是缺失条目：该设计将未评分样本计为失败实验。这对追踪回放草稿很重要，它们的 `expected_output` 为 `{}`：它们从设计上就不适用于任何指标，因此包含它们的数据集不能进入回归运行。`build` 会排除它们，除非传入 `--include-traces`；请将追踪回放放入探索集，或在将其作为回归运行前赋予真实期望。

一个声明了 `expect_skill`/`expect_kb` 但未通过该子代理就作答的条目——或加载了从未检索过的 Skill，或在没有 KB 检索证据的情况下回答 KB 问题的条目——是 `failed item`，状态为 `invalid_routing`。路由有单独分数，但 30 个样本中出现一次直接回退，平均值仍会高于 0.95 门禁，因此运行器会将该样本判为失败。

`scorable items: N` 统计产生至少一个带阈值指标分数的条目。当 `N = 0` 时，即使每个条目都已完成，运行也会以 `no scorable samples: no item produced a thresholded metric score` 失败：空门禁列表不是干净的结果。

不完整实验会在任何门禁摘要之前报告：

```text
error: experiment did not return every dataset item it planned; missing: kb-1a2b3c4d5e6f7a8b; failed: kb-1a2b3c4d5e6f7a8b=exception (RuntimeError: ...)
```

CLI 将该行打印到 stderr 并以 `1` 退出。调查其中列出的条目 ID：`missing` 条目是已计划但从未返回的，`unexpected` 条目是未计划的，`failed` 条目会列出其状态（`exception`、`unscored` 或非成功的运行状态）。不要只重跑幸存条目来获得绿色结果——下一次完整运行必须显示每个计划条目都已返回。

### 实验元数据

每次实验都会在运行上携带这些字符串值，因此两次实验无需猜测改了什么就能比较：

| 键 | 值 |
| --- | --- |
| `mode` | `ci` 或 `full` |
| `max_concurrency` | 运行使用的并发数 |
| `dataset` | 数据集名称 |
| `package_version` | 已安装的 `myflow-evals` 版本（回退为 `0.1.0`） |
| `git_sha` | 存在时使用 `GIT_SHA`/`GITHUB_SHA`，否则为 `unknown` |
| `salt_fingerprint` | `sha256(USER_ID_SALT)` 的前 8 位十六进制；绝不是盐本身 |

实验标签本身是稳定的（`agent-eval:<dataset>`），因此 `ci-<sha>` 和 `nightly-<date>` 运行会落入同一实验并保持可比。

## 包布局

| 模块 | 职责 |
| --- | --- |
| `accounts.py` | 加载固定账户（`AGENT_USERS` 或机密文件），每个账户登录一次，计算 Langfuse 哈希用户 ID。 |
| `api.py` | Gateway thread/run/event 客户端（`AgentApi`、`RunSnapshot`）。 |
| `evidence.py` | 以纯函数方式将已完成的运行规范化为 `RunEvidence`（答案、工具调用、子代理、检索、引用）。 |
| `evaluators.py` | 纯逐样本规则指标，以及 `RULE_EVALUATORS`/`THRESHOLDS` 注册表。 |
| `build_dataset.py` | 数据来源、确定性草稿合成（`build_account_items`）、可选追踪获取，以及 `DatasetWriter` 抽象接口。 |
| `runner.py` | 实验执行、完整性门禁、紧凑证据交接、指标门禁。 |
| `cli.py` | `myflow-evals build` / `run` / `ingest` 命令行及其退出码契约。 |

## 从固定账户构建草稿

`load_build_sources(session, limit=...)` 以该账户身份读取：

- `GET /api/mcp-servers`，然后——对每个**生效**服务器——一次 `GET /api/mcp-servers/{id}/tools?include_schema=true`，它同时给出工具列表、`callable`/`enabled` 标记和逐字的 `inputSchema`；这是工具的唯一来源，包自身从不打开 MCP 会话；
- `GET /api/skills`，保留 `scope == "private"`，丢弃 builtin/public Skills 以及任何缺少 `skill_id`/`name` 的 Skill；
- `GET /api/knowledge/bases` -> `/documents` -> 每个文档的分块抽样（随机三个，取最长，见下条）；
- 账户自己的失败或低分 Langfuse 追踪，仅在调用方要求时读取（`include_traces=True`，即 `--include-traces`）；见下文。

只有生效来源会成为样本，与 Agent 实际能路由到的内容保持一致：

- **MCP 服务器**：摘要必须表明 `enabled: true`、`status: "active"` 和 `reachable: true`（agentkit 的 `is_active`）。缺失标志不能视为 `true`，因此被禁用、仍待处理、出错、不可达或无标志的服务器会被跳过，并给出警告，指出其名称和原因。transport 和 url 不再参与判断：构建不调用服务器，只读取 Gateway 已存储的内容。
- **MCP 工具**：`GET /api/mcp-servers/{id}/tools?include_schema=true` 的每一行就是一个候选工具；只有 `callable: true` **且** `enabled: true`、`call_name` 是 Agent 可回调的、并且该行带有对象形式的 `input_schema` 时才会被采样（参数必须来自真实 schema，没有 schema 就只能猜）。缺少 `call_name`、未标记可调用/已启用、调用名不合法或缺少对象形式 `input_schema` 的行会被跳过并给出警告；`/tools` 调用失败会跳过整个服务器并给出警告（无法确认生效性；它绝不会中止构建）。
- **Skill**：私有 Skill 必须 `enabled: true`，当行带有 `status` 时还必须 `status == "active"`；被关闭或非活跃的 Skill 会被跳过，并给出警告指出其名称。
- **知识库分块**：不再固定取第 0 个分块，而是在该文档的分块中随机抽取三个不同位置，取正文最长的那个；比较用 `len(content.strip())`，并列时取 `chunk_index` 最小的，空白分块永不入选（即使它的原始字符串最长）。文档只有 1 个分块时复用计数请求，2-3 个分块时整页读取，更多时发 1 次计数请求加 3 次单分块请求，因此每个文档最多 4 次分块请求。抽样用注入的 `random.Random`：`build --seed N` 让抽号可复现，而条目 ID 由文档 ID 推导、与抽号无关。某个文档的分块页读取失败只跳过该文档并给出警告，不会中断构建；`count` 缺失或格式非法时给出“分块计数缺失或非法”的警告，而不是误报成没有可读分块。

transport 不影响采样，只要求服务器生效。每一次生效性跳过和每一个不可用的本地来源——未生效服务器、`/tools` 调用失败的服务器、未标记 `callable`/`enabled` 或没有可用 `input_schema` 的工具、未生效 Skill、没有可读分块的文档、不可用的 Langfuse——都会作为警告记录在 `BuildSources.warnings` 上，从而出现在构建报告中；一个损坏来源只损失它自己的条目，而不会使构建失败。因其他原因被丢弃的行按设计保持静默：public、builtin 类别或缺少 `skill_id`/`name` 的 Skill 从来不是该账户可用的样本，因此不会报告。导致账户数据不可用的 Gateway 故障仍会抛出异常。

`build_account_items(session, sources, limit=...)` 是纯函数：没有 I/O、没有时钟、没有随机性。它为每个来源生成一个带确定性 ID 的草稿（`stable_item_id(kind, source_id, ordinal, hashed_user_id)`），因此重建是幂等的，对同一账户重复运行绝不会重复条目。如果某个工具调用名是 Agent 无法回调的——带点的上游名称，或超出提供方长度限制——则跳过并给出警告，不产生草稿，因为用它构建的样本永远无法通过。

### 草稿结构

```json
// 工具
{"id": "tool-<16 hex>", "input": {"query": "Use the <tool> tool: <description>."},
 "expected_output": {"expected_tools": ["mcp__<server>__<tool>"],
   "expected_calls": [{"tool": "mcp__<server>__<tool>", "args": {}, "check_keys": []}],
   "forbidden_tools": [], "expect_kb": false, "expect_skill": false, "judge_rule": "..."},
 "metadata": {"user_id": "<hashed>", "source_id": "mcp__<server>__<tool>", "source_kind": "tool",
   "category": "single_tool", "source": "synthetic_from_tools", "tool_schema_hash": "<16 hex>"}}

// 私有 Skill
{"input": {"query": "Use the <skill> skill: <description>."},
 "expected_output": {"expect_skill": true, "expected_skills": ["<skill>"], "skill_rules": "",
   "expect_kb": false, "expected_tools": []},
 "metadata": {"user_id": "<hashed>", "source_id": "<skill_id>", "source_kind": "skill",
   "category": "skill_single", "source": "synthetic_from_skill", "skill_commit": "<sha or null>"}}

// 知识库文档
{"input": {"query": "According to <filename>, what are the key points?"},
 "expected_output": {"expect_kb": true, "expected_doc_ids": ["<document_id>"],
   "reference_answer": "<采样的最长分块正文>", "key_points": [], "expect_refusal": false},
 "metadata": {"user_id": "<hashed>", "source_id": "<document_id>", "source_kind": "knowledge",
   "category": "kb_single_hop", "source": "synthetic_from_docs", "document_id": "...",
   "chunk_id": "<document_id>:<chunk_index>", "knowledge_base_id": "...", "kb_version": "..."}}

// 回放的在线追踪
{"input": {"query": "<the trace's question>"}, "expected_output": {},
 "metadata": {"user_id": "<hashed>", "source_id": "<trace id>", "source_kind": "trace",
   "source": "online_trace", "trace_id": "<trace id>", "category": "trace_replay"}}
```

关于这些结构的说明：

- 工具的 `args` 来自真实 `inputSchema`：仅必填属性；存在 `enum` 时取第一个值，否则取类型占位符，并递归进入必填对象一层。`check_keys` 是已填充必填属性的排序列表。`forbidden_tools` 保持为空：安全期望不做猜测。
- 知识条目在元数据中携带 `document_id`/`chunk_id` 来源信息，但只输出 `expected_doc_ids`——chunk ID 是检索器实现细节。`kb_version` 为 `<base_id>:<chunk_count>`，因为知识 API 不暴露版本字符串；当文档重新分块时它仍会变化。
- 追踪草稿完全没有期望，因此评测器会将其报告为不适用（`None`），而不是评分——这也意味着它对完整性门禁是 `unscored`，所以请将回放排除在回归运行之外。
- `metadata.user_id` 始终是 Langfuse 哈希用户 ID（`HMAC-SHA256(USER_ID_SALT, agent_user_id)[:32]`）。原始 `/me.id`、用户名和密码绝不会进入草稿、日志或报告。

### 仅确定性合成

构建在真实数据上使用模板；此路径没有 LLM 调用或可选附加组件，因此构建不产生费用，并且对相同账户数据可逐字节复现。目前产生的类别有 `single_tool`、`skill_single`、`kb_single_hop`，以及仅在 `--include-traces` 时的 `trace_replay`。

设计表中的其余类别——`multi_tool`、`similar_tools`（硬负样本）、`missing_params`、`similar_skills`、`skill_with_tools`、`kb_multi_hop`、`kb_unanswerable`，以及重写/人工审核的问题——需要 LLM 合成或人工审核，属于未来工作。在此之前，构建出的数据集类别组合比设计目标比例更窄，模板问题也只是占位符，而非真实的用户措辞。

### 限制、报告与试运行

`--limit`（`limit` 参数）限制每种来源类型的草稿数：工具、私有 Skills、知识文档和追踪分别独立设上限。它限制工作量，而不是真实性：只有前 `limit` 个文档会获取分块，但每种类型仍会完整计数（追踪会在有界分页保护下翻页到完成，因此连追踪计数也是真实的），所以报告中的“M sources”是该账户实际拥有的来源数，而不是被上限截断的样本数。没有 `--include-traces` 时，根本不会读取追踪类型，报告会显示 `trace: 0 drafts from 0 sources`。返回的 `BuildReport` 会记录这两个计数并渲染如下行：

```text
dataset: myflow-eval
limit: 2 per kind
mode: dry-run (writes nothing)
tool: 2 drafts from 37 sources
skill: 2 drafts from 11 sources
knowledge: 2 drafts from 120 sources
trace: 2 drafts from 4 sources
total: 8 drafts
warning: skipped MCP server 'legacy_db': status=pending is not active
```

写入通过 `DatasetWriter.upsert_items(dataset_name, items)` 完成，由 `cli.py` 中的 `LangfuseDatasetWriter` 实现。`write_drafts(..., dry_run=True)` 返回 0 且从不调用 writer，因此试运行无法创建或更改 Langfuse 数据集。`myflow-evals build` 是唯一写入数据集条目的命令；`run` 将数据集视为冻结输入，`ingest` 只打印。

## 已知限制

- 确定性合成仅覆盖 `single_tool`、`skill_single`、`kb_single_hop` 和 `trace_replay`。不会生成 `multi_tool`、`similar_tools`、`missing_params`、`similar_skills`、`skill_with_tools`、`kb_multi_hop`、`kb_unanswerable` 以及重写问题。
- K3 `skill_adherence`、R4 `refusal_ok`、Faithfulness、`answer_correctness`、`answer_quality`、AnswerRelevancy、ContextualPrecision/Recall/Relevancy 以及所有其他 DeepEval 指标都未实现。当前每个 `THRESHOLDS` 条目都是规则指标。
- 不存在基于 LLM 的数据集合成和可选 DeepEval 附加组件；也没有人工 Golden set。
- `ingest` 会打印候选的失败/低分追踪，但不会写入探索数据集；将其提升为数据集仍需手动操作。
- 工具只来自 Gateway 存储的投影。未生效服务器（未启用、非 `active` 或不可达）、`/tools` 调用失败的服务器、Gateway 投影未标记为可调用**且**已启用或缺少对象形式 `input_schema` 的工具，以及被关闭或非活跃的私有 Skills 都会被跳过并给出警告。
- 证据携带运行追踪 ID，只是因为运行器将 `RunSnapshot.trace_id` 传给 `collect_evidence`；其他调用方必须显式传入，否则证据的 `trace_id` 为 `None`。
- 不会对用户 ID、权限、跨用户隔离或长期记忆指标评分；`metadata.user_id` 仅作为账户选择/来源键。
- 内置 Skills 和斜杠激活被排除在样本之外。只采样 `scope=private` 的 Skills。
- 检索指标只在前五个结果上比较名称/ID；增加或删除来源文档会使 `expected_doc_ids` 失效。仅重新分块不会，因为只比较文档 ID。
- `retrieval.*` 事件只携带模型看到的内容：查询、最终卡片/分块、通道、警告以及 `truncated` 标志。它们不携带 `task_query`、`domain_keywords` 或工具 `description`，因此需要这些信息的指标必须从别处读取；账本约定保持合并状态。
- 数据集版本一旦冻结就不可变：重建同名会 upsert 条目，但绝不会删除来源已消失的条目。请在新的数据集名称下冻结新版本（例如 `agent-eval/regression/v2`）。
- `trace_replay` 条目从设计上不可评分，因此除非传入 `--include-traces`，`build` 会排除它们，而包含它们的数据集无法通过运行。

# 用户隔离技能、安全扫描与生效设计

> 状态：已接受（v2，2026-11-02；2026-11-03 修订）
> 2026-11-03 修订：技能启用开关落到数据库、移除私有技能 `skills_prefs.json`、
> `search_and_load_skills` 拆成 `search_skills` / `load_skills`、Redis ZSET
> 记录使用次数并驱动 system prompt top-10。
> 本版取代 2026-10-23 版“私有创作 → 管理员审核 → 公共技能”的模型。
> 新模型只有**用户私有技能**：不设管理员审批、不做公共技能发布、不允许跨用户可见。
> 不在范围：技能市场、团队/组织共享、跨仓库合并、MCP 与知识库改造。

## 1. 目标与非目标

### 1.1 目标

1. 每个登录用户创建、保存、扫描、启用和删除的技能只属于自己；其他用户即使知道
   skill_id 也读不到正文、附件、版本、扫描结果或容器路径。
2. 用户点击“保存”后得到的是**不可变 Git 草稿版本**：暂存内容 commit/push 到
   该用户的草稿分支，并写入一行 `skill_versions`；保存不进入运行时、提示词索引
   或向量索引。
3. 用户点击“安全扫描”时，后台以 **Python 包 import 的方式**调用
   `cisco-ai-skill-scanner`，同时运行**规则分析**和**默认模型的 LLM 语义分析**，
   扫描该版本不可变的 Git commit 内容；扫描结论持久化到数据库并回显到编辑器。
4. 用户点击“生效”后，只有当前最新已保存版本存在一次通过的扫描结果时，服务端才
   把该版本设为用户的 active 版本：运行时快照立即可见，并向 outbox 写入索引事件，
   由后台 worker 在 Milvus 的 `myflow_skills_active` collection 中构建/更新该用户的
   向量行。
5. 没有 active 版本，或用户关闭启用开关的技能，不进入 `<available_skills>`、
   `search_skills` / `load_skills`、slash 技能或沙箱私有挂载。草稿仍可在“我的技能”中编辑。
6. 技能发现拆成两段：`search_skills` 只返回 name / description / skill_id；
   `load_skills` 按 skill_id 加载正文。前者只给 skill-finder，后者是系统内置工具。
7. `load_skills` 同时累加使用次数；PostgreSQL 保存持久计数，Redis ZSET 保存每个用户
   active 技能的使用排名，系统提示词超过 10 个私有技能时用 Redis 排名选择 top-10。

### 1.2 非目标

- 管理员审核、驳回、下架、发布、回滚、公共版本指针。
- 用户之间的技能共享、克隆或引用。
- 自动修复扫描发现；扫描只做检测，不执行技能脚本。
- 二进制大文件、Git LFS、跨仓库依赖。
- 扫描结果作为安全认证。扫描器是 best-effort 检测，无发现不代表绝对安全。

## 2. 可见性与状态机

### 2.1 可见性

| 对象 | 可见范围 |
|---|---|
| skills / skill_versions / skill_draft_files | 仅 owner_user_id 对应用户 |
| skill_security_scans | 仅 owner_user_id 对应用户 |
| skill_private_active | 仅 owner_user_id 对应用户 |
| 内置技能 | 所有登录用户只读，不进入用户创作数据 |

权限使用现有 scope：

- `skills:read`：列出/读取自己的技能、运行检索；
- `skills:write`：创建、编辑、保存、扫描、生效、删除自己的技能；
- 不再存在 `admin` 专属的技能审核权限。

对非所有者的读取统一返回 404 `skill_not_found`，不泄露技能是否存在。

### 2.2 生命周期

~~~text
创建草稿（暂存表）
   │ PUT draft/file、上传附件
   ▼
保存 ── Git commit ──▶ refs/heads/myflow/drafts/<user>
   │                    + skill_versions(v1)（不可变版本）
   │ 用户点击“安全扫描”
   ▼
skill_security_scans（绑定 version_id + package_sha256）
   │
   │ 用户点击“生效”
   ▼
校验最新版本 && 扫描通过
   │
   ├── skill_private_active = version
   ├── outbox: upsert_active
   └── runtime 快照刷新
           │ worker
           ▼
      Milvus myflow_skills_active（private:<owner>:<skill_id>）

保存 v2（扫描/生效前）
   │
   ├── active 仍指向 v1（v1 继续可用）
   └── v2 是草稿；必须重新扫描并生效
~~~

状态值（API 对最新版本派生，不新增可变状态列）：

- `draft`：没有已保存版本（仅有暂存文件）；
- `unscanned`：已保存版本，但没有该版本的成功扫描；
- `scan_failed`：最新扫描失败（发现 HIGH/CRITICAL，或扫描器执行失败）；
- `scanned`：最新扫描通过，但尚未生效；
- `active`：active 指针指向最新版本；
- `builtin`：内置技能。

“生效”只允许最新版本；一旦保存了 v2，v1 的扫描不能用于启用 v2。

## 3. 总体架构

~~~text
浏览器“我的技能 / 编辑器”
   │ 保存 / 扫描 / 生效
   ▼
Gateway /api/skills/*
   │
   ├── SkillAuthorService：所有权、保存提交、安全扫描、生效
   │        │
   │        ├── Git：保存时 commit/push 到 refs/heads/myflow/drafts/<user-id>
   │        ├── PostgreSQL：skills / skill_versions / 扫描 / active / outbox
   │        └── cisco-ai-skill-scanner（import 调用）
   ▼
PostgreSQL
  skills / skill_versions / skill_draft_files
  skill_private_active / skill_security_scans / skill_index_outbox
  skill_catalog_epoch / audit
   │ outbox
   ▼
Milvus myflow_skills_active：每个 owner 的 active 技能一行
   │ 检索时按 owner_user_id 过滤，并回 PostgreSQL 确认 active 指针
   ▼
提示词索引 / search_skills + load_skills / 私有沙箱挂载
~~~

### 3.1 不可破坏的约束

- 服务端是唯一写 Git 的一方；用户不持有 Git 凭据，也不能提供 Git 参数。
- **保存即写 Git**：每次保存都把暂存内容 commit/push 草稿分支，并记录一行不可变
  `skill_versions`；保存不会自动激活。
- **生效不写 Git**：生效只把已经保存、扫描通过的版本设为 active，不再产生 Git 操作。
- 安全扫描只读取该版本不可变的 Git commit 缓存，不读取可变的编辑器暂存表。
- 安全扫描结果是扫描那一刻的不可变快照；编辑/保存新版本后旧扫描不再适用于生效。
- Git 不可达时保存失败，数据库不写版本行；暂存内容保留，用户可重试保存。
- active 指针是唯一运行时可见性来源；没有 active 指针就没有索引、提示词和挂载。
- Milvus 只是可重建投影：任何命中必须回到 PostgreSQL 按 owner 和 active 版本确认。
- 技能容器路径只返回给所有者；不向其他用户暴露 host cache 路径。

## 4. Git 写入与不可变版本

### 4.1 Git 分支（保存时写入）

~~~text
refs/heads/myflow/drafts/<user-id>
  users/<user-id>/<skill_id>/SKILL.md
  users/<user-id>/<skill_id>/scripts/...
  users/<user-id>/<skill_id>/templates/...
  users/<user-id>/<skill_id>/examples/...
~~~

- 不再有 `main` 公共目录、`public/` 路径或公共发布 commit。
- 每个用户一条草稿分支；技能目录使用内部 UUID，改名不影响路径。
- 普通用户不接触 Git 凭据；只有 `SkillAuthorService.save` 会调用 Git。
- 保存使用进程内锁；Git 操作不放在数据库事务里。
- 保存步骤：读取暂存文件 → 校验路径/大小/文件数 → 临时工作区 commit/push →
  写入 `skill_versions` 并记录 `draft_ref / draft_commit / skill_path`。
- 相同 package_sha256 复用既有版本，不重复升版，也不重复产生 commit。
- Git 失败时保存返回错误，数据库不写版本行；暂存表内容保留，用户可重试保存。

### 4.2 暂存表与不可变版本

- 未点击保存的编辑写在 `skill_draft_files`，只是编辑器缓冲区。
- 点击保存时把暂存文件提交为 Git commit，并写入一行不可变的 `skill_versions`。
- 保存后暂存表保留为当前工作副本，用于继续编辑；它不是运行时内容。
- 扫描从版本的 Git commit 缓存物化到临时目录；暂存表不进入扫描或 prompt。
- 删除技能时删除暂存文件、active 指针并写 `delete_active` outbox；版本和扫描历史
  保留用于审计，除非运维显式清理。

## 5. 数据模型

### 5.1 skills

| 字段 | 类型 | 说明 |
|---|---|---|
| id | String(64) PK | 内部 UUID |
| owner_user_id | String(64) | 所有者，所有查询强制过滤 |
| private_name | String(128) | 同一 owner 内唯一 |
| enabled | Boolean | 用户开关，创建时默认 true；仅 active 技能可切换 |
| load_count | Integer | `load_skills` 成功加载次数；内置技能不计数 |
| created_at / updated_at | DateTime(tz) | 时间 |
| deleted_at | DateTime(tz) nullable | 软删除 |

约束：`UNIQUE(owner_user_id, private_name) WHERE deleted_at IS NULL`。
不再有 `public_name`，不占用全局名字空间。

### 5.2 skill_versions

内容不可变版本。保存即创建 Git commit 与版本行，但不会自动激活。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | String(64) PK | version_id |
| skill_id | String(64) FK | 所属技能 |
| skill_version | String(32) | v1、v2… |
| version_seq | Integer | 每个 skill 单调递增 |
| package_sha256 / skill_md_sha256 | String(64) | 包与入口 hash |
| metadata_json | JSON | frontmatter 快照 |
| draft_ref / draft_commit | String nullable | 保存时写入的 Git 分支与 commit |
| skill_path | String(1024) | 保存时写入的 Git 内 SKILL.md 路径 |
| index_status | String(16) | unindexed / pending / ready / failed |
| created_at | DateTime(tz) | 时间 |

约束：`UNIQUE(skill_id, skill_version)`、`UNIQUE(skill_id, package_sha256)`。
版本内容不可更新；`draft_ref / draft_commit / skill_path` 在保存时写入，
`index_status` 是投影状态。

### 5.3 skill_private_active

每个技能最多一个 active 版本，仅在用户点击生效且扫描通过后写入。

| 字段 | 类型 | 说明 |
|---|---|---|
| skill_id | String(64) PK | 技能 |
| owner_user_id | String(64) | 冗余过滤字段 |
| version_id | String(64) FK | 当前 active 版本 |
| updated_at | DateTime(tz) | 时间 |

### 5.4 skill_security_scans

一次安全扫描的不可变结果。每次点击扫描新增一行，不做覆盖。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | String(64) PK | 扫描 ID |
| skill_id | String(64) FK | 技能 |
| version_id | String(64) FK | 被扫描版本 |
| owner_user_id | String(64) | 扫描发起人/所有者 |
| package_sha256 | String(64) | 扫描时版本包 hash，防止张冠李戴 |
| status | String(16) | completed / failed |
| is_safe | Boolean | 扫描器结论；failed 时为 false |
| max_severity | String(16) | SAFE/INFO/LOW/MEDIUM/HIGH/CRITICAL |
| findings_count | Integer | 发现数量 |
| findings_json | JSON | 脱敏后的发现列表（规则、严重度、文件、行号、标题、建议） |
| scanner | String(64) | `cisco-ai-skill-scanner` |
| scanner_version | String(64) nullable | import 到的包版本 |
| duration_ms | Integer | 扫描耗时 |
| error | Text nullable | 扫描器异常的脱敏摘要，绝不存技能正文 |
| created_at | DateTime(tz) | 时间 |

索引：`(skill_id, created_at)`、`(version_id, created_at)`。

### 5.5 skill_draft_files / skill_index_outbox / skill_audit_events / skill_catalog_epoch

- `skill_draft_files` 保持现有形状：`(skill_id, path)` 主键、内容字节、hash、大小、mode。
- outbox 只保留 private 投影：`projection_key = private:<owner>:<skill_id>`，
  op 仍是 `upsert_active` / `delete_active`。
- 审计动作改为：`skill.create / skill.save / skill.scan / skill.upload / skill.activate /
  skill.delete / skill.index_reindex`。不再有 submit/approve/reject/unpublish。
- `skill_catalog_epoch` 保留，用于多 worker 快照失效。

## 6. 安全扫描

### 6.1 调用方式

服务层新增 `SkillSecurityScanner` 协议和 `CiscoSkillScanner` 适配器。适配器在
**真正扫描时**懒 import，避免网关启动就加载 YARA/Go CEL：

~~~python
from skill_scanner import scan_skill as cisco_scan_skill
from skill_scanner.core.analyzer_factory import build_analyzers
from skill_scanner.core.scan_policy import ScanPolicy

analyzers = build_analyzers(
    ScanPolicy.default(),
    use_llm=True,
    llm_model=default_model.model,
    llm_api_key=default_model.api_key,
    llm_base_url=default_model.base_url,
    llm_provider="openai-compatible",
)
result = cisco_scan_skill(skill_directory, analyzers=analyzers)
data = result.to_dict()
~~~

- 包依赖：`cisco-ai-skill-scanner`（PyPI），import 名是 `skill_scanner`。
- 放在 agentkit 的 `skills` extra 中；未安装时扫描接口返回 503
  `skill_scanner_unavailable`，技能编辑/保存仍可用。
- 一次扫描**同时启用规则分析和 LLM 语义分析**：规则包含 static / bytecode /
  pipeline / correlation，LLM 由扫描器的 `LLMAnalyzer` 执行。
- LLM 使用部署默认模型：`agents.default_model`（未配置时取 `models[0]`），复用该
  模型条目的 `model / base_url / api_key`；myflow 的默认模型走 OpenAI 兼容路由，
  `use` 明确是 Anthropic / Ollama 时映射到扫描器对应 provider。
- 没有默认模型时不静默降级为纯规则扫描：扫描接口返回 503，提示先配置
  `agents.default_model`。
- LLM 分析器没有运行（`analyzers_used` 不含 `llm_analyzer`）或自身报错时，整次扫描
  记为 `failed`，不接受“只有规则通过”的结论。
- 扫描包含对模型提供方的网络调用（会发送该版本的技能内容）；它仍是同步阻塞工作，
  放在 `asyncio.to_thread`，不阻塞事件循环，也不执行技能中的任何脚本。
- 扫描目标目录由 `SkillContentStore` 从版本的不可变 Git commit 物化到
  `tempfile.TemporaryDirectory`：逐文件校验 `path`、写入字节和可执行位，扫描结束立即删除。
- 扫描前校验版本存在、属于该用户、`package_sha256` 与数据库一致、`draft_commit`
  存在；扫描器接收到的目录只包含该版本的不可变内容。

### 6.2 结果归一化

- 保存扫描器返回的 `is_safe`、`max_severity`、`findings_count` 和
  `findings`；每条 finding 只保留 `rule_id/title/severity/category/file_path/
  line_number/remediation`，不保存扫描器可能附带的完整文件内容或密钥。
- `is_safe` 由扫描器定义（无 HIGH/CRITICAL 发现）决定；“生效”默认要求 `is_safe`。
- 扫描器抛异常、import 失败或 LLM 阶段失败：
  - import 失败 / 没有默认模型：不写扫描行，返回 503；
  - 扫描执行失败：写 `status=failed` 的扫描行，前端展示错误，允许用户重试；
  - LLM 分析器未运行或报错：同样写 `status=failed`，不能只凭规则结果判定通过。
- 每次扫描都针对最新已保存版本；HTTP 接口不接受版本参数，服务层也拒绝非最新版本。

### 6.3 生效门禁

`POST /api/skills/{skill_id}/activate`：

1. 校验 owner、技能未删除；
2. 取最新已保存版本；请求中的 version_id 必须等于它；
3. 取该版本最新一次扫描；必须 `status=completed && is_safe=true`；
4. 在同一 PG 事务中重新校验扫描行、版本包 hash、active 指针和 Git commit，写
   `skill_private_active`、outbox upsert、审计；
5. 路由刷新进程级快照；worker 在 Milvus 构建/更新该用户的向量行；
6. 返回 active 版本和 `index_status`。若投影未配置，`indexed=null` 且
   `index_status=pending`，但运行时快照仍立即生效。

生效不写 Git：commit/push 已在保存阶段完成。扫描未通过或版本不是最新时返回 409，
不改变 active 指针。

扫描通过但用户尚未点击生效：不进入运行时。

## 7. HTTP API

### 7.1 作者接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/skills/mine | 当前用户全部技能（含草稿、active、扫描摘要） |
| POST | /api/skills | 创建草稿 |
| POST | /api/skills/{id}/draft/open | 打开/恢复暂存 |
| GET | /api/skills/{id}/draft | 技能 + 文件树 + 最新版本 + 最新扫描 |
| GET/PUT/POST/DELETE | /api/skills/{id}/draft/file(s) | 暂存文件读写/上传/删除 |
| POST | /api/skills/{id}/draft/save | Git commit + 不可变版本，不生效 |
| GET | /api/skills/{id}/versions | 版本历史 |
| POST | /api/skills/{id}/scan | 扫描最新已保存版本（Git commit 快照） |
| GET | /api/skills/{id}/scans | 扫描历史（最多 N 条） |
| POST | /api/skills/{id}/activate | 校验扫描 → 生效并写索引 outbox |
| DELETE | /api/skills/{id} | 删除私有技能（含 active 和索引删除事件） |

不再提供 `/submit`、`/withdraw` 或任何 `/api/admin/skills/*` 审核接口。

### 7.2 目录接口

- `GET /api/skills`：内置技能 + 当前用户 active 技能，不再返回其他用户技能，
  不再支持 `scope=public`。
- `PUT /api/skills/{id}/preference`：更新 `skills.enabled`；只允许 active 技能，
  未知/非 active 返回 404/409；变更写快照刷新与投影 outbox。
- `POST /api/skills/reload`：admin 运维刷新可保留，但与审核/发布无关。
- 移除全局技能开关 PATCH：没有公共技能，也就不存在全局开关。

### 7.3 响应字段

`SkillListItem` 增加：

- `status`：draft / unscanned / scan_failed / scanned / active / builtin；
- `latest_scan`：扫描摘要（可为空）；
- `active_version_id`、`active`：当前运行时版本；
- `can_activate`、`activation_blocked_reason`：前端按钮可用性；
- `index_status`：active 版本的投影状态；
- `enabled`：用户启用开关（默认 true）；`load_count` 只在内部/调试需要时返回，
  UI 不依赖它做操作。

## 8. 前端

### 8.1 “我的技能”页

- 只请求 `/api/skills/mine` 和内置技能；删除公共目录区、审核入口、编辑公共技能入口。
- 每行显示：名称、私有标签、生命周期徽标（草稿/待扫描/扫描未通过/已扫描/已生效）、
  最新扫描摘要、编辑按钮。
- 已生效行额外显示启用开关；关闭后显示“不参与技能检索”，开关调用 preference API
  并刷新列表。草稿/扫描中的行不显示开关。
- 空态文案改为“保存后先做安全扫描，扫描通过再点击生效”。

### 8.2 编辑器

- 文件树/正文/上传/删除/保存保持现有交互。
- 保存写入 Git 并产生不可变版本；成功后提示“已保存草稿”。
- 保存后状态为草稿/待扫描，按钮组变为：保存、安全扫描、生效、删除。
- “安全扫描”调用扫描接口；显示：时间、耗时、扫描器版本、最高严重度、发现数、
  finding 列表（文件、行号、规则、严重度、建议）。
- “生效”仅在 `can_activate` 时可用；失败显示 `activation_blocked_reason`。
- “生效”成功提示包含“已生效”；保存阶段 Git 失败时如实展示错误，暂存内容保留。
- 生效后显示 active 版本和 index_status；保存新版本后旧 active 仍保留，页面同时
  展示“已生效版本”和“当前草稿版本”。
- 删除审核相关文案、驳回原因、撤回按钮和 pending 状态。

### 8.3 移除的页面

- `/workspace/admin/skills` 审核台；
- review-queue / review-detail 组件及测试；
- 侧栏“审核”入口和 `reviewActive` 属性。

## 9. 运行时、检索与沙箱

- `SkillSnapshot` 只包含 `private_by_owner`；`visible_for(user)` 只返回该用户的
  active 且 `enabled=true` 的技能，再应用 `available_skills`。
- `build_skills_section` 只渲染 `/mnt/skills/private/<name>` 与内置根，不再有
  public 根；私有行带 `skill_id` 和容器 `path`。
- `SkillSearchService` 只查 owner 的 active、enabled 技能：
  Milvus 过滤 `scope == "private" and enabled == true and owner_user_id == "<user>"`，
  PostgreSQL 用 active 指针和 `skills.enabled` 二次确认；不再有 public 回退。
- outbox worker 只处理 `private:<owner>:<skill_id>`；投影 collection 名保持
  `myflow_skills_active`，单行仍带 `owner_user_id` / `enabled` 以便过滤和审计。
- 沙箱只做按用户私有挂载 `/mnt/skills/private -> active/users/<user>`；
  不支持按用户挂载的 provider 不提供容器路径，只通过提示词/检索注入正文。
- slash 激活、prompt、active context 全部只从调用者自己的 active、enabled 集合取。
- 私有技能的用户开关只来自 `skills.enabled`；`skills_prefs.json` 不再参与 Git
  私有技能的 prompt、slash、search 或目录响应。

## 10. 迁移与清理

- 旧库的 `skill_submissions / skill_releases / skill_release_batches /
  skill_public_active / skill_settings` 以及 `skills.public_name` 不再被读取。
- 迁移 `0011_user_isolated_skills`：
  1. 新建 `skill_security_scans`；
  2. 丢弃上述公共/审核表；PostgreSQL 上同时丢弃 `skills.public_name`，SQLite
     为了不重建被外键引用的 `skills` 表而保留这个不再映射/写入的可空旧列；
  3. 保留现有 `skills / skill_versions / skill_draft_files /
     skill_private_active / skill_index_outbox / skill_audit_events` 数据；
  4. 把旧的私有 active 指针保留为可用技能，删除尚未生效的公共指针。
- 删除 `bootstrap_public_skills.py`、审核相关脚本和前端审核页面。
- 启动时不再读取 public release 或 settings；Milvus 里旧 public 行由 outbox 对账
  或运维 reindex 清掉。
- 新增 `0013_skill_enabled`：`skills.enabled` 默认 true；旧私有技能迁移后默认启用。
- 新增 `0014_skill_load_count`：`skills.load_count` 默认 0；供 Redis 启动回填和
  无 Redis 时的 prompt 兜底排序。
- 删除/停止写入 Git 私有技能的 `skills_prefs.json`；legacy filesystem 调试路径的
  文件兼容代码单独标注，不属于本设计的私有技能链路。

## 11. 验收标准

1. 保存时 commit/push Git 成功并写入不可变版本行；Git 不可达或 push 失败时保存
   返回错误，数据库不写版本行，暂存内容保留，页面如实展示失败原因。
2. 用户 A 创建并保存技能后，用户 B 的 `/api/skills/mine`、`/api/skills`、
   搜索、slash 和沙箱路径都看不到该技能。
3. 保存后技能不出现在 `<available_skills>`，也没有 Milvus 行，状态为 unscanned；
   Git 草稿分支已经产生新 commit。
4. 点击安全扫描后，后端确实 import `skill_scanner`，扫描该版本不可变的 Git commit
   内容，扫描结果写入 `skill_security_scans`，接口回显 findings。
5. 未扫描、扫描失败或扫描不属于最新版本时，生效返回 409，不改变 active 指针。
6. 扫描通过并点击生效后：active 指针更新、快照刷新、outbox 产生
   `private:<owner>:<skill_id>` upsert，worker 调用 Milvus upsert。
7. Git 不可达时保存失败且不写版本行；恢复后重试保存成功并产生新版本。
8. 保存 v2 后 active 仍为 v1；v1 继续可检索，v2 必须重新扫描和生效。
9. 删除技能后 active 指针和 Milvus 行被删除，其他用户/检索不再返回。
10. 不存在任何管理员审批或公共技能路由、页面、表写入路径。
11. 新技能默认 `enabled=true`；只有 active 技能可以切换开关；关闭后它仍出现在
    所有者管理列表，但不出现在 prompt、slash、检索候选或 Milvus 可检索行中。
12. Milvus 投影行与检索过滤都带 `enabled`；开关变更经过 outbox 收敛，版本
    `index_status` 在切换后进入 pending，worker/对账最终恢复 ready。
13. `search_skills` 只返回 `skill_id / name / description`，不返回正文；它是
    skill-finder 的专属工具。`load_skills` 是系统内置工具，按 skill_id 返回正文。
14. 每次成功 `load_skills` 同时增加 PostgreSQL `skills.load_count` 与 Redis
    per-user ZSET；两者不要求同一事务，失败独立记录且不影响正文加载。
15. 私有技能超过 10 个时，system prompt 的 `<available_skills>` 用 Redis ZSET
    排名选择 top-10；Redis 冷启动/不可用时退回 PostgreSQL 计数，再退回稳定名称序。

## 12. 2026-11-03 修订：工具拆分与使用次数

### 12.1 工具拆分

| 工具 | 归属 | 输入 | 输出 |
|---|---|---|---|
| `search_skills` | 仅 `skill-finder` 子代理 | `task_query` / `domain_keywords` | `[{skill_id, name, description}]`，不含正文 |
| `load_skills` | 系统内置工具，lead 和普通 agent 始终可用 | `skill_id` | `{"skill": {skill_id, name, description, scope, path, body}, "message"}` |

- `search_skills` 只做召回和元数据返回；不读取正文到返回体，不增加使用次数。
- `load_skills` 按 owner + active + enabled 校验后读取 SKILL.md 正文（frontmatter
  已剥离，元数据由字段返回），再计入使用次数。正文不截断；它是“加载即激活”的入口。
- `skill-finder` 的系统提示词和 description 改为：用 `search_skills` 找候选，
  交回 `skill_id / name / description / 匹配理由`；正文由 lead 调 `load_skills`，
  不再让子代理 read_file 或搬运正文。
- `load_skills` 进入 `ALWAYS_AVAILABLE_BUILTIN_TOOL_NAMES` 和 durable-context
  的 loader 工具集合；`search_skills` 不进 always-available，由 skill-finder 认领。
- prompt 私有技能行增加 `id=<skill_id>` 与 `path=<container_file>`，lead 可以直接
  用 id 调 `load_skills`，不需要再解析路径。
- 内置技能不进入 `search_skills` / `load_skills`；内置发现仍走索引、read_file 或 slash。
- legacy filesystem source 的 `skill_id` 用技能名充当；不计入数据库/Redis 使用次数。

### 12.2 PostgreSQL 使用次数

- `skills.load_count`：Integer，默认 0，只对非内置、非软删除技能计数。
- `load_skills` 成功返回正文后调用 store 的原子自增；失败只记日志，不阻断加载。
- 启动回填、Redis 丢失恢复都从这张表读取，不另建队列表。

### 12.3 Redis ZSET

- key：`<skills.usage.key_prefix>:<user_id>`，默认
  `myflow:skills:usage:<user_id>`。
- member：internal `skill_id`；score：非负整数使用次数。
- `load_skills`：先/后独立执行 `ZINCRBY` 与 DB 自增；没有跨系统事务，任一失败
  只记录并继续。Redis 成功后刷新本进程的 top-K 缓存。
- 启动：`SkillRuntime.start()` 调度一个异步任务，从
  `skills.load_count > 0` 的行读出 `(owner_user_id, skill_id, load_count)`（技能表规模
  有限，一次读完即可），按用户分组用 `ZADD ... GT` 写入 Redis，避免把 Redis 中更新的
  计数降回去；随后为这些用户预热 top-K 缓存。启动流程不等待它完成。
- 刷新：后台按 `skills.usage.refresh_seconds` 从 Redis 重读已知用户的 top-K，
  让多 worker 的 prompt 缓存最终一致。
- Redis 不可用：记 warning，`load_skills` 仍读正文并尝试写 DB；prompt 使用 DB
  `load_count` 兜底，不因为缓存不可用而隐藏技能。

### 12.4 Prompt top-10

- 私有 active + enabled 技能总数 ≤ 10：全部渲染。
- 超过 10：优先按 Redis ZSET 的 top-10 skill_id 选择；返回不足 10 个（冷启动、
  已删除、已关闭）时用 DB `load_count` + 稳定名称序补齐。
- 内置技能始终保留，不占私有技能 top-10 名额，也不写入使用次数。
- prompt builder 是同步路径，不能直接 await Redis；它读取由 `SkillUsageService`
  从 Redis 水合的进程内 top-K 缓存。缓存冷/不可用时按 DB 计数兜底。缓存 key 包含
  选中的技能身份，不包含易变的原始计数文本。

### 12.5 配置

~~~yaml
skills:
  usage:
    enabled: true
    redis_url: $REDIS_URL      # 留空则退回 DB 计数模式
    key_prefix: myflow:skills:usage
    prompt_top_k: 10
    refresh_seconds: 30
~~~

Redis 客户端由 gateway lifespan 持有并按 runtime 关闭；不把 Redis URL 或连接对象
暴露给前端。加载次数累加不是强一致事务：Redis 排名、DB 持久化和 prompt 缓存之间的
短暂偏差是设计允许的，最终由启动回填 + 周期刷新 + DB 兜底收敛。

## 13. 2026-11-04 修订：投影 collection 的自愈

### 13.1 事故形态

`myflow_skills_active` 存在但没有向量索引时，`load_collection` 返回 700
`index not found`，异常被包成 `ProjectionUnavailable`。旧实现把"集合存在"当成
"集合可用"：已存在的分支只调用一次 load 就去写向量，于是

1. 首次创建走 `create_collection(..., index_params=...)`，索引那一步失败（或 load
   与索引可见性抢跑）时集合已经落库，却没有任何向量索引；
2. 之后的每次重试都命中"已存在 → 只 load"，必然以同样的 700 失败；
3. 该 outbox 事件按退避重试到上限后被判为终止，版本 `index_status` 停在
   `failed`，技能版本永远没有进向量索引；
4. 只删集合不解决问题：下一次重试会再建出一个没有索引的集合，复现同一条失败路径。

### 13.2 不变式

任何一次写/查之前，"可用集合" = **存在** ∧ **schema 维度匹配** ∧ **embedding 向量
索引存在** ∧ **已 load**。集合存在不再是可用的充分条件。

### 13.3 修复规则

1. **已存在的集合先自检再 load**：`describe_collection` 校验 embedding 维度；
   再用 `list_indexes` / `describe_index` 确认 embedding 上真的有索引。
   缺失就补建（embedding 索引 + `scope` / `owner_user_id` / `enabled`
   的 INVERTED 标量索引），再 load。
2. **load 抛 700 时补建索引并重试一次**：覆盖"索引刚建好还不可见"和"集合被手工
   动过"两种情况；第二次仍失败就按普通失败处理，交给 outbox 退避。
3. **新建不留半成品**：`create_collection` 只建 schema（不再把 `index_params`
   塞进创建调用），随后显式 `create_index` → `load`。创建链路任何一步失败都丢弃
   刚建出来的集合（`drop_collection`，尽力而为），避免"没有索引的幽灵集合"。
4. **补建索引前先 release**：Milvus 不允许给已 load 的集合建索引；release 失败
   （通常本来就没 load）按正常情况处理。并发 worker 撞到"index already exist"
   视为成功，不报错。
5. **健康集合不重建索引**：`describe_index` 显示 embedding 索引存在时只 load，
   不产生额外写操作；索引类型/参数不匹配（例如换过 index_type）不在自愈范围内，
   需要运维重建 collection。

### 13.4 已经卡住的版本怎么恢复

- 索引补齐后，下一次 `SkillIndexOutboxWorker.reconcile()` 会因为
  `index_status != ready`（或投影 key 缺失）重新 embedding 并 upsert，把版本带回
  `ready`；不需要人工 reindex，也不需要删集合。
- 已经耗尽重试的 outbox 事件不会被"复活"——事件只是提示，权威来源是 active 指针
  加 `index_status`。
- 首轮对账用 `min(reconciliation_interval, 30s)` 的延迟，而不是先等一整个周期：
  部署重启后约 30 秒就会把 `index_status != ready` 的版本修回来，之后的轮次仍按
  `skills.index.reconciliation_interval_minutes`（默认 10 分钟）运行。
- 运维动作：部署新代码并重启网关即可，集合不需要手工删除，也不需要人工 reindex；
  在首轮对账之前，任何一次写/查同样会先把索引补上（自愈在投影内部，不依赖对账）。


## 14. 2026-11-04 修订：对账规模化

技能数、版本数、worker 数任一变大，§13 那套“每轮全量比对”就会先撑不住：一轮
`list_all_versions()` + 一次全量 Milvus 主键 query（`limit=16384`，超出即截断）
+ 一次本地目录 walk，按 10 分钟节奏空转。本节把对账拆成**快路径 + 低频巡查**，
并把提交缓存的清理改成事件驱动。

### 14.1 权威信号

- PostgreSQL 的 `skill_versions.index_status` 是“这一版该不该在 Milvus 里”的权威标记：
  成功 upsert 之后写 `ready`，失败写 `failed`。
- Milvus 主键集合只用来发现**静默丢失**（集合被清空、被手工动过）：低频事件，
  不该占用 10 分钟节奏。
- 因此**每轮只处理 `index_status != ready` 的行，不扫 Milvus**。

### 14.2 快路径（默认 10 分钟）

1. 一条索引查询取出到期需要修复的 active 版本：`index_status IN
   ('pending', 'failed', 'unindexed')` 且 `index_next_attempt_at IS NULL OR
   index_next_attempt_at <= now()`，按 `index_next_attempt_at`（NULL 优先）、
   `version_seq` 排序。
2. 逐行 embedding + upsert；**写前复核 active 指针**（语义不变），成功后写 `ready`，
   并把 `index_attempts / index_last_error / index_next_attempt_at` 清零。
3. 失败写 `failed`，记 `index_attempts += 1`、`index_last_error`（截断）、
   `index_next_attempt_at = now + min(60s * 2^(attempts-1), 1h)`。坏行不会每轮吃满
   预算，也不会饿死后面的行。
4. **预算**：每轮最多 `repair_budget_rows`（默认 500）行、`repair_budget_seconds`
   （默认 180 秒），先到为准；没修完的下一轮继续。本轮不做批量 embedding，
   所以“集合被清空”后的重建是每行一次 provider 调用：5000 行约 2–3 轮；预算调小
   会显著拉长恢复时间，配置时要注意这一条。
5. `index_attempts` 超过 `repair_max_attempts`（默认 8）后不再自动重试，留 `failed`
   等管理端重新生效或运维处理。

### 14.3 完整性巡查（默认 60 分钟，首轮 5 分钟内）

1. `SkillProjectionStore.iter_projection_keys()`：用 `query_iterator(batch_size=1000)`
   流式读主键，**不再有 16384 截断**。
2. 把 active 指针按 `private:<owner>:<skill_id>` 建索引；“active 有、Milvus 没有”的
   key 只把对应版本标回 `pending`，交给快路径带预算修复（巡查自己不做 embedding）。
3. Milvus 有、active 与 pending outbox 都没有的 key → 删除（孤儿行）。
4. 同样受 `verify_budget_rows / verify_budget_seconds` 约束，可中断。
5. 首轮延迟 `min(interval, 300s)`：集合被清空这类静默丢失不该等一整小时才发现。

### 14.4 分布式锁（每轮互斥，不是选主）

- 快路径与巡查各自跑在 `pg_try_advisory_lock` 下，key 由
  `sha256("skill-index-reconcile:<collection>")` / `"...:verify:<collection>"` 取前 8
  字节转 int64。抢不到就跳过本轮（`skipped_due_to_lock`），不排队。
- 复用 `skills/author.py::_skill_lock` 的写法：专用连接（`bind.connect()`）、抢到后
  `commit()` 结束隐式事务、`finally` 显式 `pg_advisory_unlock`。不要用
  `pg_advisory_xact_lock`：那会把整轮（含 embedding 与 Milvus 写）包进一个 PG 事务，
  违反“Milvus 写不发生在 PG 事务里”的既有约束。
- 非 PostgreSQL（SQLite 测试、本地调试）直接放行，不起锁。
- 配置 `skills.index.maintenance_lock`（默认 true）；单 worker 部署也只是一次多余 RPC。

### 14.5 提交缓存：事件驱动清理

- **删除周期性 `prune()`**：它每轮做全量集合比对（全表版本 + 全量 Milvus 主键 +
  目录 walk），收益只是磁盘上界，不值得占对账节奏。
- 缓存缺失不是错误：`SkillContentStore.ensure()` 本来就是“本地没有就从 Git 物化”，
  `read_text` / `read_bytes` 都会走到它。提交缓存只是缓存，删掉不影响正确性。
- 清理时机改成事件驱动：保存新版本、删除技能时，只对该技能计算保留集（active、
  pending outbox、最近 `retention.keep_versions_per_skill` 版），把不在保留集、且比
  `retention.cache_ttl_days` 更旧的提交目录删掉。只处理该技能名下的提交，绝不做
  全目录扫描，也不会误删别的技能的缓存。
- Milvus 孤儿行不再由周期性 prune 负责：删除技能已有 `delete_active` 事件，残余
  孤儿交给 §14.3 的巡查处理。

### 14.6 配置

~~~yaml
skills:
  index:
    outbox_poll_seconds: 2
    catalog_poll_seconds: 5
    reconciliation_interval_minutes: 10   # 快路径
    maintenance_lock: true                # pg_try_advisory_lock 每轮互斥
    repair_budget_rows: 500
    repair_budget_seconds: 180
    repair_max_attempts: 8
    verify_interval_minutes: 60           # 完整性巡查
    verify_budget_rows: 5000
    verify_budget_seconds: 300
  retention:
    keep_versions_per_skill: 20
    cache_ttl_days: 30
~~~

### 14.7 迁移

- `0015_skill_index_repair`：`skill_versions` 新增 `index_attempts INTEGER NOT NULL
  DEFAULT 0`、`index_last_error VARCHAR(255) NULL`、`index_next_attempt_at TIMESTAMP
  WITH TIME ZONE NULL`，并为 `index_status` 建索引（快路径与运维查询共用）。
- 旧行 `index_attempts=0`、`index_next_attempt_at=NULL`，迁移后即可被快路径拾取。

### 14.8 本轮不做的部分

- **批量 embedding**：每行一次 provider 调用；预算足够大时恢复时间可接受。
- **指标设施**：不引入 Prometheus/OTel；每轮写一条 INFO 汇总
  （`scanned / repaired / failed / deleted / seconds / skipped_due_to_lock`）。

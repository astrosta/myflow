# 随项目发布的内置技能设计

> 状态：已确认（2026-10-28）
> 适用范围：MyFlow 的技能系统（Git catalog + filesystem 兼容层）
> 方案：A（完整内置技能层），但不提供个人开关

## 1. 意图与问题

公共技能和私有技能都依赖 Git/PostgreSQL 的生命周期。有些技能应该由项目直接随代码发布：例如团队通用的编码规范、排障 SOP、固定输出格式。它们：

- 应该和公共技能区分开，不能混进公共目录或审核队列；
- 直接放在项目目录中，由开发者随代码修改，不经过 Git catalog 管理；
- 不需要用户单独开关，name 和 description 始终出现在系统提示词索引；
- 应该在 UI 里可见，但只读。

当前实现没有“内置技能”这一层。仅把目录放进项目根不会被 source: git 的运行时读取；即使被 filesystem 扫描到，也会被当成 public/custom，和公共技能混在一起。

## 2. 目标与非目标

### 目标

1. skills/builtin/<name>/SKILL.md 随项目发布，启动后自动进入技能索引。
2. 内置技能有独立的 scope（builtin）和容器目录（/mnt/skills/builtin）。
3. 不受全局开关和个人偏好影响，始终启用。
4. 支持系统提示词索引、/name 激活、read_file、沙箱挂载。
5. 采用渐进式加载：索引常驻，正文只在 read_file 或 /name 激活时进入上下文；**不参与 search_and_load_skills 检索**。
6. 在 GET /api/skills 和技能页回显；UI 只读，不可编辑、提交审核或删除。
7. 不写 Git，不写 PostgreSQL，不需要数据库迁移。
8. Git catalog 不可用时，内置技能仍然能被列表、提示词和沙箱使用。

### 非目标（V1）

- 内置技能的个人开关、管理员全局开关。
- 在 UI 编辑、删除、提交审核内置技能。
- 把内置技能写入 Milvus 投影或做独立的语义召回/精排。
- 让内置技能出现在 search_and_load_skills 的候选或结果里。
- 内置技能与公共技能之间的版本、审核、所有权关系。
- 嵌套技能目录、内置技能包自动安装、远程下载。
- 通过配置热切换 skills.builtin.path 后重建沙箱挂载。

### 反目标

- 不能因为内置技能绕过沙箱路径映射或 allowed-tools 策略。
- 不能把内置技能混入 public 目录、审核队列或 PostgreSQL 的 skill/version 表。
- 不能每次模型调用都全量解析磁盘；必须有轻量缓存。
- 不能让同名技能在索引里出现两行，导致 slash 和 read 路径二义。

## 3. 现状约束

- Git 模式下，提示词索引、slash 激活、工具策略、<active_skills> 都从进程级 SkillSnapshot 读取。
- SkillRegistry 是这些路径的统一入口；它当前只投影 snapshot 或 filesystem scanner。
- 沙箱只静态挂载 <container_path>/public 和按用户的 <container_path>/private。
- GET /api/skills 在 Git 模式下从 snapshot 输出；runtime 缺失时 fail-closed 为空。
- SkillResponse.scope 和前端 SkillScope 只接受 public/private。
- 个人偏好按 skill_id（catalog）或 name（filesystem）存储，effective_enabled 会参与可见性判断。
- 搜索服务 SkillSearchService 以 PostgreSQL active 指针和 Milvus 投影为权威，正文从 commit cache 读。
- search_and_load_skills 是一条“检索并加载正文”的路径；内置技能不使用它，走索引 → read_file / slash 的渐进式加载。

因此内置技能必须作为独立来源 overlay 接入，而不是伪装成 catalog 行。

## 4. 方案与取舍

### A. 独立 builtin overlay（采用）

- 新增 skills/builtin 配置和扫描器。
- SkillRegistry 在 catalog/filesystem 结果之上合并内置技能。
- 单独的只读沙箱挂载。
- API/前端增加 builtin scope。
- 不写 Git/DB。

优点：职责清晰，Git 故障时仍可用，不需要迁移，用户偏好语义不被污染。
代价：要在 registry、prompt、API、UI、sandbox 五个边界各加一处，但每处都很薄。检索路径刻意不动。

### B. 只做提示词索引和挂载（未采用）

不接入 API/slash，模型只能看到索引并 read_file。改动更小，但没有 slash 激活，UI 也不回显；不符合“完整内置层”的要求。

### C. 启动时导入 Git catalog（未采用）

让内置技能走 create/submit/approve。仍然依赖 Git 和 PostgreSQL，不符合“不用 Git 管理”的核心要求，且会把内置内容变成可审核、可编辑的公共内容。

## 5. 详细设计

### 5.1 目录与配置

项目目录：

    skills/
    └── builtin/
        ├── code-review/
        │   ├── SKILL.md
        │   └── templates/
        └── incident-triage/
            └── SKILL.md

配置：

    skills:
      builtin:
        enabled: true
        path: skills/builtin

约定：

- path 相对项目根，和 skills.path 使用同一套 resolve_path。
- 只扫描 builtin.path 的直接子目录；每个含 SKILL.md 的目录是一个技能包。
- 目录名和 frontmatter name 不一致时，以 frontmatter 为准并 warning；索引会自动带 dir=。
- SKILL.md 必须是 UTF-8，frontmatter 至少有 name 和 description。
- 内置目录拒绝符号链接目录和符号链接 SKILL.md，避免把项目外的路径带进沙箱。
- enabled: false 时完全不加载、不挂载；该字段和 path 都是 startup-only，因为沙箱静态映射在 provider 构造时固定。
- 仓库提交一个 skills/builtin/.gitkeep，保证默认路径存在；没有内置技能时目录为空即可。
- 默认 path 不存在时等于没有内置技能，不报启动错误；但 enabled=true 且路径缺失时启动日志 warning 一次，避免镜像漏打包后静默为零技能。
- 部署时必须把 skills/builtin 放进网关进程的 project_root（或配置绝对路径）；API 进程看到的宿主路径必须和沙箱挂载的是同一个路径。

### 5.2 类型与扫描

新增：

- SkillCategory.BUILTIN = "builtin"
- SkillScope.BUILTIN = "builtin"
- agentkit/skills/builtin.py

扫描器输出 Skill：

- category=BUILTIN
- scope=BUILTIN
- enabled=True
- skill_id=None、version_id=None、release_id=None
- relative_path=Path(目录名)
- container_path=config.container_path，因此 container_file 自动变成
  <container_path>/builtin/<目录名>/SKILL.md

复用现有扫描规则（包边界、名字正则、frontmatter 解析、坏技能跳过），将 storage.py 中 public/custom 的目录扫描抽成共享 helper；不要复制一份 frontmatter/符号链接逻辑。

缓存：

- 模块级 BuiltinSkillSource 保存上次指纹和扫描结果。
- 指纹从每个技能 SKILL.md 的 name + mtime_ns + size 计算。
- 每次 SkillRegistry.visible()、API 列表调用时检查指纹；变化才重新解析。
- clear_builtin_cache() 供 POST /api/skills/reload 显式失效。
- 解析失败的单个技能跳过并 warning，不影响其它内置技能。

### 5.3 注册表合并与优先级

SkillRegistry 增加可选的 builtin 注入点（默认从当前 SkillsConfig 读取，测试可注入固定 tuple）。

可见性：

1. 先按现有逻辑得到 catalog/filesystem 的可见技能；
2. 再加载内置技能；
3. 内置技能只受 available_skills（agent 白名单）限制，不读取个人偏好；
4. 按 name 去重：内置技能优先，同名 catalog 条目被隐藏并 warning；
5. 返回值排序仍按 (category, name)，因此 builtin 行稳定在索引前部。

search_and_load_skills 不走“全量可见技能”：它在 filesystem 分支调用 visible(include_builtin=False)，只检索 public/custom；Git 分支的 SkillSearchService 从不接收 builtin。内置技能因此不会出现在检索结果里。

SkillRegistry.find() 走合并后的 visible()，所以 /name 天然优先解析到内置技能。
index_by_container_file() 同时包含内置容器路径，工具策略和 durable context 不需要新分支。

不在 SkillSnapshot 中增加 builtin 字段：内置技能是独立 overlay，这样 Git runtime 缺失或 snapshot 为空时仍然可见。SkillSnapshot.visible_for() 的行为不变，避免影响现有 catalog 语义。

### 5.4 始终启用

- Skill.enabled=True。
- visible() 对 builtin 跳过 effective_enabled / global_enabled_map / read_user_skill_prefs。
- 即使 skills_prefs.json 里有同名 false，内置技能仍在索引和 slash 解析中。
- available_skills 是 agent 级白名单，不是个人开关，仍生效；默认 None 时对主 agent 全部可见。
- 不做 PUT /api/skills/{id}/preference 的内置分支；builtin 没有 skill_id，现有路由无法定位它。
- 不做 PATCH /api/skills/{id} 的全局开关；部署级开关只有 skills.builtin.enabled。

### 5.5 提示词索引

- SkillRegistry.visible() 包含 builtin 后，build_skills_section() 自动得到这些行。
- _entry_marker() 用 scope 值打标，因此行尾为 [builtin]。
- 根目录约定按“来源 + 实际 scope”分支，保留旧测试的兼容路径：
  - source=git：使用 catalog 文本（public/private；有 builtin 时再加 builtin 根）。
  - source=filesystem 且快照里有 public/private scope（测试或迁移注入）：继续使用 catalog 文本，和旧行为一致；若同时有 builtin，则用 catalog+builtin 文本。
  - source=filesystem、没有 public/private scope、但有 builtin：使用新的三段文本（public/custom/builtin，custom 覆盖 public）。
  - source=filesystem、既没有 catalog scope 也没有 builtin：继续使用现有 SKILL_ROOT_CONVENTION，不改旧测试。
- 三段路径分别是：
  - 公共：<container>/public/<name>/SKILL.md
  - 私有：<container>/private/<name>/SKILL.md
  - 内置：<container>/builtin/<name>/SKILL.md
- 内置技能的 name/description 常驻；描述仍按 index_description_max_chars 截断。
- 现有 HTML 转义、索引 LRU、密钥提示保持不变。
- 索引缓存 key 已包含 name/description/scope/container_dir，内置技能内容变化会自然换 key；reload 再清一次 LRU。

### 5.6 沙箱挂载

在 LocalSandboxProvider 的静态映射中增加：

    <container_path>/builtin  ->  <resolved builtin.path>   (read_only=True)

- Git 模式：与 public/private 映射并列。
- filesystem 模式：整棵 skills root 已挂载；仍增加 builtin 精确映射，保证可配置到 root 外。
- operator 的 sandbox.mounts 继续禁止覆盖 <container_path>/*。
- 内置目录不存在时也建立映射；之后创建目录不需要重建 provider，但配置路径变更需要重启。
- 文件工具写入 builtin 路径会被 read_only 拒绝；host bash 仍是已知的非隔离边界。

### 5.7 API

GET /api/skills：

- 无论 source=git 还是 filesystem，结果都先加内置技能，再加原有 catalog/filesystem 行。
- Git runtime 缺失时仍返回内置技能；public/private 为空，保持原本的 fail-closed 语义。
- 同名 catalog 行被内置行遮蔽（与 registry 一致）。
- 内置行：
  - scope="builtin"
  - category="builtin"
  - status="builtin"
  - enabled=True
  - global_enabled=None
  - skill_id/version_id/git_commit/skill_path 为 null
  - container_file 指向 /mnt/skills/builtin/...
- ?scope=global 仍只对 admin 开放；内置行的 global_enabled 仍为 null。
- GET /api/skills/mine 不包含内置技能。

POST /api/skills/reload：

- 先清 builtin 指纹缓存；prompt LRU 在每条路径上只清一次：runtime 缺失时清完再返回 503，成功 refresh 后清。
- 这保证 Git runtime 缺失时删除/新增内置技能仍然能被下一次读取看到，同时不改变现有成功路径的缓存清理次数。
- Git 模式仍按现有逻辑刷新 snapshot/tree；runtime 缺失时接口保持现有 503，不把“catalog 没刷新”伪装成成功。
- filesystem 模式继续清 extensions_config 缓存。

PUT /preference、PATCH：

- 不改语义；builtin 没有 skill_id，无法被这些路由定位。
- 如果未来出现按 name 的入口，必须显式拒绝 builtin（只读错误码）。

SkillResponse 扩展：

    category: Literal["public", "custom", "builtin"]
    scope: Literal["public", "private", "builtin"] | None

### 5.8 前端

- SkillScope 增加 "builtin"；SkillStatus 增加 "builtin"。
- 技能页把 listSkills() 结果拆成：
  - 「内置技能」：只读，显示名称、描述、[内置] 标签；
  - 「公共技能」：保持现有逻辑。
- 内置行不渲染编辑按钮、不渲染个人开关、不跳编辑器。
- 列表 key 改为 skill.skill_id || skill.name，避免 builtin 的 skill_id=null 产生重复 React key。
- ScopeTag 增加 builtin 配色和“内置”文案；空态/页脚文案说明内置技能随应用发布、只读。
- 如 UI 后续增加开关，必须按 scope === "builtin" 禁用。

### 5.9 渐进式加载与检索边界

内置技能采用渐进式加载，分三层：

1. **L0 索引**：name + 截断 description 常驻系统提示词；模型知道“有这个技能、什么时候用”。
2. **L1 正文**：模型用 read_file 读 <container>/builtin/<name>/SKILL.md，或用 /name 显式激活；正文进入本次调用上下文，并由 DurableContextMiddleware 记入 <active_skills>。
3. **L2 资源**：脚本、模板、示例按需 read_file，不预先注入。

关键约束：**内置技能不参与 search_and_load_skills**。

- Git 模式：SkillSearchService 不注入 builtin，Milvus/PG/commit-cache 路径完全不变；内置技能不会成为检索候选。
- filesystem 模式：search_and_load_skills 现在用 SkillRegistry.visible() 建候选；该调用改为 visible(include_builtin=False)，只检索 public/custom。内置技能不进入 build_payload。
- SkillRegistry.visible(include_builtin: bool = True) 是唯一开关；提示词、slash、policy、durable context 保持默认 True，搜索工具显式传 False。
- include_builtin=False 时完全不加载内置技能，也不做“同名内置遮蔽”：catalog 的 public/custom 行原样参与检索。遮蔽只发生在面向用户的合并视图（默认 True 和 API 列表）。
- 不修改 router.py、loader.py、SkillSearchService 的评分/预算逻辑；不引入 builtin 的 embedding、BM25 或精排。
- 即使 keyword.enabled=true，内置技能也不会被 search_and_load_skills 命中；这是产品语义，不是配置遗漏。
- 检索不可用或 keyword 关闭，不影响内置技能的索引、read_file 和 slash 路径。

渐进式加载的可见效果：

- 首次模型调用只多出索引行，不增加正文 token。
- 模型读 SKILL.md 后，DurableContextMiddleware 把正文投影到 <active_skills>，后续轮次不需要重复读。
- 正文预算仍由 skills.active_context 控制；read_file 工具自身的读取上限不变。
- 内置技能与公共/私有技能共用同一套路径、正文注入和 allowed-tools 策略，只是发现方式不同。

### 5.10 同名与保留名

- 运行时同名：内置优先；catalog 行不出现在 registry/API 列表/索引中，记 warning。
- 新建草稿时：SkillAuthorService.create_draft 读取当前内置名集合，命中则返回 SkillConflict，提示该名称被内置技能保留。
- 审核通过时：SkillReviewService.approve 再次检查 public_name，防止草稿创建后内置技能新增导致冲突。
- 两个内置目录同名：按目录名排序保留第一个，warning 第二个。
- 这保证 slash、索引、容器路径始终只有一条可解析的 builtin 技能。

### 5.11 安全

- 内置技能正文仍作为不可信数据处理：索引 HTML 转义、正文经现有 provenance 注入。
- allowed-tools、required-secrets、secrets-autonomous 继续走现有 parser/policy。
- 服务端不执行内置技能里的脚本；只读挂载给沙箱。
- 拒绝符号链接，避免把项目外文件映射进容器。
- 不允许通过 sandbox.mounts 覆盖 <container_path>/builtin。

## 6. 改动清单

### 后端

- backend/packages/harness/agentkit/config/skills_config.py
  - BuiltinSkillsConfig；SkillsConfig.builtin；root_dir() 类似 helper。
- backend/packages/harness/agentkit/config/reload_boundary.py
  - skills.builtin.path、skills.builtin.enabled startup-only 说明。
- backend/packages/harness/agentkit/skills/types.py
  - SkillCategory.BUILTIN。
- backend/packages/harness/agentkit/skills/domain.py
  - SkillScope.BUILTIN。
- backend/packages/harness/agentkit/skills/builtin.py（新）
  - 扫描、缓存、load_builtin_skills、clear_builtin_cache、builtin_names。
- backend/packages/harness/agentkit/skills/storage.py
  - 抽取共享扫描 helper；SkillRegistry 合并 builtin；builtin 跳过偏好和全局开关。
- backend/packages/harness/agentkit/skills/prompt.py
  - 根目录三段说明；builtin marker 已由 scope 泛化。
- backend/packages/harness/agentkit/skills/prefs.py
  - effective_enabled 对 builtin 返回 True（或由 registry 绕过）。
- backend/packages/harness/agentkit/tools/builtin/search_and_load_skills.py
  - filesystem 分支改调 visible(include_builtin=False)，内置技能不参与检索。
- backend/packages/harness/agentkit/sandbox/local/local_sandbox_provider.py
  - builtin 静态只读映射；路径解析 helper。
- backend/app/gateway/skills_catalog.py
  - 列表合并；builtin response；runtime 缺失时回显 builtin；reload 清缓存。
- backend/app/gateway/models/response.py
  - category/scope literal 增加 builtin。
- backend/app/gateway/routers/skills.py
  - 无需新增路由；reload 语义覆盖 builtin。
- backend/packages/harness/agentkit/skills/author.py
  - 创建草稿时拒绝内置保留名。
- backend/packages/harness/agentkit/skills/review.py
  - 批准时再次检查 public_name。
- config.example.yaml、config.yaml
  - 增加 skills.builtin 注释与默认配置。
- skills/builtin/.gitkeep（新）
  - 保证默认目录随仓库存在；不提交示例技能。

### 前端

- frontend/src/core/api/skills.ts
  - SkillScope/SkillStatus 增加 builtin；toSkillListItem 保留 builtin scope。
- frontend/src/components/skills/my-skills-page.tsx
  - 拆分内置/公共；只读行；key fallback；文案。
- frontend/src/components/skills/skill-status-badge.tsx
  - 增加 builtin 文案/色调（如果渲染状态徽标）。
- frontend/src/components/skills/my-skills-page.dom.test.tsx
  - 内置行、无编辑按钮、无重复 key。

### 文档

- backend/README.md 技能章节。
- 本设计文档。

## 7. 测试计划

后端：

- test_builtin_skills.py（新）
  - 合法/非法 frontmatter、目录名不一致、坏技能跳过、符号链接拒绝。
  - container_file == /mnt/skills/builtin/<name>/SKILL.md。
  - 缓存命中与修改后失效。
  - skills.builtin.enabled=false 时为空；路径缺失时 warning 一次且不抛。
  - Git/DB 不存在时仍可加载。
- test_skill_index.py
  - 内置技能出现在索引，标记 [builtin]。
  - 即使 prefs 同名 false，仍出现。
  - 同名 catalog 被内置遮蔽。
  - available_skills 限制仍生效。
  - source=filesystem、无 builtin 时根目录文本与旧断言一致；有 builtin 时切到 filesystem+builtin 三段文本。
- test_skill_activation_request_only.py
  - /builtin-name 注入正文；allowed-tools 生效。
- test_skills_catalog_routes.py
  - list 含 builtin，字段正确；runtime 缺失时仍回显；/skills/mine 不含。
  - ?scope=global 的 builtin global_enabled=None。
  - runtime 缺失时 POST /skills/reload 仍返回 503，但 builtin 指纹缓存和 prompt LRU 已被清空。
- test_builtin_mounts.py（新）
  - 静态映射存在且只读；路径解析到 host builtin；operator mount 不能覆盖。
- test_search_and_load_skills_tool.py
  - builtin 不出现在检索结果（filesystem 的 visible(include_builtin=False) 与 Git 的 SkillSearchService 都不含它）。
  - 同一内置技能仍能通过索引、read_file 和 /name 激活使用。
  - public/custom 的检索结果和预算不变。
- test_skill_author_service.py / test_skill_review_service.py
  - 内置保留名不能创建草稿/批准。

前端：

- my-skills-page.dom.test.tsx
  - 内置区回显 name/description；没有编辑/开关；公共区不受影响。
- skills.test.ts（如涉及类型映射）。

命令：

    cd backend
    uv run python -m pytest \
      backend/tests/test_builtin_skills.py \
      backend/tests/test_skill_index.py \
      backend/tests/test_skill_activation_request_only.py \
      backend/tests/test_skills_catalog_routes.py \
      backend/tests/test_builtin_mounts.py \
      backend/tests/test_search_and_load_skills_tool.py -v

    cd frontend
    pnpm vitest run src/components/skills/my-skills-page.dom.test.tsx

## 8. 迁移与兼容

- 无数据库迁移、无 Git 变更。
- 默认 skills.builtin.path 指向不存在的目录时不报错（仅 warning 一次），行为与当前一致。
- SkillSnapshot 不新增字段，现有 catalog 测试不需要改写。
- SkillScope/SkillCategory 新增枚举值；现有 public/private 分支保持不变。
- SkillResponse 新增 literal 值；旧客户端忽略 scope/category 新值也能渲染基础字段。
- 现有 skills_prefs.json 中同名的条目对 builtin 无效；不会删除用户数据。

## 9. 风险与已定取舍

1. agent 白名单：内置技能在 registry（提示词/slash/policy）仍受 available_skills 限制；search_and_load_skills 完全不含内置技能，不存在“检索绕过白名单”的问题。
2. 检索能力：内置技能明确不参与检索，也不接 Milvus/embedding。若未来要支持检索，需要重新设计候选类型、评分和权限；V1 不做。
3. 同名策略：内置优先并隐藏同名 catalog 行。若希望“公共覆盖内置”，需要反转优先级并调整保留名检查。
4. 热更新：SKILL.md 内容变化通过指纹自动生效；skills.builtin.path 变化需要重启（沙箱静态映射）。
5. UI 只读：不提供内置技能的编辑入口；开发者直接改项目文件。
6. 部署：内置技能随 project_root 下的 skills/builtin 发布；镜像/挂载必须包含该目录，API 进程与沙箱必须看到同一宿主路径。

## 10. 验收标准

- 在 skills/builtin/demo/SKILL.md 放入合法技能后，无需 Git/DB 操作，下一轮模型调用索引出现 demo [builtin]。
- 用户个人偏好里把 demo 设为 false，索引仍出现；API 返回 enabled=true。
- /demo 能激活并注入正文；/mnt/skills/builtin/demo/SKILL.md 可读且不可写。
- search_and_load_skills 不返回 demo；但索引、read_file、/demo 都可用，且读过的正文进入 <active_skills>。
- GET /api/skills 回显 builtin 行；技能页有“内置技能”区且没有编辑/开关。
- 公共技能、私有草稿、审核队列、个人偏好、Git catalog 行为不变。
- 删除 skills/builtin/demo 后，索引/列表/沙箱解析不再出现该技能（重启或 reload 后；指纹扫描可更早发现）。

## 11. Builder check 已定问题

以下四项在 builder check 中被提出，设计阶段直接定案，不再阻塞计划：

1. **filesystem 模式的根目录文本**：_root_convention() 先看来源和实际 scope；source=filesystem 且快照注入 public/private 时仍用 catalog 文本（保留旧测试），只有“纯 filesystem + builtin”才用 filesystem+builtin 三段文本。无 builtin 的纯 filesystem 部署继续用旧文本。
2. **检索边界**：内置技能不参与 search_and_load_skills；filesystem 分支显式传 visible(include_builtin=False)，Git 分支不注入 builtin。渐进式加载只走索引 → read_file / slash。
3. **部署与缺失路径**：skills/builtin 随 project_root 发布，提交 .gitkeep；enabled=true 且路径缺失时 warning 一次，不阻止启动。API 与沙箱使用同一宿主路径。
4. **runtime 缺失时的 reload**：先清 builtin 指纹缓存；prompt LRU 在 runtime 缺失路径和成功路径各清一次（不重复），runtime 缺失仍返回现有 503，不伪装成功。

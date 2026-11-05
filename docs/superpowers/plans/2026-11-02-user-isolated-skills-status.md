# 当前状态：用户隔离技能 + 安全扫描 + 生效

> 日期：2026-11-02
> 状态：**实现与自动化验证已完成，尚未提交 Git**。后续从这里继续。
> 保存语义：点击保存即 commit/push Git 草稿分支并写不可变版本；扫描针对该 commit；
> 生效只切 active 指针（不再有“本地保存”阶段）。
> 设计基线：`docs/superpowers/specs/2026-10-23-skill-package-git-release-design.md`
> （已重写）。旧计划 `docs/superpowers/plans/2026-10-23-skills-catalog.md` 已标记 SUPERSEDED。

## 已完成的业务变化

1. 删除管理员审核 / 公共技能发布模型：
   - 删除 `skill_submissions / skill_releases / skill_release_batches /
     skill_public_active / skill_settings` 的 ORM、SQL store 路径、服务、路由和前端页面；
   - 删除 `review.py`、`skill_reviews.py`、审核排队/详情组件、admin 技能页面；
   - 删除 `submit / withdraw / approve / reject / unpublish` 路由；
   - Git 只保留 `refs/heads/myflow/drafts/<user>`，不再有 `public/` 发布树。

2. 技能按用户隔离：
   - `skills.owner_user_id` 是唯一可见性边界；
   - snapshot / prompt / slash / search / Milvus / sandbox 都只返回调用者自己的 active 技能；
   - Milvus 过滤条件固定为 `scope == "private" and owner_user_id == "<caller>"`；
   - 非 owner 读取统一 404。

3. 保存即 Git 草稿提交：
   - `POST /api/skills/{id}/draft/save` 把暂存内容 commit/push 到用户草稿分支，
     并写入一行不可变 `skill_versions`；
   - 保存不写 active 指针、不写 outbox；
   - 版本 `index_status` 初始为 `unindexed`；
   - 草稿不会进入提示词、检索或沙箱挂载。

4. 安全扫描（规则 + LLM）：
   - 新增 `backend/packages/harness/agentkit/skills/security_scan.py`；
   - 懒 import `skill_scanner`（PyPI 包名 `cisco-ai-skill-scanner`）；
   - 一次扫描同时启用规则分析（static / bytecode / pipeline / correlation）和
     `LLMAnalyzer`；
   - LLM 复用 `agents.default_model`（未配置时取 `models[0]`）的
     `model / base_url / api_key`，走 OpenAI 兼容路由；没有默认模型返回 503；
   - LLM 分析器未运行或报错时整次扫描记 `failed`，不接受纯规则结论；
   - 在 `asyncio.to_thread` 中扫描该版本不可变 Git commit 物化出的临时目录；
   - 新增 `skill_security_scans` 表，保存
     `version_id / package_sha256 / status / is_safe / max_severity / findings_json /
     scanner_version / duration_ms / error`；
   - 新增 `POST /api/skills/{id}/scan` 和 `GET /api/skills/{id}/scans`；
   - 未安装扫描器返回 503 `skill_scanner_unavailable`。

5. 生效门禁：
   - `POST /api/skills/{id}/activate` 只允许最新已保存版本；
   - 必须存在该版本最新一次 `completed && is_safe` 的扫描；
   - 生效不写 Git（commit/push 已在保存阶段完成）；
   - 事务内重校验 scan/version/hash/owner 后写 `skill_private_active`、outbox
     `private:<owner>:<skill_id>`、审计；
   - 路由立即刷新快照并 best-effort 调用 `outbox.run_once()`；
   - 保存 v2 不会改变 v1 的 active 指针，v2 必须重新扫描和生效。

6. 前端：
   - `/workspace/skills` 只展示当前用户技能 + 内置技能；
   - 编辑器按钮改为：保存 / 安全扫描 / 生效 / 删除；
   - 展示扫描时间、严重度、findings、扫描器版本、错误；
   - 保存后状态徽标显示“草稿 · 待扫描 / 草稿 · 扫描通过 / 草稿 · 扫描未通过 / 已生效”；
   - 删除审核页面、审核 API client、侧栏审核入口、`reviewActive/isAdmin`。

7. 迁移与依赖：
   - 新增 `0011_user_isolated_skills`：建 `skill_security_scans`，删除公共/审核表，
     PostgreSQL 删除 `skills.public_name`；SQLite 因外键重建风险保留该不再映射的旧列；
   - 历史 `0005` 的索引创建增加列存在性检查，兼容“已是新形状再 stamp 旧 revision”的库；
   - `agentkit[skills]` 增加 `cisco-ai-skill-scanner>=2.2,<3`（实际锁定 2.3.2）；
   - 测试 dev 依赖改为 `fakeredis[lua]`，恢复 Redis bridge EVALSHA 支持。

## 二次收尾（继续会话）

- 删除技能时在刷新快照后立即 best-effort 执行一次 outbox（`_drain_index_outbox`），
  避免已删除技能在 Milvus 里留到下一次轮询；新增路由测试断言顺序
  `refresh -> outbox`。
- 生效回显区分三种索引状态：`indexed=true` = 索引已就绪；
  `indexed=false` = 后台重试；`indexed=null` = 当前部署未配置向量索引。
- 清理已无入口的全局开关实现：删除 `patch_skill_enabled`、`_scope_value`、
  `SkillUpdateRequest`，同步更新 catalog/README/错误文档/旧 M7 设计的历史说明。

## 保存语义修正

曾尝试“保存只写本地快照、生效才上传 Git”（`skill_version_files` + 迁移 `0012`），
按反馈已整体撤回：删除该表、迁移、store 方法、`SkillGitUnavailable` 与相关测试，
恢复“保存即 Git commit/push + 不可变版本；扫描该 commit；生效只切 active 指针”。

## 验证结果（全绿）

~~~text
uv run python -m pytest backend/tests/test_skill*.py \
  backend/tests/test_skills_catalog_routes.py backend/tests/test_search_and_load_skills_tool.py \
  backend/tests/test_m3_routes.py backend/tests/test_mcp_skills_routes.py -q
380 passed, 3 skipped in 56.88s
~~~

全量 `pytest backend/tests -q` 当前有 3 个与技能无关的 OpenViking 测试失败
（工作区同时存在其他未提交改动），不是本次扫描改动引入。

~~~text
cd frontend
pnpm test        # 35 files / 309 tests passed
pnpm typecheck   # passed
pnpm lint        # passed
pnpm build       # passed；/workspace/admin/skills 已不存在
~~~

真实扫描器已手工验证：

~~~text
cisco-ai-skill-scanner 2.3.2
scan_skill 可调用
规则 + LLM 组合扫描（本地假 OpenAI 端点，模型 fake-model）
-> status=completed, is_safe=true, 规则发现 2 条 LOW
-> 命中 1 次 POST /v1/chat/completions，请求 model=fake-model
~~~

## 关键文件

- 设计：`docs/superpowers/specs/2026-10-23-skill-package-git-release-design.md`
- 扫描器适配：`backend/packages/harness/agentkit/skills/security_scan.py`
- 生命周期服务：`backend/packages/harness/agentkit/skills/author.py`
- 数据模型：`backend/packages/harness/agentkit/skills/store/model.py`
- SQL store：`backend/packages/harness/agentkit/skills/store/sql.py`
- 迁移：`backend/packages/harness/agentkit/persistence/migrations/versions/0011_user_isolated_skills.py`
- HTTP：`backend/app/gateway/routers/skill_drafts.py`
- 前端 API：`frontend/src/core/api/skills.ts`
- 编辑器：`frontend/src/components/skills/skill-editor-page.tsx`

## 后续建议（未做）

1. **提交前 review diff**，尤其是 `uv.lock` 的大量依赖变化。
   加入 scanner 后 `openai` 从 3.x 解析到 2.54、`protobuf`/`huggingface-hub`
   等版本也变化；需确认生产镜像与 LangChain/OpenAI 兼容。
2. **真实 Postgres 迁移演练**：自动化测试只覆盖 SQLite；SQLite 保留
   `skills.public_name` 是有意为之。生产升级前备份公共/审核表数据。
3. **真实网关烟测**：使用实际 `SKILLS_GIT_REPOSITORY`、embedding/Milvus 配置，
   验证保存 → 扫描 → 生效 → Milvus 行 → 检索；当前测试使用 fake projection。
4. **扫描耗时**：规则扫描本机约 2s（热启动），LLM 阶段另加一次默认模型调用；
   目前 HTTP 同步等待（仅放到线程）。模型较慢时接口等待会明显变长，后续可改成
   后台扫描任务 + 前端轮询；当前需求按“点击扫描”实现。
5. **无投影部署**：`embedding.enabled=false` 时没有 outbox，生效后
   `index_status` 会保持 `pending`、`indexed=null`，界面应明确“未配置向量索引”。
6. **UI E2E**：没有跑 Playwright；只跑了 Vitest DOM 与生产构建。
7. **未提交文件**：
   - 本次实现全部未 commit；
   - 工作区里另有与本任务无关的修改：
     `backend/packages/harness/agentkit/agents/memory/openviking/tools.py`、
     `backend/packages/harness/agentkit/subagents/builtins/memory_researcher.py`；
   - 未跟踪的 `myflow-frontend.tar` 也不是本次产物。

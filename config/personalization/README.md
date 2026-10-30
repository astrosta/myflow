# config/personalization

个性化三件套都不进入 OpenViking：`identity.md` / `soul.md` 随代码版本化；`profile.md` 只是种子模板，真实用户画像存 PostgreSQL。

| 文件/位置 | 是什么 | 谁改 | 注入位置 |
|---|---|---|---|
| `profile.md` | **用户身份**种子模板；首次创建时写入 `memory_profile_documents` | 用户本人，通过“我的画像”表单 | 隐藏的 `<personalization-context>` 消息 |
| `identity.md` | **助手身份**：名称、形象、气质、emoji、头像、自我介绍 | 项目维护者，PR 评审 | lead agent 系统提示 |
| `soul.md` | **助手原则**：核心原则、边界、工作方式、风格、连续性 | 项目维护者，PR 评审 | lead agent 系统提示 |

规则：

1. 三者语义不混用：`profile` = 用户是谁，`identity` = 助手是谁，`soul` = 助手怎么做事。
2. `identity.md` / `soul.md` 运行时**只读**；对话、记忆抽取、`EXPLICIT_REMEMBER`、`USER_CORRECTION` 都不会改写它们。
3. `profile.md` 本文件只是**空模板**；首次创建用户 profile 时写入数据库表 `memory_profile_documents`，之后只由表单在 PostgreSQL 事务中写入（行级 etag = sha256）。
4. 数据库 profile 的 YAML frontmatter 是唯一数据源，正文只是渲染结果；手改正文不会生效，下一次写入会被重建。
5. 三个文件都不加入 OpenViking 的 `memory_types`，不参与自动抽取，不产生 `memory_diff`。
6. 文件缺失或 frontmatter 非法时，跳过对应段落并记 warning，不阻塞聊天；数据库 profile 行缺失时由种子模板创建。
7. 运行时不再生成 `data/personalization/`；真实用户画像不得写入仓库文件。
8. `identity.md` 的头像目前为「未设置」；需要时把图片放到 `config/personalization/assets/avatar.png`，并把该行改为相对路径——头像只是给人看的元数据，不会注入提示词。

字段映射（`profile.md` → 内部 `field_path`）：

| 文件 key | field_path |
|---|---|
| `preferred_name` | `identity.preferred_name` |
| `pronouns` | `identity.pronouns` |
| `locale` | `identity.locale` |
| `timezone` | `identity.timezone` |
| `language` | `communication.language` |
| `tone` | `communication.tone` |
| `verbosity` | `communication.verbosity` |
| `format` | `communication.format` |
| `role` | `work.role` |
| `domains` | `work.domains` |
| `tools` | `work.tools` |
| `expertise` | `work.expertise` |
| `accessibility_needs` | `accessibility.needs` |
| `boundaries` | `boundaries` |

设计文档：`docs/plans/2026-10-29-openviking-memory-module-design.md`。

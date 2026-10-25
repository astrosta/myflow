# 技能包 Git 发布、私有创作与审核设计

> 状态：待评审（2026-10-23）
> 本版取代初版中“管理员整仓发布”的模型。现在的主线是：
> 普通用户创作私有技能 → 提交审核 → 管理员批准 → 成为公共技能。
> 不在范围：MCP、知识库、技能市场、跨仓库合并。

## 1. 意图与问题

技能必须同时满足三件事：

1. 技能包由 Git 管理，任何线上内容都能追溯到仓库、commit 和路径；
2. 普通用户可以在应用内创建带 SKILL.md、scripts/、模板和示例的技能包，未审核时只有自己可用；
3. 管理员审核某个具体版本后，该版本成为公共技能，所有用户可用。

现有实现只有“部署机上的目录 + 扫描 frontmatter”，没有所有者、审核、草稿或版本记录。
新模型把技能分成两个可见域：

- **私有域**：作者的草稿和待审核版本，只对作者可见、可检索、可使用；
- **公共域**：管理员批准过的版本，对所有登录用户可见、可检索、可使用。

Git 保存内容，PostgreSQL 保存所有权、版本、审核和激活指针，Milvus 保存可重建的向量投影。
Milvus 只负责候选召回；任何命中都必须回到 PostgreSQL 按当前用户和版本确认。

### 1.1 成功标准

1. 普通用户可以在应用内创建技能包，保存后立即成为自己的私有版本；其他用户看不到、搜不到、

   读不到它的正文或附件。
2. 作者点击提交后，管理员看到的是一个不可变的版本（固定 draft_commit），批准或驳回都

   针对这个版本，不会因为作者继续编辑而审核到另一份内容。
3. 批准后，该版本进入公共主线；所有用户都可以检索和使用，作者之后的编辑只产生新的

   私有版本，不会自动改变公共版本。
4. 每个公共版本都能回答：skill_id、skill_version、git_repository、git_commit、skill_path、

   提交人、审核人、审核时间。
5. 向量库中的私有版本不会泄漏给其他用户；即使 Milvus 出现旧记录或伪造记录，PostgreSQL

   也会按用户和激活版本拒绝。
6. 管理员驳回后，技能仍然是作者的私有技能；作者可以修改后重新提交。

### 1.2 非目标（V1）

- 多仓库合并、跨仓库依赖、技能市场分成。
- 用户直接使用 Git 推送或合并；普通用户不接触 Git 凭据和分支。
- 自动代码审查、恶意脚本沙箱扫描、签名校验。V1 的信任边界是“管理员人工审核”。
- 二进制附件、Git LFS、超大模型文件。V1 允许文本脚本、模板、示例和小型资源文件。
- 团队/组织级可见性。V1 只有 private 和 public 两个可见域。
- 删除 Git 历史。公共发布历史和审核记录永久保留；只清理可重建的缓存和向量。

## 2. 角色、可见性与生命周期

### 2.1 角色

| 角色 | 能力 |
|---|---|
| user | 创建/编辑自己的草稿；保存、提交审核、撤回；检索和使用公共技能 + 自己的私有技能；管理个人偏好 |
| admin | user 的全部能力；查看审核队列；批准/驳回；下架公共技能；回滚公共版本；全局启停 |

权限继续使用现有 require_scope：

- skills:read：查看可见技能、使用检索；
- skills:write：创建/编辑/提交自己的技能；
- admin：审核、公共发布、下架、回滚、全局开关。

普通用户的创建能力可以由 skills.creation.enabled 关闭；关闭后只有 admin 能创建。

### 2.2 两个版本指针

每个技能有两个独立指针：

- 私有指针：作者当前使用的最新私有版本；
- 公共指针：管理员最后一次批准的公共版本。

两者可以不同。典型时间线：

~~~
作者保存 v1           私有=v1，公共=无
提交审核             私有=v1，公共=无，审核中=v1
管理员批准           私有=v1，公共=v1
作者继续编辑 v2      私有=v2，公共=v1
作者提交 v2          私有=v2，公共=v1，审核中=v2
管理员批准 v2        私有=v2，公共=v2
~~~

管理员批准后，作者如果只想使用公共版本，可以删除自己的私有草稿指针；
但公共版本不受影响。

### 2.3 审核状态

单个提交记录的状态：

- pending：已提交，等待审核；版本冻结，作者不能修改该版本；
- approved：审核通过，已创建公共发布记录；
- rejected：审核驳回；私有版本仍可用，作者可修改并重新提交；
- withdrawn：作者主动撤回；可重新提交；
- superseded：同一技能又提交了新版本，旧提交被取代（仅用于历史）。

作者在 pending 期间调用保存会返回 409，必须先撤回。这样可以保证管理员看到的版本就是
最终被批准的版本。

## 3. 总体架构

~~~
普通用户工作台 / 管理员审核台
   │ 创建、编辑、提交、批准
   ▼
Gateway /api/skills/*
   │ 暂存表 → 服务端 Git commit/push
   ▼
Git：用户草稿分支 + main 公共目录
   │
   ▼
PostgreSQL：skills / versions / submissions / releases / active / outbox
   │ outbox 异步更新
   ▼
Milvus active projection：公共技能一行 + 私有技能一行
   │ 检索：scope/owner 过滤 + 向量匹配
   ▼
PostgreSQL 确认 → 合并去重（作者私有优先）→ Git 缓存正文
~~~

### 3.1 不可破坏的约束

- 普通用户创建的未审核内容绝不进入 main，也不进入公共目录。
- 服务端是唯一写 Git 的一方；用户不持有 Git 凭据，不直接操作分支。
- 审核针对不可变版本；管理员批准的不是“当前文件”，而是某个 submitted_commit。
- 批准会把该版本完整复制到 main，公共内容不依赖作者草稿分支继续存在。
- 向量库只保存可重建的投影；私有/公共可见性和审核状态只存在 PostgreSQL。
- 私有正文、附件和容器路径只对所有者开放；管理员只为审核目的读取。

## 4. Git 拓扑与草稿存储

### 4.1 仓库和分支

一个配置的技能仓库（V1 单仓库），由服务端读写：

~~~
main
  public/<public_name>/SKILL.md
  public/<public_name>/scripts/...
  public/<public_name>/templates/...
  public/<public_name>/examples/...

refs/heads/myflow/drafts/<user-id>
  users/<user-id>/<skill_id>/SKILL.md
  users/<user-id>/<skill_id>/scripts/...
  users/<user-id>/<skill_id>/templates/...
  users/<user-id>/<skill_id>/examples/...
~~~

- 私有技能用内部 UUID skill_id 作为目录，避免作者改名/重名破坏路径。
- 公共技能用 public_name 作为目录；public_name 全局唯一。
- 草稿分支只由服务端写入，普通用户和浏览器看不到 Git 凭据。
- 草稿分支可以包含同一用户的多个技能；每次保存只提交一个技能的目录变更。

### 4.2 服务端 Git 写入

先明确两个位置：**已经保存但未审核的草稿包在 Git 草稿分支**；**尚未点击保存的编辑在
PostgreSQL 的 skill_draft_files 暂存表**。后者只是编辑器缓冲区，不是技能版本，不进入
检索、审核或公共发布。

V1 使用系统 git CLI，通过受限参数和环境执行。编辑暂存和 Git 提交分成两层：

- 未保存的编辑和附件上传写入 PostgreSQL 的 skill_draft_files 暂存表；
这是编辑器缓冲区，不是技能版本，不参与检索、审核或公共发布；
- 点击“保存”时，在 PostgreSQL advisory lock 下从远端拉取草稿分支，使用临时工作区
物化暂存文件并提交；
- 保存步骤：
  1. 校验路径、文件类型、大小、数量和符号链接；
  2. 解析 SKILL.md、计算 package_sha256 / skill_md_sha256；
  3. 在临时工作区 git add + git commit，并 push 到远端草稿分支；
  4. 写入 skill_versions 和 skill_private_active；
  5. 写 skill_index_outbox（upsert private active）；worker 生成 embedding 并 upsert
     Milvus active projection 后，私有版本可被检索。
- 不在 worker 本地保存跨请求的用户工作区：每个 worker 都用临时工作区，保存前 fetch
远端分支，保存后 push；这样多 worker 下不会因为请求落到不同实例而丢失未提交编辑。
- Git 作者/提交者固定为 MyFlow 服务身份；commit message 可以带 user_id、skill_id、
version，不写用户邮箱。
- 禁止 Git hook、交互式认证和用户提供的命令参数；所有路径由服务端生成。
- 保存按 skill 加锁（进程内锁 + PostgreSQL advisory lock）；公共批准按公共分支锁。
- 未保存编辑在暂存表中持久化；已保存版本在 Git 中。服务重启不会丢掉暂存内容。

### 4.3 附件和包边界

- 一个技能是 users/// 整个目录；SKILL.md 是入口。
- scripts/、templates/、examples/ 随包提交，服务端不执行任何脚本。
- 拒绝符号链接、硬链接、设备文件、绝对路径、.. 路径和目录逃逸。
- V1 默认限制：单包 5 MiB、单文件 1 MiB、最多 200 个文件；文本/脚本允许，二进制只允许
小型资源文件。限制由配置覆盖。
- 审核页必须展示完整文件列表和 diff；脚本内容属于人工审核范围。

### 4.4 审核通过后的公共 commit

管理员批准时：

1. 从草稿分支读取被批准的 submitted_commit 和 skill_path；
2. 重新校验 submitted_commit 的包 hash、frontmatter、文件边界和 public_name 冲突；
3. 在 main 上创建权威的批准 commit，把技能目录复制到 public//，并 push 到远端；
4. 记录 source_commit=submitted_commit、git_commit=main commit、skill_path=public 路径；
5. 公共版本不依赖草稿分支，作者删除草稿也不影响公共技能。

批准 commit 只包含被批准的一个技能（V1 一次审核一个技能；批次表允许未来合并多个）。

### 4.5 运行时缓存与沙箱

-  是节点本地缓存，不要求共享文件系统；每个 worker 可从 PG + Git 重建；
- /commits///：从 Git 导出的不可变内容，校验 hash；
- /active/public/：公共激活树的只读副本；
- /active/users//：该用户私有激活树的只读副本；
- 公共技能挂到 /mnt/skills/public，私有技能挂到 /mnt/skills/private；
- /mnt/skills/private 必须是按 sandbox/用户挂载的，不允许进程级静态挂载给所有用户；
- 如果某个 sandbox provider 不支持按用户挂载，则私有技能不提供容器路径，只通过
search_and_load_skills / slash 注入正文；不能退化成全局可读目录。

## 5. 数据模型

新增十一张表。所有时间使用 UTC。

### 5.1 skills

技能身份。名字可以改，skill_id 不变。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | String(64) PK | 内部 UUID，API 的 skill_id |
| owner_user_id | String(64) | 创建者/所有者 |
| private_name | String(128) | 作者当前名字；同一 owner 内唯一 |
| public_name | String(128) nullable | 首次批准时确定；全局唯一，批准后不可随意改 |
| created_at / updated_at | DateTime(tz) | 创建/更新时间 |
| deleted_at | DateTime(tz) nullable | 私有草稿软删除；不删除公共历史 |

约束：

- UNIQUE(owner_user_id, private_name) WHERE deleted_at IS NULL；
- UNIQUE(public_name) WHERE public_name IS NOT NULL；
- public_name 为空表示还没有公共版本；一旦批准就保留，即使之后下架也不释放名字。

### 5.2 skill_versions

技能内容的不可变版本，版本号由发布系统生成。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | String(64) PK | version_id；active projection 的版本字段 |
| skill_id | String(64) FK | 所属技能 |
| skill_version | String(32) | 发布系统生成 v1、v2… |
| version_seq | Integer | 每个 skill 单调递增 |
| package_sha256 | String(64) | 整个技能目录的 hash；版本去重 |
| skill_md_sha256 | String(64) | SKILL.md hash；embedding 与校验 |
| metadata_json | JSON | frontmatter 元数据快照 |
| draft_ref | String(255) nullable | 草稿分支 ref；bootstrap 公共技能可为空 |
| draft_commit | String(40) nullable | 首次保存该内容的草稿 commit；bootstrap 可为空 |
| skill_path | String(1024) | 草稿分支内 SKILL.md 路径；bootstrap 为 main 路径 |
| index_status | String(16) | pending / ready / failed |
| created_at | DateTime(tz) | 版本创建时间 |

约束：

- UNIQUE(skill_id, skill_version)；
- UNIQUE(skill_id, package_sha256)：同一内容不重复升版；
- 版本行不可更新内容；index_status 可以变化。

注意：skill_version 是每个技能内部递增的编号，不同技能可以同时是 v1。全局唯一定位
一律用 version_id；skill_version 必须和 skill_id 一起使用。

版本生成：

1. 保存时对技能目录计算 package_sha256：按 POSIX 相对路径排序，包含文件字节和可执行位，

   排除临时文件和系统垃圾；
2. 若相同 hash 已存在，复用原 skill_version；版本被重新激活时重新 embedding 并
   upsert active projection，不假设历史向量仍然存在；
3. 否则取该 skill 最大 version_seq + 1，生成 v{n}；
4. 版本只增不改；内容错误只能保存新版本。

复用版本时，提交审核使用**本次提交时的草稿分支 HEAD**，而不是版本最早保存时的
旧 commit；这样即使版本记录是旧的，审核和批准仍然落在包含该内容的当前 commit 上。

### 5.3 skill_submissions

一次提交审核。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | String(64) PK | 提交 ID |
| skill_id | String(64) FK | 技能 |
| version_id | String(64) FK | 被审核的不可变版本 |
| submitted_commit | String(40) | 提交那一刻的草稿分支 HEAD；审核/批准的源 commit |
| owner_user_id | String(64) | 提交人 |
| status | String(16) | pending / approved / rejected / withdrawn / superseded |
| submitted_at | DateTime(tz) | 提交时间 |
| reviewed_by | String(64) nullable | 审核人 |
| reviewed_at | DateTime(tz) nullable | 审核时间 |
| reject_reason | Text nullable | 驳回理由 |
| public_release_id | String(64) nullable | 批准后生成的公共发布记录 |
| created_at | DateTime(tz) | 创建时间 |

约束：

- 部分唯一索引：一个 skill 同时最多一个 pending submission；
- 一个 version 可以有多条历史 submission，但只有一条 approved 可以指向公共 release。

### 5.4 skill_release_batches

一次公共批准产生的仓库 commit 批次。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | String(64) PK | 批次 ID |
| git_repository | String(512) | 规范化仓库 URL |
| git_commit | String(40) | main 上的批准 commit |
| approved_by | String(64) | 管理员 |
| created_at | DateTime(tz) | 时间 |
| stats_json | JSON | 文件数、大小、耗时 |

V1 一次审核一个技能；批次表和 release 表的形状允许未来批量批准。

### 5.5 skill_releases

公共发布记录。需求要求的五个字段在这里。

| 字段             | 类型                  | 说明                                  |
| -------------- | ------------------- | ----------------------------------- |
| id             | String(64) PK       | 发布记录 ID                             |
| batch_id       | String(64) FK       | 批次                                  |
| version_id     | String(64) FK       | 内容版本                                |
| skill_id       | String(64)          | 技能 UUID                             |
| skill_version  | String(32)          | 版本号                                 |
| git_repository | String(512)         | 仓库 URL                              |
| git_commit     | String(40)          | main 批准 commit                      |
| skill_path     | String(1024)        | public//SKILL.md                    |
| public_name    | String(128)         | 批准时的公共名字                            |
| source_commit  | String(40) nullable | 审核时的 submitted_commit；bootstrap 可为空 |
| approved_by    | String(64)          | 管理员                                 |
| created_at     | DateTime(tz)        | 发布时间                                |

约束：

- UNIQUE(batch_id, skill_id)；
- 索引：(skill_id, created_at)、git_commit、skill_version；
- 发布记录不可变；回滚只改激活指针，不改 release。

### 5.6 skill_public_active 与 skill_private_active

公共激活指针：

| 字段 | 类型 | 说明 |
|---|---|---|
| skill_id | String(64) PK | 技能 |
| release_id | String(64) FK | 当前公共 release |
| activated_by | String(64) | 管理员 |
| updated_at | DateTime(tz) | 时间 |

私有激活指针：

| 字段 | 类型 | 说明 |
|---|---|---|
| skill_id | String(64) PK | 技能 |
| owner_user_id | String(64) | 所有者，冗余用于过滤 |
| version_id | String(64) FK | 当前私有版本 |
| updated_at | DateTime(tz) | 时间 |

没有公共激活行 = 该技能当前不是公共技能；没有私有激活行 = 作者当前没有私有可用版本。
管理员下架只删公共指针；作者删除草稿只删私有指针。

### 5.7 skill_settings 与 skill_audit_events

skill_settings：公共技能的全局开关。

| 字段 | 类型 | 说明 |
|---|---|---|
| skill_id | String(64) PK | 公共技能 |
| enabled | Boolean | 全局启用 |
| updated_by | String(64) | 管理员 |
| updated_at | DateTime(tz) | 时间 |

私有技能不受全局开关影响；作者自己的个人偏好仍然生效。

skill_audit_events：append-only 管理/审核审计。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | String(64) PK | 事件 ID |
| actor | String(64) | 操作人 |
| action | String(32) | skill.create / skill.save / skill.submit / skill.withdraw / skill.approve / skill.reject / skill.unpublish / skill.activate / skill.enable / skill.disable / skill.delete |
| skill_id | String(64) nullable | 技能 |
| submission_id | String(64) nullable | 提交 |
| release_id | String(64) nullable | 公共发布 |
| detail_json | JSON | 版本、commit、变更前后值、驳回理由摘要 |
| created_at | DateTime(tz) | 时间 |

草稿保存的完整内容历史在 Git；审计表记录生命周期动作。普通用户的个人偏好不进入审计表。

### 5.8 skill_draft_files

未保存的编辑暂存。它不是技能版本，也不进入 Git、向量库或审核接口。

| 字段 | 类型 | 说明 |
|---|---|---|
| skill_id | String(64) FK | 技能 |
| path | String(1024) | 相对技能目录的 POSIX 路径 |
| content | LargeBinary | 文件字节；文本/脚本/小型资源 |
| sha256 | String(64) | 内容 hash |
| size_bytes | Integer | 文件大小 |
| mode | String(16) | text / binary |
| updated_at | DateTime(tz) | 最后编辑时间 |

约束：

- PRIMARY KEY(skill_id, path)；
- 只允许 owner 通过作者接口读写；admin 审核读取的是已保存版本，不读暂存表；
- 保存时把暂存表物化到临时 Git 工作区；保存成功后暂存表保留为当前工作副本；
- 暂存表也计入每用户配额；删除草稿时一起删除；
- 暂存表只存在私有内容，日志和审核接口不输出其正文。

### 5.9 skill_index_outbox

Milvus active projection 的可靠更新队列。它和业务状态变更写在同一个 PostgreSQL 事务里。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | String(64) PK | 事件 ID |
| op | String(16) | upsert_active / delete_active |
| projection_key | String(255) | public:<skill_id> 或 private:<owner_id>:<skill_id> |
| scope | String(16) | public / private |
| owner_user_id | String(64) nullable | 私有行所有者；公共行为空 |
| skill_id | String(64) | 技能 |
| version_id | String(64) nullable | 要投影的版本；delete 时可为空 |
| payload_json | JSON | enabled、name、description、hash 等非正文元数据 |
| status | String(16) | pending / running / done / failed |
| attempts | Integer | 重试次数 |
| next_attempt_at | DateTime(tz) | 下次重试 |
| last_error | Text nullable | 脱敏错误 |
| created_at / updated_at | DateTime(tz) | 时间 |

- 发布、批准、下架、回滚、全局开关、私有保存/删除都在同一 PG 事务里写 outbox；
- worker 幂等 upsert/delete Milvus；失败指数退避；
- 同一 projection_key 的多个 pending 事件由 worker 合并为最新状态；worker 应用事件前
  重新读取 PG 当前 active/settings，事件里的 version_id/enabled 已过期就跳过，避免旧
  outbox 覆盖新状态；
- 启动和每日对账扫描 PG active 指针，补齐缺失行、删除孤儿行；
- PG 仍是权威；Milvus 落后时检索可能短暂少结果，但不会返回未确认内容。

### 5.10 与旧 JSON 状态的关系

- extensions_config.json 继续服务 MCP；skills 段在新目录启用后不再读取；
- 全局技能开关迁移到 skill_settings，按公共 skill_id 建键；
- skills_prefs.json 继续保存个人偏好，但键从“技能名”改为内部 skill_id，避免私有/公共重名；
- 一次性脚本 scripts/migrate_skill_state.py 负责按旧名字找到对应公共 skill_id 并导入开关；
脚本可重复执行，不在启动时偷偷写库；
- 旧公共技能通过管理员 bootstrap 导入，给予 admin 所有者和已批准状态；
- 切换后生产运行时**只认 Git 目录**，不再合并本地 skills/ 扫描；source=filesystem 仅用于
测试/本地调试，不是生产回退；
- 启动不要求 Git 在线：已有 PG 激活记录 + 本地 commit 缓存即可服务；只有保存/批准/缓存
重建需要访问远端；Git 不可达时新写入失败，已有技能不受影响。

## 6. 创建、保存、提交与审核

### 6.1 创建草稿

1. 用户在“我的技能”点击新建，填写 private_name 和 description；
2. 服务端创建 skills 行（owner=当前用户）；
3. 生成初始 SKILL.md，写入 skill_draft_files 暂存表；
4. 不立即创建版本；用户保存时才 commit 到 Git。
5. private_name 不能与其他技能已占用的 public_name 冲突（同一 skill 除外），否则创建/改名失败。

### 6.2 编辑和保存

- 编辑器支持 SKILL.md 正文、文件树、文本文件、附件上传和删除；
- 所有编辑先写 skill_draft_files 暂存表，点击保存时执行 §4.2 的 Git commit；
- 保存成功后：生成/复用 version，写 skill_versions，更新 skill_private_active，
  并在同一事务里写 private active 的 outbox；
- worker 完成 embedding 和 active projection upsert 后，私有版本才进入向量检索；
  prompt/slash/容器读取不依赖向量，可立即使用；
- 如果保存时存在 pending submission，返回 409；作者必须先撤回。

### 6.3 提交审核

1. 作者点击提交；服务端检查包合法、没有 pending submission；
2. 读取草稿分支 HEAD，校验该 commit 的包 hash 等于 version.package_sha256；
3. 创建 skill_submissions（pending），记录 version_id 和 submitted_commit=HEAD；
4. 私有版本仍对作者可用；
5. 管理员审核页看到的是固定 version + submitted_commit，不是作者暂存表；
6. 向量索引未就绪不阻止提交：正文仍可通过 prompt/slash 使用，active projection 完成后
   检索自动可见；返回结果里带 index_warning。

### 6.4 管理员审核

审核页展示：

- 作者、技能名、版本、submitted_commit、source_commit；
- 与当前公共版本的 diff；
- SKILL.md 全文、frontmatter、allowed-tools、required-secrets；
- scripts/、templates/、examples/ 的文件列表和内容；
- 包大小、文件数、hash。

批准：

1. 校验提交仍是 pending，submitted_commit 的包 hash 与版本一致；
2. 检查 public_name 冲突；冲突返回 409，要求作者改名后重新提交；
3. 按 §4.4 从 submitted_commit 创建 main 批准 commit；
4. 在 PostgreSQL 事务中写 release、public_active、settings 默认 enabled=true，并写
   public active 的 outbox（upsert）；
5. 提交置 approved，写审计；
6. 刷新公共快照和该用户的私有快照；worker 完成公共 active projection 后，所有用户
   的向量检索可见。

驳回：

- 写 rejected、reviewed_by、reject_reason；
- 不改变私有激活指针，作者可编辑后重新提交；
- 审计留痕。

### 6.5 修改已批准技能

- 作者保存新版本 v2 后，私有指针指向 v2，公共指针仍是 v1；
- 其他用户继续使用 v1；
- 作者提交 v2，管理员批准后公共指针切到 v2；
- 公共 v1 的 release 记录和 Git commit 永久保留。
- V1 不提供公共改名：public_name 在首次批准后固定，作者修改 private_name 只影响私有
版本，不影响公共路径；必须改公共名时由管理员下架后作为新技能重新发布。

### 6.6 下架和回滚

- 管理员下架：删除 skill_public_active，保留 release 和 public_name，并写 public active
  的 outbox（delete）；对**下一次 run / 下一次检索**生效；当前 run 已经注入上下文的
  正文无法回收，这是所有上下文注入的共同限制，不假装能撤回；
- 管理员回滚：把 skill_public_active 指向历史 release，并写 public active 的 outbox
  （upsert；worker 必要时重新 embedding）；不改 Git、不改 release；同样对下一次 run 生效；
- 下架/回滚后，作者私有指针不受影响；
- 重新批准时，公共指针切到新 release。

### 6.7 管理员创建

管理员拥有 skills:write，可以创建私有草稿；管理员可以审核自己的提交（self_approved=true
记录在审计中），也可以由另一个管理员审核。V1 不允许绕过审核直接写公共指针。

## 7. 向量索引与检索

### 7.1 Active Projection

Milvus collection 默认名 myflow_skills_active。它**不是全量版本索引**，而是当前激活
状态的物化视图：

- 每个公共 active 技能一行；
- 每个用户每个私有 active 技能一行。

这样检索过滤器只需要 scope/owner，不需要把上千个 version_id 拼进 IN。

| 字段 | 类型 | 用途 |
|---|---|---|
| pk | VARCHAR | public:<skill_id> 或 private:<owner_id>:<skill_id> |
| scope | VARCHAR | public / private |
| owner_user_id | VARCHAR | 私有行所有者；公共行为空串 |
| skill_id | VARCHAR | 确认/去重 |
| version_id | VARCHAR | 指向版本 |
| skill_version | VARCHAR | 调试/确认 |
| enabled | BOOL | 公共全局开关；私有行恒为 true |
| name | VARCHAR | 调试 |
| description | VARCHAR | 调试 |
| package_sha256 | VARCHAR | 重建校验 |
| skill_md_sha256 | VARCHAR | 内容校验 |
| embedding | FLOAT_VECTOR | 向量 |
| embedding_model | VARCHAR | 模型漂移 |
| embedding_dimensions | INT | 维度 |
| updated_at | INT64 | 投影更新时间 |

- 版本历史不常驻向量库；历史版本只在 Git + PostgreSQL。回滚或重新激活旧版本时，
  重新 embedding 后 upsert 对应的 active 行。
- 同一个 version_id 可以同时出现在 public 行和作者的 private 行；pk 是 projection key，
  不是 version_id。
- scope/owner_user_id/enabled 是可变的投影字段。这是为了避免大 IN 列表刻意接受的
  折中：PostgreSQL 仍是权威，outbox 负责更新，检索后仍要 PG 确认。
- 不存正文和附件。
- scope、owner_user_id、enabled 建 scalar index；公共/私有两条过滤路径不依赖大 IN。
- consistency_level 默认 Strong；outbox 写入成功后，跨 worker 的检索应尽快看到。
- 维度或模型变化走全量重建 active projection。

### 7.2 Embedding 文本

~~~
{name}
{description}
~~~

只有**作者手写的元数据**参与 embedding，正文不参与。理由：

- 元数据是作者对“这个技能处理什么任务”的摘要，正是语义入口；
- 正文每次改动都会变，嵌正文等于每次保存都重新 embedding 一次，成本高且噪声大；
- 正文可能超出 embedding 的 max_input_tokens，超限时整条索引失败
  （index_status=failed）而不是截断；嵌元数据从结构上消除了这条失败路径。

正文不进向量，进的是另外两段：BM25 词法召回（§7.4 步骤 5）读正文，精排（§7.4
步骤 7）也读正文。归纳成一句：**语义召回靠元数据，字面召回与终排靠正文。**

代价是召回天花板取决于 description 的质量。因此创建/编辑界面必须把“适用任务 +
关键工具/库名/报错词”写进描述引导里；系统提示词索引展示的同样是 name + description，
两处口径因此一致。

附件不进入 embedding，也不进入召回；它们只在正文里被引用。正文的用量由两个预算
分别约束：max_body_chars 管“装载进上下文”，max_document_chars 管“送进精排”。

> 改这一段的语义（换成嵌正文、改前缀格式、换模型）等于让全部存量向量失效，必须配
> 一次强制重建，见 §7.6。

### 7.3 可见性和冲突规则

对当前用户，可见集合由两路组成：

- 公共：scope=public、enabled=true 的 active 行；
- 私有：scope=private、owner_user_id=当前用户的 active 行。

同一个 skill_id 可能同时有公共行和作者的私有行。**作者侧私有版本优先**：

- 作者有私有 active 版本时，公共分支直接排除该 skill_id，结果只保留私有；
- 私有 active 版本存在但投影暂时缺失/失效时，该技能在作者检索里暂不出现，
  outbox/对账修复后恢复；不自动回退公共版本；
- 其他用户看不到该私有行，只看到公共行。

这样即使用户更新后的私有版本和公共版本 embedding 高度相似，也只会返回一个技能，
不会出现“同一技能两个版本同时命中”的冲突。作者侧**无条件私有优先**：不考虑两边
分数谁高，只要私有版本有效就保留私有；不提供本次检索切换到公共版本的参数。
作者要使用公共版本，删除私有草稿即可。

个人偏好、agent 白名单、全局 enabled 仍由 PostgreSQL 最终确认；Milvus 的 enabled 字段
只是减少候选。

### 7.4 检索顺序

三阶段漏斗：**稠密召回（元数据向量）→ 稀疏召回（正文 BM25）→ 精排（正文**
**cross-encoder）**。每一段都只做自己最擅长的事，越贵越靠后。

1. 生成 query embedding（只生成一次），查询文本是模型的 task_query；
2. 从当前用户快照读取私有 active 的 skill_id 集合（数量受草稿配额限制，通常很小；
   不需要额外 DB 查询）；
3. **稠密召回**：Milvus **一次 search**，过滤表达式：
   ~~~
   (scope == "public" and enabled == true and skill_id not in [...私有 skill_id...])
   or (scope == "private" and owner_user_id == 当前用户)
   ~~~
   limit = candidate_k；私有集合为空时只保留公共分支。
4. 对命中批量回 PostgreSQL（一条 JOIN，不做 N+1），逐条确认：
   - 公共：skill_public_active 指向的 release.version_id 命中，且 settings.enabled=true；
   - 私有：skill_private_active.owner_user_id=当前用户且 version_id 命中；
   - 版本 hash、skill_id、skill_version 一致；
   - 个人偏好、agent 白名单通过；
   丢弃失效命中。Milvus 只是候选来源，确认之后才存在“技能”，这一步不能省。
5. **稀疏召回**：对同一份 PG 可见集合做本地 BM25（正文 + name + description），
   得分来自 keyword 通道配置。正文从 Git 缓存读，按 (commit, path) 做进程内 LRU，
   避免每次检索都 O(可见技能数) 次磁盘读；读不出正文的候选丢弃并记 warning。
6. **合并**：按 skill_id 防御性去重（私有优先，无条件），再对两路各自的名次做 RRF
   融合（rrf_k）。融合分数只用于排序，不改变可见性；min_score 作用在融合分数上。
7. **精排**：对融合后的前 candidate_k 个候选，用正文（截断到 max_document_chars）
   送 cross-encoder rerank，得到终排。精排只重排**已去重**的集合，因此不可能把作者
   的公共副本排到自己的私有副本前面。
8. 取 top_k，按 max_body_chars / max_context_chars 预算装载正文；
9. 返回 skill_id、name、scope、skill_version、version_id、git_commit、skill_path、
   release_id（公共）、score、命中的通道和 warning。

代价与取舍：BM25 要覆盖正文就必须读到正文，这是这条链上唯一随目录规模增长的开销
（步骤 5 的 LRU 是它的对冲）；精排是每次检索一次网络往返（约 2×候选数 × 4k 字符），
换来的是“只往上下文里装 top_k 个正文”时的命中率。两者都比“装错技能、走错流程”便宜。

这里仍然只有一次 Milvus search，query embedding 也只生成一次。私有分支用
`owner_user_id == 当前用户` 过滤；公共分支用 `skill_id not in [我的私有 skill_id]`
把作者自己的公共版本排除掉，所以同一技能不会同时出现两个版本。

`not in` 列表是“当前用户私有 active 的 skill_id”，不是 version_id；它受草稿配额限制，
通常只有几十个，不会形成大集合。无条件私有优先意味着：私有版本在投影中有效时，
作者不会通过检索回退到公共版本；要使用公共版本，删除私有草稿即可。

去重仍然保留为防御措施：如果 Milvus 出现重复/过期行，PG 确认后再按 skill_id 收敛。

### 7.5 降级

- 精排不可用（超时/5xx/协议错误）：跳过步骤 7，直接用融合顺序，返回 rerank_unavailable
  并记 warning；精排永远不阻断检索，也不改变可见性；
- 向量通道不可用（embedding 或 Milvus）：跳过步骤 1/3/4，只用 BM25 对 PG 可见集合排序，
  返回 lexical_fallback 和 vector_unavailable，并按同一套私有优先规则去重；
  因为 BM25 读的是正文，这条路仍然能命中“报错原文/库名”类查询；
- BM25 通道异常（正文读不出）：只用向量分数排序，返回 lexical_unavailable；
  全部通道都不可用时 fail-safe：返回空结果和原因，不返回未排序的候选；
- PostgreSQL 不可用：fail-closed，不返回任何未确认的命中；
- 内容缓存丢失：从 Git mirror 按 commit 重导出；失败则丢弃该条；
- active projection 缺失/过期：outbox 重试和对账任务修复；修复前检索可能少结果，
  prompt/slash 仍可用；
- 个人偏好关闭的公共技能可能占用候选名额；candidate_k 允许过采样，确认阶段丢弃；
- 所有降级显式返回，不静默隐藏。

### 7.6 投影文本版本与强制重建

投影行是**派生数据**，唯一让它失效的方式是重新 embedding。因此 embedding 文本的
语义变更（§7.2 的格式、模型、维度）必须视为一次数据迁移，而不是一次代码改动。

- 代码里维护一个 PROJECTION_TEXT_VERSION；它与某个 active 版本的“已索引文本版本”
  不一致时，该行的可检索性是未知的；
- 下面三种情况都会让存量向量失去意义：改嵌入文本、换 embedding 模型、换维度。
  三者都必须走**全量重建**，不能依赖 outbox（outbox 只处理新的状态变更）；
- 对账任务在稳态下会跳过“key 在 Milvus 且版本 ready”的行（这是为了不每十分钟
  重嵌整个目录），所以**只改代码不会自愈**：必须显式触发重建；
- 提供管理员入口 POST /api/admin/skills/reindex（skills:write + 审计），语义是
  reconcile(force=True)：重建全部 active 行、删除多余 key、把版本 index_status 置回
  ready；返回修复/删除计数。运维在换模型或换文本格式后调用一次即可；
- 重建期间检索仍然可用：旧向量与新向量在 collection 里共存，Milvus 只是候选来源，
  最终仍由 PG 确认；重建完成后旧行被覆盖或删除。

## 8. 运行时快照与沙箱隔离

### 8.1 按用户快照

中间件和系统提示词是同步路径。SkillCatalogService 为每个用户构建不可变快照：

- 快照包含公共可见技能 + 该用户自己的私有技能；
- 一次 run 使用同一快照，保证索引、策略、激活一致；
- 保存、提交、批准、下架、回滚、全局开关后按 epoch 失效；
- 多 worker 通过 PG catalog_epoch 和短轮询/reload 刷新；
- 检索工具不依赖快照，每次按 PG 现查。

### 8.2 Skill 运行时字段

增加：

~~~
skill_id
scope: private | public
owner_user_id
skill_version
version_id
release_id (public only)
git_repository
git_commit
skill_path
container_file
~~~

保留 name/description/category/allowed_tools/required_secrets，让 middleware、slash、
DurableContext 和 tool policy 继续用路径协议。

### 8.3 容器路径

公共技能：

~~~
/mnt/skills/public/<public_name>/SKILL.md
~~~

私有技能：

~~~
/mnt/skills/private/<private_name>/SKILL.md
~~~

- 私有路径在容器里不带 user_id；同一个字符串在每个用户的 sandbox 中映射到不同主机目录；
- 只有所有者的 sandbox 挂载私有目录；
- 提示词索引分别说明 public/private 两个根；
- slash 同名时私有优先；公共技能仍可被 search_and_load_skills 显式命中；
- 如果 sandbox provider 无法按用户挂载私有目录，私有 skill 不写 container_file，
只通过工具/slash 注入正文，绝不做全局只读挂载。

### 8.4 激活树

- 公共激活树按公共 release 生成，全进程共享；
- 私有激活树按用户生成，内容是该作者当前私有版本的只读副本；
- 内容来自 commit 缓存，按技能目录原子替换；
- 激活树是缓存，不是权威；短暂落后不影响检索确认。

## 9. HTTP API

### 9.1 作者

| Method | Path | 权限 | 说明 |
|---|---|---|---|
| GET | /api/skills | skills:read | 可见技能 = 公共 + 自己的私有 |
| GET | /api/skills/mine | skills:read | 我的私有技能和公共状态 |
| POST | /api/skills | skills:write | 创建私有草稿 |
| GET | /api/skills/{skill_id}/draft | skills:write | 读取自己的暂存草稿 |
| GET | /api/skills/{skill_id}/draft/tree | skills:write | 暂存文件树 |
| GET | /api/skills/{skill_id}/draft/file?path=... | skills:write | 读暂存文件 |
| PUT | /api/skills/{skill_id}/draft/file | skills:write | 写文本文件到暂存表 |
| POST | /api/skills/{skill_id}/draft/files | skills:write | 上传附件到暂存表 |
| DELETE | /api/skills/{skill_id}/draft/file?path=... | skills:write | 删除文件 |
| POST | /api/skills/{skill_id}/draft/save | skills:write | 校验并提交到 Git，生成版本 |
| POST | /api/skills/{skill_id}/submit | skills:write | 提交审核 |
| POST | /api/skills/{skill_id}/withdraw | skills:write | 撤回 pending 提交 |
| DELETE | /api/skills/{skill_id} | skills:write | 删除私有草稿；pending 时 409，需先撤回；公共版本不受影响 |
| GET | /api/skills/{skill_id}/versions | 所有者或 admin | 版本历史 |
| PUT | /api/skills/{skill_id}/preference | skills:read | 个人开关（JSON，按 skill_id） |

### 9.2 管理员审核

| Method | Path | 权限 | 说明 |
|---|---|---|---|
| GET | /api/admin/skills/reviews | admin | 审核队列，按提交时间 |
| GET | /api/admin/skills/reviews/{submission_id} | admin | 详情、diff、文件列表 |
| POST | /api/admin/skills/reviews/{submission_id}/approve | admin | 批准并创建公共 release |
| POST | /api/admin/skills/reviews/{submission_id}/reject | admin | 驳回，写理由 |
| POST | /api/admin/skills/{skill_id}/unpublish | admin | 下架公共技能 |
| POST | /api/admin/skills/{skill_id}/activate | admin | 回滚到历史公共 release |
| PATCH | /api/skills/{skill_id} | admin | 全局启停 |
| POST | /api/skills/reload | admin | 刷新本进程快照 |

### 9.3 响应形状

技能列表项至少包含：

- skill_id、name、scope、description；
- skill_version、version_id、git_commit、skill_path；
- owner_user_id（只对本人/admin 返回）、enabled、global_enabled（admin）；
- review_status（本人/admin）；
- container_file（私有技能只对所有者返回）。

审核详情包含版本、commit、文件树、diff、frontmatter、allowed-tools、required-secrets、
作者、提交时间。

## 10. 前端工作台

### 10.1 我的技能

- 列表：私有/公共、审核中/已驳回/已通过、最后版本、更新时间；
- 新建：名字、描述、初始 SKILL.md；
- 编辑器：Markdown 正文、文件树、文本文件编辑、附件上传/删除、预览；
- 操作：保存（生成版本）、提交审核、撤回、删除私有草稿；
- 状态：未保存、保存中、索引中、可检索、审核中、已驳回、已通过。

### 10.2 管理审核台

- 待审核列表：作者、技能、版本、提交时间；
- 详情：diff 当前公共版本、文件树、脚本内容、frontmatter、allowed-tools、
required-secrets、包大小；
- 批准：检查 public_name 冲突，创建公共 commit；
- 驳回：必填理由；
- 公共技能管理：下架、回滚、全局开关。

### 10.3 可见技能页

现有技能设置页改为显示公共技能 + 本人私有技能；私有项标记“私有”，公共项按现有
全局/个人开关显示。

## 11. 配置

~~~yaml
skills:
  creation:
    enabled: true
    max_drafts_per_user: 20
    max_pending_submissions_per_user: 5
    max_package_bytes: 5242880
    max_file_bytes: 1048576
    max_files: 200

  git:
    repository: "git@github.com:org/myflow-skills.git"
    public_branch: main
    draft_ref_prefix: "refs/heads/myflow/drafts"
    cache_dir: ".myflow/skills"
    ssh_key_path: "$SKILLS_GIT_SSH_KEY_PATH"
    known_hosts_path: "$SKILLS_GIT_KNOWN_HOSTS"
    command_timeout_seconds: 120

  review:
    require_admin: true
    allow_self_approval: true

  index:
    outbox_poll_seconds: 2          # worker 轮询间隔
    catalog_poll_seconds: 5         # 跨 worker epoch 轮询；0 = 单 worker
    reconciliation_interval_minutes: 10   # 对账 + 清理周期

  search:
    enabled: true
    candidate_k: 20                 # 每路召回交给融合的候选数，也是精排上限
    top_k: 3
    rrf_k: 60                       # 向量名次与 BM25 名次的融合常数
    min_score: null                 # 作用在融合分数上；null = 不过滤
    max_body_chars: 6000            # 装载进上下文的单个正文预算
    max_context_chars: 12000
    keyword:                        # 稀疏通道：读正文，本地 BM25
      enabled: true
      bm25_k1: 1.2
      bm25_b: 0.75
      keyword_weight: 2.0
      name_weight: 3.0
    embedding:                      # 稠密通道：只嵌 name + description（§7.2）
      enabled: true
      base_url: "$EMBEDDING_BASE_URL"
      path: /embeddings
      api_key: "$EMBEDDING_API_KEY"
      model: Qwen/Qwen3-Embedding-8B
      dimensions: 4096
    vector_store:
      provider: milvus
      uri: "$MILVUS_URI"
      token: "$MILVUS_TOKEN"
      collection: myflow_skills_active
      metric_type: COSINE
      index_type: AUTOINDEX
      consistency_level: Strong
    rerank:                         # 终排：读正文的 cross-encoder
      enabled: false                # 打开时 base_url 与 model 必填
      protocol: cohere              # cohere | tei
      base_url: "$RERANK_BASE_URL"
      path: /rerank
      api_key: "$RERANK_API_KEY"
      model: Qwen/Qwen3-Reranker-8B
      max_document_chars: 4000      # 送进精排的单个正文上限
      timeout_seconds: 30
      max_retries: 1

  retention:
    keep_versions_per_skill: 20
    cache_ttl_days: 30
~~~

- search.enabled=true 时 embedding.base_url 和 vector_store.uri 必填；关掉向量后
  检索仍有 BM25 通道（只依赖 PG + Git 缓存，不需要任何外部服务）；
- keyword.enabled=false 且 embedding.enabled=false 时 search 退化为“没有任何召回通道”，
  启动时直接报配置错误；
- rerank.enabled=true 时 base_url 与 model 必填；精排失败只回退融合顺序；
- git.write 需要服务端可写部署密钥；普通用户永远拿不到该密钥；
- 创建/审核默认开启；关闭 creation 后只有 admin 能创建；
- 代码中没有任何默认服务地址。

## 12. 安全、审计与配额

### 12.1 私有隔离

- PostgreSQL：所有作者接口按 owner_user_id 过滤；admin 只能通过审核接口读取待审内容；
- Git：草稿分支只由服务端访问；管理员/运维通过审核页读取；
- Milvus：私有 active 行带有 owner_user_id，检索只查当前用户 owner 的 scope=private 行；
  命中后再次按 owner_user_id 确认；其他用户无法构造请求拿到私有版本；
- Sandbox：私有目录按用户挂载；不支持按用户挂载时不暴露 container_file；
- 暂存表只允许 owner 读写；审核只读已提交版本；日志、错误、审计不记录私有正文和附件内容。

### 12.2 不可信内容

- 服务端绝不执行用户脚本；只在审核页展示；
- 只允许常规文件，拒绝符号链接、硬链接、设备文件、路径逃逸；
- 限制包大小、文件数、单文件大小、文件名长度和扩展名；
- 审核是公共技能的信任边界；审核页必须看到脚本和附件，不能只看 SKILL.md；
- 公共技能正文仍然作为不可信数据注入，沿用当前的提示注入防护。

### 12.3 审计和配额

- 审计：创建、保存、提交、撤回、批准、驳回、下架、回滚、全局开关、删除；
- 草稿保存的完整内容历史由 Git commit 记录；
- 配额：每用户草稿数、待审数、总字节、文件数；超限返回 429/422；
- 失败信息脱敏，不泄漏其他用户内容。

## 13. 失败恢复与保留

- 保存时 Git commit 成功、PG 写入失败：留下未引用的草稿 commit，不激活；重试可复用；
- Git 失败：不创建版本，暂存表保留未提交内容；
- active projection 写入失败：版本仍可被作者通过 slash/read 使用，但不能被检索；
  index_status=failed/outbox 重试，管理端可见；提交/批准不被阻断，公共技能通过
  prompt/slash 可用，检索在 outbox/对账恢复后自动加入；
- search.enabled=false：不要求 active projection，提交/批准仍可用；技能靠
  prompt/slash/read；
- 复用旧版本但 active 行已被清理/替换：重新激活时重新 embedding 并 upsert，不依赖
  旧向量；
- 嵌入文本/模型/维度变化：视为数据迁移，必须调 POST /api/admin/skills/reindex 触发
  全量重建（§7.6）；对账在稳态会跳过健康行，因此不会自愈；
- 批准时 Git commit 成功、PG 失败：公共 commit 成为未引用对象，不改变 public_active；
管理员可重试，重试检测同一 source_commit 并复用；
- PG 不可用：所有技能操作 fail-closed；
- Milvus 不可用：检索降级到 BM25，公共/私有可见性仍由 PG 决定；
- 多 worker：保存按 skill advisory lock；批准按公共分支锁；Git 保存前 fetch 远端分支、
保存后 push；没有跨请求的本地工作区；启动时只清理自己能确认失联的暂存任务/批次；
- 保留：skills、versions、submissions、releases、audit 永久保留；公共 main 历史永久；
  Milvus 只保留 active projection 行，不保留历史版本向量；commit 内容缓存保留 active、
  待审和最近 N 个版本（默认 20）；旧版本回滚时从 Git 重新 embedding；
- 草稿分支历史由 Git 保留；用户删除草稿时删除暂存表和私有指针，不重写 Git 历史；
skills.deleted_at 只隐藏私有草稿，公共 release 和 public_active 不受影响。

## 14. 测试计划

### 14.1 单元

- 版本生成：相同内容复用版本；内容/附件变化升版；路径排序稳定；
- 包校验：重名、坏 frontmatter、路径逃逸、符号链接、超大文件、空包；
- 审核状态机：pending 不可编辑、撤回、驳回、重新提交、批准；
- 私有/公共指针分离：批准后编辑不改变公共版本；
- 可见集合：公共 + 自己的私有；私有优先；他人私有不可见；
- 检索确认：伪造/过期/他人私有 version_id 被拒绝；
- 一次 Milvus OR 检索：公共分支排除本人私有 skill_id，私有分支按 owner 过滤；
  同一 skill_id 不会返回公共/私有两条；作者侧无条件私有优先；
- active projection 的 scope/owner/enabled 过滤器不超过简单表达式；
- 公共与私有版本 embedding 高相似时仍只返回一个技能；
- embedding 文本只由 name + description 组成，正文变化不触发重新 embedding；
- 融合：两路名次按 rrf_k 合成，min_score 作用在融合分数上；私有去重发生在融合与
  精排之前，精排无法把公共副本排到作者私有副本之前；
- 精排：文档按 max_document_chars 截断；精排失败回退融合顺序并带 warning；
- 降级矩阵：向量不可用 → 纯 BM25；BM25 不可用 → 纯向量；两者都不可用 → 空结果 + 原因；
- 重建：reconcile(force=True) 会重写健康行；不 force 时健康行不重新 embedding。

### 14.2 集成

- 临时 Git 仓库：创建草稿、写暂存表、保存 commit、附件、提交、批准复制到 main；
- 多 worker 保存：不同实例都能读取暂存表，保存时 fetch/push 不丢提交；
- 批准 commit 与 source_commit 的关系；作者删除草稿后公共版本仍可读；
- 发布 v1、作者编辑 v2：公共 v1 不变，作者搜索优先 v2；批准 v2 后公共切换；
- 管理员驳回后作者仍可用私有版本；
- 管理员下架/回滚后可见性变化；
- active projection 重建、outbox 重试/对账、缓存删除、索引失败恢复；
- 换投影文本版本后：重建前检索可能命中旧向量、重建后与 PG 一致；管理员 reindex 接口
  返回修复/删除计数并记审计；
- 精排按正文提高“正文里出现报错原文”的命中名次（用固定分数替身断言，不依赖真模型）；
- 多用户隔离：A 无法通过 API/检索/文件路径读到 B 的私有包；
- 权限：普通用户不能审核/下架/全局开关；admin 可以；
- 版本复用但 active projection 被清理/索引失败时，提交/批准仍可用，outbox 重建后
  检索恢复；
- search 关闭时作者/管理员流程仍可用；
- 下架/回滚对下一次 run 生效，当前 run 不被假装撤回；
- public_name 首次批准后不可变，作者改 private_name 不影响公共路径。

### 14.3 安全

- Git 命令注入、分支名注入、文件路径注入；
- 符号链接、硬链接、设备文件、tar/zip 炸弹；
- 附件类型/大小/数量限制；
- 私有内容不出现在其他用户的 prompt、检索结果、sandbox；
- 审核页只对 admin 开放；
- 凭据不出现在日志、API、数据库。

## 15. 实施顺序

1. **数据模型**：skills / versions / submissions / releases / active / settings / audit，

   Alembic 迁移，内部 UUID 与名字约束；
2. **Git 服务端写入**：bare mirror、临时 worktree、fetch/push、保存 commit、附件校验、

   暂存表与分支锁；
3. **作者 API/UI**：创建、文件树、编辑、保存、提交、撤回、版本历史；
4. **审核 API/UI**：队列、diff、批准/驳回、公共 main commit、审计；
5. **向量投影**：active projection collection、outbox worker、私有/公共 active 行、
   回滚重嵌入、对账/reindex；
6. **检索**：PG 可见集合 → Milvus → PG 批量确认 → Git 正文；
7. **运行时**：按用户快照、提示词索引、slash、私有挂载、公共挂载；
8. **迁移与测试**：旧 JSON 开关、bootstrap 导入、并发/安全/端到端测试。

## 16. Builder calls

1. skill_id 使用内部 UUID；private_name 按 owner 唯一；public_name 全局唯一。
2. 首次批准时 public_name 取 private_name；冲突则批准 409，要求作者改名后重新提交。
3. 一个用户一个草稿分支，技能路径用 skill_id；每次保存生成一个 commit/version。
4. 编辑先写 PostgreSQL 的 skill_draft_files 暂存表；点击保存才 commit 到 Git；

   未保存内容不是技能版本，也不参与检索/审核。
5. 审核针对不可变 version + draft_commit；pending 期间禁止编辑，必须先撤回。
6. 批准复制完整技能目录到 main，公共 release 的 git_commit 是 main 批准 commit；

   source_commit 保留草稿 commit。
7. 私有版本也进入 active projection；可见性由 scope/owner 过滤 + PostgreSQL 确认控制。
8. active projection 每个公共/私有技能只保留一行，不保留历史版本向量；回滚时重新
   embedding；同一版本可同时有 public/private 两行，按 skill_id 去重。
9. 公共和私有同名时作者侧私有优先；公共技能仍可被工具检索。
10. 管理员可以审核自己的提交（allow_self_approval=true），审计记录 self_approved。
11. 管理员下架/回滚只改公共指针，不删除 release 或 Git 历史。
12. V1 单仓库；草稿分支和 main 在同一仓库；普通用户没有 Git 凭据。
13. 私有 sandbox 路径按用户挂载；provider 不支持时不暴露容器的私有路径。
14. 旧公共技能通过显式 bootstrap 导入为 admin 所有、已批准；不自动扫描旧目录。
15. 保留策略：元数据/公共历史永久；Milvus 只保留 active 行；内容缓存保留 active、
    待审和最近 20 个版本。
16. 个人偏好按 skill_id 存 JSON；全局开关按公共 skill_id 存 PostgreSQL。
17. Milvus 默认 Strong consistency；PG 确认使用一条批量 JOIN，不做 N+1。
18. 暂存表用 PG 而不是本地工作区，保证多 worker 下未保存编辑不丢；保存前 fetch、

    保存后 push 远端草稿分支。
19. 管理员审核只读已提交的 version/submitted_commit，不读 skill_draft_files 暂存表。
20. private_name 不得与其他技能已占用的 public_name 冲突（同一 skill 除外）。
21. 提交记录保存 submitted_commit（提交时草稿 HEAD）；审核/批准用它，而不是版本最早的
    draft_commit；重新激活版本时重新 embedding 并 upsert active projection。
22. 向量索引不是提交/批准的硬门槛；search 关闭或索引失败时，技能仍靠 prompt/slash/read

    可用，检索恢复后自动加入。
23. 生产只认 Git 目录，不合并本地 skills/ 扫描；source=filesystem 仅用于测试/本地调试。
24. public_name 首次批准后固定；V1 不提供公共改名，改名需下架后作为新技能重新发布。
25. 下架/回滚对下一次 run/检索生效；当前 run 已注入正文无法撤回，不承诺强撤稿。
26. 检索只发一次 Milvus search：公共分支排除本人私有 skill_id，私有分支按 owner 过滤；
    作者侧无条件私有优先，不考虑分数高低，也不提供 scope 切换参数；不拼大 version_id IN。
27. outbox 与业务状态同事务写入，worker 幂等更新 Milvus；对账任务修复缺失/孤儿行。

## 17. 备选方案与取舍

| 方案 | 放弃原因 |
|---|---|
| 每个用户一个独立 Git 仓库 | 隔离更强，但管理员审核、公共合并、部署凭据和仓库数量都成倍复杂；V1 用单仓库 + 用户分支 |
| 未审核内容只放数据库/对象存储，批准后才进 Git | 草稿版本、附件和 diff 会分裂成两套存储；Git 的提交历史和审核追溯更有价值 |
| 批准时只切换可见性，不复制到 main | 公共技能依赖用户草稿分支，作者删除分支/改历史会破坏公共内容；复制到 main 才能形成不可变公共目录 |
| 管理员整仓发布 tag | 与普通用户逐技能创作/审核冲突；V1 改为按版本的审核批准 |
| 只索引公共技能，私有草稿只用 slash/read | 不满足“未审核草稿也要被作者检索和使用”的要求 |

**结论**：采用“单仓库 + 用户草稿分支 + main 公共目录 + PostgreSQL 审核/激活账本 +
Milvus active projection + outbox”的方案。

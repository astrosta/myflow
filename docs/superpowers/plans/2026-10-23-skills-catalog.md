# Skill Git Catalog, Private Drafts, Review, and Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace filesystem-scanned skills with a Git-backed catalog where users author private skill packages, admins review and publish them, and search uses an active Milvus projection confirmed by PostgreSQL.

**Architecture:** PostgreSQL owns skill identity, versions, submissions, public releases, active pointers, drafts, audit, and an outbox. Git owns immutable draft/public content. Milvus stores one active projection row per public skill and one per private skill. Search sends one OR query to Milvus, confirms hits in PostgreSQL, and applies unconditional private precedence for the author.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy async, Alembic, PostgreSQL/SQLite, system git CLI, Milvus/pymilvus, Next.js 16/React 19/TanStack Query/Vitest.

**Spec:** docs/superpowers/specs/2026-10-23-skill-package-git-release-design.md

## Global Constraints

- Production database is PostgreSQL; SQLite is the test and local-development backend.
- Never execute user-provided scripts on the server. They are stored, shown for review, and mounted read-only only.
- Git commands use argument arrays, never a shell. Disable hooks, prompts, and system config.
- Unapproved private content never enters the public branch, public projection, public prompt, or another user's sandbox.
- The author's private version is unconditionally preferred over their public version; there is no scope switch in V1.
- One Milvus search per retrieval. Public rows exclude the caller's private skill_ids; private rows filter by owner.
- version_id is globally unique. skill_version is per skill and may repeat across skills.
- Every public release row contains skill_id, skill_version, git_repository, git_commit, and skill_path.
- Public name is immutable after first approval; rename requires unpublish plus a new skill in V1.
- Outbox rows are written in the same PostgreSQL transaction as the state change they project.
- Tests run with: uv run python -m pytest backend/tests/<file> -v (from repo root)
- Frontend tests run with: cd frontend && pnpm vitest run <path>

## Review Focus

- A private skill must not appear in another user's API list, prompt, retrieval hit, Git path, or sandbox mount.
- One OR search must not return both public and private rows for the same skill; verify the public branch excludes the caller's private skill_ids.
- An outbox event older than the current active pointer must not overwrite the newer projection.
- Approval must reject a submission whose submitted_commit no longer contains the submitted package hash.
- A Git commit that succeeds before a PostgreSQL failure must leave no active pointer and must be safely retried.
- 精排（Task 21）只能重排已经按 skill_id 去重过的集合：它不得让作者的公共副本排到自己的私有副本前面。
- BM25 通道（Task 22）读正文但不得改变可见性：个人偏好、agent 白名单、全局开关仍由 PostgreSQL 判定。
- 改嵌入文本（Task 23）必须配一次强制重建；对账在稳态会跳过健康行，所以旧向量不会自愈。

---

## File Structure

Create:

- agentkit/skills/domain.py - immutable skill/version/submission/release/projection records
- agentkit/skills/package.py - staged-file validation, package hashing, frontmatter generation
- agentkit/skills/store/model.py - eleven ORM tables
- agentkit/skills/store/sql.py - SQL repository and transactions
- agentkit/skills/git.py - Git CLI writer, draft branch, public branch, archive/materialize
- agentkit/skills/content.py - immutable commit cache and release content reads
- agentkit/skills/author.py - create/edit/save/submit/withdraw/delete service
- agentkit/skills/review.py - approve/reject/unpublish/rollback service
- agentkit/skills/projection.py - Milvus active projection protocol and implementation
- agentkit/skills/outbox.py - projection outbox worker and reconciliation
- agentkit/skills/search.py - one-OR-query retrieval, PG confirmation, private precedence
- agentkit/skills/catalog.py - catalog snapshot loader and process-wide runtime
- backend/app/gateway/models/skills.py - author/review request and response models
- backend/app/gateway/routers/skill_drafts.py - author HTTP surface
- backend/app/gateway/routers/skill_reviews.py - admin HTTP surface
- backend/packages/harness/agentkit/persistence/migrations/versions/0005_skill_catalog.py - schema
- frontend/src/core/api/skills.ts - typed API client
- frontend/src/app/workspace/skills/page.tsx - my skills page
- frontend/src/app/workspace/skills/[skill_id]/page.tsx - draft editor page
- frontend/src/app/workspace/admin/skills/page.tsx - review queue page
- frontend/src/components/skills/*.tsx - author/review components

Modify:

- agentkit/config/skills_config.py - source, creation, git, review, index, retention config
- backend/app/gateway/auth/models.py - add skills:write to PAT_SCOPE_MENU
- agentkit/persistence/schema.py - import the new ORM module
- agentkit/skills/types.py - add skill_id/scope/owner/version/release fields
- agentkit/skills/storage.py - keep filesystem scanner for tests/migration; use snapshot at runtime
- agentkit/skills/prompt.py - render public/private roots
- agentkit/skills/prefs.py and agentkit/config/skills_prefs.py - key prefs by skill_id
- agentkit/tools/builtin/search_and_load_skills.py - call the new search service
- agentkit/agents/middlewares/assembly.py, skill_activation_middleware.py, skill_tool_policy_middleware.py - snapshot-aware registry
- agentkit/sandbox/local/local_sandbox_provider.py - per-user private mount
- backend/app/gateway/app.py - initialize and close SkillRuntime
- backend/app/gateway/routers/skills.py and skills_catalog.py - new list/preference/global behavior
- backend/app/gateway/models/request.py and response.py - compatibility fields
- config.example.yaml, config.yaml - new skills sections
- frontend/src/components/workspace/sidebar.tsx - skills navigation
- frontend/src/app/workspace/layout.tsx and sidebar-container.tsx - active nav state

---

### Task 1: Config, Auth Scope, Schema, and Migration

**Files:**
- Modify: backend/packages/harness/agentkit/config/skills_config.py
- Modify: backend/app/gateway/auth/models.py
- Modify: backend/packages/harness/pyproject.toml (add pymilvus to the skills extra)
- Modify: backend/pyproject.toml (add a skills extra forwarding to agentkit[skills])
- Create: backend/packages/harness/agentkit/skills/store/__init__.py
- Create: backend/packages/harness/agentkit/skills/store/model.py
- Create: backend/packages/harness/agentkit/persistence/migrations/versions/0005_skill_catalog.py
- Modify: backend/packages/harness/agentkit/persistence/schema.py
- Test: backend/tests/test_skill_catalog_schema.py

**Interfaces:**
- SkillsConfig gains source: Literal["filesystem", "git"] = "filesystem".
- SkillsConfig gains SkillCreationConfig, SkillGitConfig, SkillReviewConfig, SkillIndexConfig, SkillRetentionConfig with fields and defaults from the spec.
- PAT_SCOPE_MENU contains "skills:write".
- ORM table classes: SkillRow, SkillVersionRow, SkillSubmissionRow, SkillReleaseBatchRow, SkillReleaseRow, SkillPublicActiveRow, SkillPrivateActiveRow, SkillSettingsRow, SkillAuditEventRow, SkillDraftFileRow, SkillIndexOutboxRow.
- Tables: skills, skill_versions, skill_submissions, skill_release_batches, skill_releases, skill_public_active, skill_private_active, skill_settings, skill_audit_events, skill_draft_files, skill_index_outbox.
- Alembic revision 0005_skill_catalog has down_revision = "0004_knowledge_base".

- [ ] **Step 1: Write the failing schema/config test**

~~~python
# backend/tests/test_skill_catalog_schema.py
from agentkit.persistence.schema import Base
from backend.app.gateway.auth.models import PAT_SCOPE_MENU

EXPECTED = {
    "skills", "skill_versions", "skill_submissions", "skill_release_batches",
    "skill_releases", "skill_public_active", "skill_private_active",
    "skill_settings", "skill_audit_events", "skill_draft_files", "skill_index_outbox",
}

def test_skill_tables_and_scope_are_registered():
    assert EXPECTED <= set(Base.metadata.tables)
    assert "skills:write" in PAT_SCOPE_MENU
~~~

Add a second test that inserts two pending submissions for one skill and expects IntegrityError, and a third that inserts two skills with the same (owner_user_id, private_name) while deleted_at is null and expects IntegrityError.

- [ ] **Step 2: Run test to verify it fails**

Run: uv run python -m pytest backend/tests/test_skill_catalog_schema.py -v

Expected: FAIL because the tables and scope are absent.

- [ ] **Step 3: Implement config, auth scope, ORM models, migration, and schema import**

Use uuid4().hex for API ids. Use DateTime(timezone=True) and UTC-aware defaults. Add partial unique indexes with both sqlite_where and postgresql_where for:
- skills (owner_user_id, private_name) where deleted_at IS NULL
- skills (public_name) where public_name IS NOT NULL
- skill_submissions (skill_id) where status = 'pending'

Add unique constraints:
- skill_versions (skill_id, skill_version), (skill_id, package_sha256)
- skill_releases (batch_id, skill_id)
- skill_draft_files (skill_id, path) primary key
- skill_index_outbox indexes on (status, next_attempt_at)

Make 0005 idempotent with sa.inspect(op.get_bind()).has_table(), matching 0004.

- [ ] **Step 4: Run test to verify it passes**

Run: uv run python -m pytest backend/tests/test_skill_catalog_schema.py backend/tests/test_bootstrap_schema.py -v

Expected: all PASS.

- [ ] **Step 5: Commit**

~~~bash
git add backend/packages/harness/agentkit/config/skills_config.py backend/app/gateway/auth/models.py backend/packages/harness/agentkit/skills/store backend/packages/harness/agentkit/persistence/migrations/versions/0005_skill_catalog.py backend/packages/harness/agentkit/persistence/schema.py backend/tests/test_skill_catalog_schema.py
git commit -m "feat(skills): add catalog schema and configuration"
~~~

### Task 2: Domain Records, Path Validation, and Package Hashing

**Files:**
- Create: backend/packages/harness/agentkit/skills/domain.py
- Create: backend/packages/harness/agentkit/skills/package.py
- Modify: backend/packages/harness/agentkit/skills/types.py
- Test: backend/tests/test_skill_package.py

**Interfaces:**
- class SkillScope(StrEnum): PUBLIC = "public"; PRIVATE = "private"
- dataclass StagedFile: path: str; content: bytes; mode: str = "text"
- dataclass StagedFileInfo: path: str; size_bytes: int; mode: str; sha256: str
- class InvalidSkillPackage(ValueError): code: str = "invalid_skill_package"
- class SkillConflict(RuntimeError): code: str = "skill_conflict"
- dataclass PackageManifest: files: tuple[StagedFile, ...]; package_sha256: str; skill_md_sha256: str; parsed: ParsedSkill
- dataclass SkillRecord: id; owner_user_id; private_name; public_name; created_at; updated_at; deleted_at
- dataclass SkillVersionRecord: id; skill_id; skill_version; version_seq; package_sha256; skill_md_sha256; metadata; draft_ref; draft_commit; skill_path; index_status; created_at
- dataclass SkillSubmissionRecord: id; skill_id; version_id; submitted_commit; owner_user_id; status; submitted_at; reviewed_by; reviewed_at; reject_reason; public_release_id
- dataclass SkillReleaseRecord: id; batch_id; version_id; skill_id; skill_version; git_repository; git_commit; skill_path; public_name; source_commit; approved_by; created_at
- dataclass PublicActiveRecord: skill_id; release_id; activated_by; updated_at
- dataclass PrivateActiveRecord: skill_id; owner_user_id; version_id; updated_at
- dataclass VisibleSkill: skill: Skill; scope: SkillScope; version: SkillVersionRecord; release: SkillReleaseRecord | None
- dataclass ReviewItem: submission_id; skill_id; name; version; owner_user_id; submitted_at; submitted_commit
- dataclass ReviewDetail: item: ReviewItem; files: list[StagedFileInfo]; skill_md: str; diff: str; metadata: dict; package_bytes: int
- def validate_skill_path(path: str) -> str
- def build_package(files: Mapping[str, StagedFile], *, max_files: int, max_file_bytes: int, max_package_bytes: int) -> PackageManifest
- def render_skill_md(name: str, description: str) -> str
- Reuse the existing Embedder protocol from agentkit.skills.router; do not create a second embedding interface.
- Skill gains optional fields at the end: skill_id, scope, owner_user_id, version_id, skill_version, release_id, git_repository, git_commit, repo_path. Existing filesystem tests keep working when these are unset.
- Skill.container_dir uses scope when scope is set; otherwise keeps the legacy category path.

- [ ] **Step 1: Write failing tests**

~~~python
def test_package_hash_is_stable_and_ignores_mapping_order():
    a = {"SKILL.md": StagedFile("SKILL.md", b"same", "text")}
    b = {"SKILL.md": StagedFile("SKILL.md", b"same", "text")}
    assert build_package(a, ...).package_sha256 == build_package(b, ...).package_sha256

def test_package_rejects_parent_traversal_and_absolute_paths():
    for bad in ("../x", "/etc/passwd", "a/../../b"):
        with pytest.raises(InvalidSkillPackage):
            validate_skill_path(bad)

def test_package_requires_skill_md_and_valid_frontmatter():
    with pytest.raises(InvalidSkillPackage):
        build_package({}, ...)

def test_private_skill_container_path_uses_private_root():
    skill = make_skill(scope=SkillScope.PRIVATE, name="pdf-tools")
    assert skill.container_file == "/mnt/skills/private/pdf-tools/SKILL.md"
~~~

- [ ] **Step 2: Run tests to verify they fail**

Run: uv run python -m pytest backend/tests/test_skill_package.py -v

- [ ] **Step 3: Implement domain types, validation, hash, and frontmatter generation**

Canonical hash input is sorted by POSIX relative path: path + NUL + mode + NUL + content + NUL. Reject empty segments, ".", "..", backslash, absolute paths, NUL, duplicate normalized paths, files over limits, and a package without SKILL.md. Parse SKILL.md with the existing parse_skill_text. Render initial frontmatter with yaml.safe_dump and name/description only.

- [ ] **Step 4: Run tests and existing skill parser tests**

Run: uv run python -m pytest backend/tests/test_skill_package.py backend/tests/test_skill_parser.py backend/tests/test_skill_loading.py -v

- [ ] **Step 5: Commit**

~~~bash
git add backend/packages/harness/agentkit/skills/domain.py backend/packages/harness/agentkit/skills/package.py backend/packages/harness/agentkit/skills/types.py backend/tests/test_skill_package.py
git commit -m "feat(skills): add package domain and hashing"
~~~

### Task 3: SQL Store for Identity, Versions, Submissions, Releases, and Active Pointers

**Files:**
- Create: backend/packages/harness/agentkit/skills/store/sql.py
- Test: backend/tests/test_skill_store.py

**Interfaces:**
- class SqlSkillStore:
  - __init__(session_factory)
  - async create_skill(*, owner_user_id: str, private_name: str) -> SkillRecord
  - async get_skill(skill_id: str) -> SkillRecord | None
  - async list_owned(owner_user_id: str) -> list[SkillRecord]
  - async find_version_by_hash(skill_id: str, package_sha256: str) -> SkillVersionRecord | None
  - async next_version_seq(skill_id: str) -> int
  - async record_saved_version(*, skill_id: str, owner_user_id: str, manifest: PackageManifest, draft_ref: str, draft_commit: str, skill_path: str) -> SkillVersionRecord
  - async create_submission(*, skill_id: str, version: SkillVersionRecord, owner_user_id: str, submitted_commit: str) -> SkillSubmissionRecord
  - async pending_submission(skill_id: str) -> SkillSubmissionRecord | None
  - async submission(submission_id: str) -> SkillSubmissionRecord | None
  - async set_submission_status(submission_id: str, *, status: str, reviewed_by: str | None = None, reject_reason: str | None = None, public_release_id: str | None = None) -> SkillSubmissionRecord
  - async record_public_release(*, submission: SkillSubmissionRecord, public_name: str, git_repository: str, git_commit: str, skill_path: str, source_commit: str, actor: str) -> SkillReleaseRecord
  - async list_pending_submissions(limit: int = 50) -> list[ReviewItem]
  - async review_detail(submission_id: str) -> ReviewDetail | None
  - async list_versions(skill_id: str) -> list[SkillVersionRecord]
  - async set_public_active(skill_id: str, release_id: str, actor: str) -> None
  - async clear_public_active(skill_id: str) -> None
  - async set_global_enabled(skill_id: str, enabled: bool, actor: str) -> None
  - async visible_for_user(user_id: str) -> list[VisibleSkill] (public + own private; includes version/release)
  - async private_skill_ids(user_id: str) -> tuple[str, ...]
  - async confirm_hits(user_id: str, hits: Sequence[ProjectionHit]) -> list[VisibleSkill]
  - async audit(*, actor: str, action: str, skill_id: str | None = None, submission_id: str | None = None, release_id: str | None = None, detail: dict | None = None) -> None
- All methods translating IntegrityError to SkillConflict with a stable code field.

- [ ] **Step 1: Write failing tests**

Use a SQLite async engine, Base.metadata.create_all, and a session factory. Assert:
- create_skill lowercases nothing but rejects blank private_name; duplicate owner/name raises SkillConflict.
- record_saved_version reuses an existing version with the same package hash and allocates v1, v2 otherwise.
- a second pending submission for the same skill raises SkillConflict.
- visible_for_user returns public plus only the caller's private rows.
- confirm_hits rejects a private hit whose owner is another user.

- [ ] **Step 2: Run tests to verify they fail**

Run: uv run python -m pytest backend/tests/test_skill_store.py -v

- [ ] **Step 3: Implement the store in focused methods**

Every method opens one session. record_saved_version, create_submission, and record_public_release perform their state change, active-pointer change, audit row, and outbox insert in one transaction. Do not put outbox writes in a separate transaction. Use selectinload or explicit joins to avoid N+1 in visible_for_user.

- [ ] **Step 4: Run tests**

Run: uv run python -m pytest backend/tests/test_skill_store.py -v

- [ ] **Step 5: Commit**

~~~bash
git add backend/packages/harness/agentkit/skills/store/sql.py backend/tests/test_skill_store.py
git commit -m "feat(skills): add SQL catalog store"
~~~

### Task 4: Draft Files and Outbox Store

**Files:**
- Modify: backend/packages/harness/agentkit/skills/store/sql.py
- Test: backend/tests/test_skill_drafts_store.py

**Interfaces:**
- async put_draft_file(skill_id: str, path: str, content: bytes, mode: str = "text") -> StagedFileInfo
- async list_draft_files(skill_id: str) -> list[StagedFile]
- async delete_draft_file(skill_id: str, path: str) -> bool
- async delete_all_draft_files(skill_id: str) -> None
- async enqueue_outbox(*, op: str, projection_key: str, scope: str, owner_user_id: str | None, skill_id: str, version_id: str | None, payload: dict) -> None
- async claim_outbox(limit: int = 20) -> list[OutboxEvent]
- async finish_outbox(event_id: str) -> None
- async retry_outbox(event_id: str, error: str, delay_seconds: int) -> None
- async pending_outbox_count() -> int
- dataclass OutboxEvent: id: str; op: str; projection_key: str; scope: str; owner_user_id: str | None; skill_id: str; version_id: str | None; payload: dict; attempts: int

- [ ] **Step 1: Write failing tests**

Assert draft file upsert replaces bytes and updates sha256/size, deleting the last file is allowed, and claim_outbox never returns the same event to two sequential callers. Assert retry_outbox sets next_attempt_at in the future and status back to pending.

- [ ] **Step 2: Run tests to verify they fail**

Run: uv run python -m pytest backend/tests/test_skill_drafts_store.py -v

- [ ] **Step 3: Implement using one transaction per public method**

Use SELECT ... FOR UPDATE SKIP LOCKED on PostgreSQL for claim_outbox; on SQLite, select pending rows ordered by created_at and update them to running in the same transaction. Cap payload_json to 64 KiB.

- [ ] **Step 4: Run tests**

Run: uv run python -m pytest backend/tests/test_skill_drafts_store.py backend/tests/test_skill_store.py -v

- [ ] **Step 5: Commit**

~~~bash
git add backend/packages/harness/agentkit/skills/store/sql.py backend/tests/test_skill_drafts_store.py
git commit -m "feat(skills): add draft staging and outbox storage"
~~~

### Task 5: Git Writer and Immutable Content Cache

**Files:**
- Create: backend/packages/harness/agentkit/skills/git.py
- Create: backend/packages/harness/agentkit/skills/content.py
- Test: backend/tests/test_skill_git.py
- Test: backend/tests/test_skill_content_store.py

**Interfaces:**
- dataclass GitCommit: commit: str; ref: str; path: str
- class GitCommandError(RuntimeError): command: tuple[str, ...]; returncode: int; stderr: str
- class SkillGitRepository:
  - __init__(config: SkillGitConfig, cache_dir: Path)
  - async ensure_ready() -> None
  - async save_draft(*, owner_user_id: str, skill_id: str, files: Sequence[StagedFile], message: str) -> GitCommit
  - async head(ref: str) -> str
  - async list_tree(commit: str, path: str) -> list[str]
  - async publish_public(*, public_name: str, source_commit: str, source_path: str, message: str) -> GitCommit
  - async read_file(commit: str, path: str) -> bytes
  - async materialize(commit: str, destination: Path) -> None
- class SkillContentStore:
  - __init__(cache_dir: Path, git: SkillGitRepository)
  - async ensure(commit: str) -> Path
  - async read_text(commit: str, path: str) -> str
  - async read_bytes(commit: str, path: str) -> bytes
  - async materialize_public(active: Sequence[VisibleSkill]) -> Path
  - async materialize_private(owner_user_id: str, active: Sequence[VisibleSkill]) -> Path
  - async prune(keep_commits: set[str]) -> int

**Implementation constraints:**
- Commands are argument lists with shell=False.
- Environment includes GIT_TERMINAL_PROMPT=0, GIT_CONFIG_NOSYSTEM=1, GIT_HOOKS_PATH set to an empty directory, and identity variables for the service account.
- save_draft fetches the draft ref from origin, creates a temporary worktree, writes exactly the skill directory, commits, and pushes HEAD to refs/heads/myflow/drafts/<owner_user_id>.
- publish_public fetches the public branch, writes public/<public_name>/ from source_commit:source_path, commits, and pushes.
- materialize runs git archive and extracts with a strict tar filter: no links, devices, absolute paths, or parent escapes; enforce max files and bytes.
- read_file uses git show and returns bytes; it never touches a working tree.

- [ ] **Step 1: Write failing Git tests**

~~~python
@pytest.mark.asyncio
async def test_save_draft_pushes_private_tree(git_fixture):
    commit = await git_fixture.repo.save_draft(
        owner_user_id="u1", skill_id="s1",
        files=[StagedFile("SKILL.md", b"---\nname: foo\ndescription: x\n---\nbody\n"), StagedFile("scripts/run.sh", b"echo ok\n")],
        message="draft",
    )
    assert commit.ref == "refs/heads/myflow/drafts/u1"
    assert (await git_fixture.repo.read_file(commit.commit, "users/u1/s1/scripts/run.sh")) == b"echo ok\n"

@pytest.mark.asyncio
async def test_publish_public_copies_the_exact_tree_and_survives_draft_deletion(git_fixture):
    public = await git_fixture.repo.publish_public(
        public_name="foo", source_commit=git_fixture.draft.commit,
        source_path="users/u1/s1", message="approve",
    )
    assert (await git_fixture.repo.read_file(public.commit, "public/foo/SKILL.md")).startswith(b"---")
    await git_fixture.delete_draft_ref()
    assert await git_fixture.repo.read_file(public.commit, "public/foo/SKILL.md")
~~~

Add tests for parent traversal, symlink archive entry rejection, refusing a command argument containing a newline in a ref segment, and content.prune removing only commit cache directories not present in the keep set.

- [ ] **Step 2: Run tests to verify they fail**

Run: uv run python -m pytest backend/tests/test_skill_git.py backend/tests/test_skill_content_store.py -v

- [ ] **Step 3: Implement Git wrapper and content cache**

Use asyncio.to_thread for subprocess calls. Keep a bare mirror at cache_dir/repos/<repo-id>.git. Derive repo-id from sha256(repository). Use tempfile.TemporaryDirectory for worktrees and archive extraction. Do not use shell=True anywhere.

- [ ] **Step 4: Run tests**

Run: uv run python -m pytest backend/tests/test_skill_git.py backend/tests/test_skill_content_store.py -v

- [ ] **Step 5: Commit**

~~~bash
git add backend/packages/harness/agentkit/skills/git.py backend/packages/harness/agentkit/skills/content.py backend/tests/test_skill_git.py backend/tests/test_skill_content_store.py
git commit -m "feat(skills): add git writer and content cache"
~~~

### Task 6: Author Service for Drafts, Save, Submit, and Withdraw

**Files:**
- Create: backend/packages/harness/agentkit/skills/author.py
- Test: backend/tests/test_skill_author_service.py

**Interfaces:**
- class SkillAuthorService:
  - __init__(store: SqlSkillStore, git: SkillGitRepository, content: SkillContentStore, creation: SkillCreationConfig)
  - async create_draft(*, owner_user_id: str, name: str, description: str) -> SkillRecord
  - async open_draft(*, owner_user_id: str, skill_id: str) -> list[StagedFileInfo]
  - async list_versions(*, owner_user_id: str, skill_id: str) -> list[SkillVersionRecord]
  - async put_file(*, owner_user_id: str, skill_id: str, path: str, content: bytes, mode: str = "text") -> StagedFileInfo
  - async delete_file(*, owner_user_id: str, skill_id: str, path: str) -> None
  - async save(*, owner_user_id: str, skill_id: str) -> SkillVersionRecord
  - async submit(*, owner_user_id: str, skill_id: str) -> SkillSubmissionRecord
  - async withdraw(*, owner_user_id: str, skill_id: str) -> SkillSubmissionRecord
  - async delete_draft(*, owner_user_id: str, skill_id: str) -> None
  - async list_files(*, owner_user_id: str, skill_id: str) -> list[StagedFileInfo]

**Rules:**
- Only the owner can read/write the draft.
- create_draft writes an initial SKILL.md to skill_draft_files; it does not commit. Reject a private_name that equals another skill's public_name (same skill is allowed). Enforce max_drafts_per_user.
- submit enforces max_pending_submissions_per_user. save/put_file enforce max_package_bytes, max_file_bytes, and max_files through build_package.
- open_draft returns existing staging files. If staging is empty and the skill has a public active release owned by the caller, copy every file from the public release commit/path into skill_draft_files; this is how editing an approved skill starts a new private draft.
- put_file/delete_file/save are rejected with SkillConflict if a pending submission exists.
- save materializes all staged files, validates limits and package boundaries, commits to Git, then calls store.record_saved_version. If Git fails, the staging table remains unchanged and no version is written.
- submit requires no pending submission; it resolves submitted_commit = git.head(version.draft_ref), verifies git.read_file(commit, version.skill_path) hashes to version.skill_md_sha256, and stores that commit. If the head content no longer matches, raise SkillConflict.
- delete_draft requires no pending submission and does not delete a public release.

- [ ] **Step 1: Write failing tests**

Use fakes for git/content and the real SQLite store. Assert:
- create_draft then list_files returns one SKILL.md.
- save creates v1; saving identical bytes reuses v1; changing bytes creates v2.
- save writes a private active pointer and an outbox event in the same transaction.
- put_file during pending raises SkillConflict.
- submit records submitted_commit equal to the version draft_commit.
- deleting a draft with a public release keeps the public release and only removes private state.

- [ ] **Step 2: Run tests to verify they fail**

Run: uv run python -m pytest backend/tests/test_skill_author_service.py -v

- [ ] **Step 3: Implement the service**

Use build_package for validation. Use a per-skill PostgreSQL advisory lock around save; on SQLite use the transaction's write lock. Never call Git inside an open database transaction. Store the outbox event through record_saved_version, not a separate call after commit.

- [ ] **Step 4: Run tests**

Run: uv run python -m pytest backend/tests/test_skill_author_service.py backend/tests/test_skill_store.py -v

- [ ] **Step 5: Commit**

~~~bash
git add backend/packages/harness/agentkit/skills/author.py backend/tests/test_skill_author_service.py
git commit -m "feat(skills): add private draft author service"
~~~

### Task 7: Review and Public Release Service

**Files:**
- Create: backend/packages/harness/agentkit/skills/review.py
- Test: backend/tests/test_skill_review_service.py

**Interfaces:**
- class SkillReviewService:
  - __init__(store: SqlSkillStore, git: SkillGitRepository, content: SkillContentStore, review: SkillReviewConfig)
  - async list_pending(limit: int = 50) -> list[ReviewItem]
  - async detail(submission_id: str) -> ReviewDetail
- ReviewItem and ReviewDetail are defined in domain.py (Task 2); do not redefine them here.
  - async approve(*, admin_user_id: str, submission_id: str) -> SkillReleaseRecord
  - async reject(*, admin_user_id: str, submission_id: str, reason: str) -> SkillSubmissionRecord
  - async unpublish(*, admin_user_id: str, skill_id: str) -> None
  - async activate(*, admin_user_id: str, skill_id: str, release_id: str) -> None

**Rules:**
- approve checks status == pending, reads submitted_commit, recomputes package_sha256, and rejects on mismatch.
- approve rejects self-approval when review.allow_self_approval is false.
- approve checks public_name is free (same skill is allowed), rejects with SkillConflict 409 otherwise. If the skill already has a public_name, the approved name must equal it; public names are immutable. On first approval, set skills.public_name to the approved name.
- approve calls git.publish_public, then store.record_public_release(submission=..., public_name=..., git_repository=..., git_commit=..., skill_path=..., source_commit=..., actor=...) in one transaction (release + public active + settings enabled=true + outbox + audit).
- reject keeps the private pointer and writes audit.
- unpublish deletes public active and writes a delete outbox event; it never deletes releases.
- activate verifies the release belongs to the skill and updates public active + outbox upsert; it does not commit Git.

- [ ] **Step 1: Write failing tests**

Assert:
- approve copies the exact draft tree to main and records source_commit=submitted_commit and git_commit=public commit.
- approving a submission with a changed draft commit hash raises SkillConflict.
- public name collision raises SkillConflict.
- reject keeps private active.
- unpublish removes public active but keeps release rows.
- rollback sets public active to an older release and enqueues an upsert.

- [ ] **Step 2: Run tests to verify they fail**

Run: uv run python -m pytest backend/tests/test_skill_review_service.py -v

- [ ] **Step 3: Implement approval, rejection, unpublish, and activation**

Use a PostgreSQL advisory lock keyed by the public branch for approval. Do not hold the lock while waiting on HTTP. Git commit happens before the PG release transaction. If PG fails after Git, leave the commit unreferenced and raise a retryable error.

- [ ] **Step 4: Run tests**

Run: uv run python -m pytest backend/tests/test_skill_review_service.py backend/tests/test_skill_author_service.py -v

- [ ] **Step 5: Commit**

~~~bash
git add backend/packages/harness/agentkit/skills/review.py backend/tests/test_skill_review_service.py
git commit -m "feat(skills): add review and public release service"
~~~

### Task 8: Author HTTP API

**Files:**
- Create: backend/app/gateway/models/skills.py
- Create: backend/app/gateway/routers/skill_drafts.py
- Modify: backend/app/gateway/routers/skills.py (list/mine only; keep old routes until Task 15)
- Modify: backend/app/gateway/app.py (include new router)
- Test: backend/tests/test_skill_author_routes.py

**Interfaces:**
- Request models: CreateDraftRequest(name, description), WriteFileRequest(path, content, mode="text"), SubmitResponse, DraftView.
- Response models: SkillListItem(id, name, description, scope, owner_user_id, skill_version, version_id, git_commit, skill_path, status, enabled, global_enabled), DraftTreeResponse(files), SaveResponse(version), SubmissionResponse.
- Routes:
  - GET /api/skills/mine
  - POST /api/skills
  - POST /api/skills/{skill_id}/draft/open (owner starts/finds a private draft from an existing public skill)
  - GET /api/skills/{skill_id}/draft
  - GET /api/skills/{skill_id}/draft/tree
  - GET /api/skills/{skill_id}/draft/file
  - PUT /api/skills/{skill_id}/draft/file
  - POST /api/skills/{skill_id}/draft/files
  - DELETE /api/skills/{skill_id}/draft/file
  - POST /api/skills/{skill_id}/draft/save
  - POST /api/skills/{skill_id}/submit
  - POST /api/skills/{skill_id}/withdraw
  - GET /api/skills/{skill_id}/versions
  - DELETE /api/skills/{skill_id}
- Register /skills/mine before /skills/{skill_id} routes.
- Dependency: get_skill_author_service from app.state.skill_author_service; tests inject a fake.

- [ ] **Step 1: Write failing route tests**

Use FastAPI + AuthSettings.disabled and a fake author service. Assert:
- POST /api/skills with skills:write returns 201 and only the owner can read it.
- GET /api/skills/mine returns private skills for the caller, not another user's.
- PUT draft file with a path traversal returns 422.
- POST submit without a ready version returns 409 with code skill_conflict.
- A user without skills:write gets 403.
- GET /api/skills/{id}/draft on another user's skill returns 404, not 403.

- [ ] **Step 2: Run tests to verify they fail**

Run: uv run python -m pytest backend/tests/test_skill_author_routes.py -v

- [ ] **Step 3: Implement router and Pydantic models**

Use require_scope("skills:write") for writes and skills:read for mine. Map InvalidSkillPackage to 422 and SkillConflict to 409 through existing gateway errors. Do not expose host cache paths. Return container_file only to the owner.

- [ ] **Step 4: Run tests**

Run: uv run python -m pytest backend/tests/test_skill_author_routes.py -v

- [ ] **Step 5: Commit**

~~~bash
git add backend/app/gateway/models/skills.py backend/app/gateway/routers/skill_drafts.py backend/app/gateway/routers/skills.py backend/app/gateway/app.py backend/tests/test_skill_author_routes.py
git commit -m "feat(skills): add author HTTP API"
~~~

### Task 9: Admin Review HTTP API

**Files:**
- Modify: backend/app/gateway/models/skills.py
- Create: backend/app/gateway/routers/skill_reviews.py
- Modify: backend/app/gateway/app.py
- Test: backend/tests/test_skill_review_routes.py

**Interfaces:**
- Request: RejectRequest(reason: str, min_length=1, max_length=2000)
- Response: ReviewQueueResponse(items: list[ReviewSummary]), ReviewDetailResponse, ReleaseResponse, UnpublishResponse, ActivateRequest(release_id: str).
- Routes:
  - GET /api/admin/skills/reviews
  - GET /api/admin/skills/reviews/{submission_id}
  - POST /api/admin/skills/reviews/{submission_id}/approve
  - POST /api/admin/skills/reviews/{submission_id}/reject
  - POST /api/admin/skills/{skill_id}/unpublish
  - POST /api/admin/skills/{skill_id}/activate
- All routes use require_scope("admin").
- Dependency: get_skill_review_service from app.state.skill_review_service.

- [ ] **Step 1: Write failing tests**

Assert:
- A normal user gets 403 on every route.
- The review detail contains the exact submitted_commit, file list, diff, and package size.
- Approve returns a release with skill_id, skill_version, git_repository, git_commit, skill_path.
- Reject requires a non-empty reason.
- Unpublish and activate are admin-only and return 204/200 as specified.

- [ ] **Step 2: Run tests to verify they fail**

Run: uv run python -m pytest backend/tests/test_skill_review_routes.py -v

- [ ] **Step 3: Implement the admin router**

Keep the route order so reviews/{submission_id} is declared before any future generic admin skill route. Do not return private staging content; review detail reads the submitted Git commit. Map SkillNotFound/SkillConflict to existing error bodies.

- [ ] **Step 4: Run tests**

Run: uv run python -m pytest backend/tests/test_skill_review_routes.py -v

- [ ] **Step 5: Commit**

~~~bash
git add backend/app/gateway/models/skills.py backend/app/gateway/routers/skill_reviews.py backend/app/gateway/app.py backend/tests/test_skill_review_routes.py
git commit -m "feat(skills): add admin review API"
~~~

### Task 10: Milvus Active Projection Store

**Files:**
- Create: backend/packages/harness/agentkit/skills/projection.py
- Test: backend/tests/test_skill_projection.py

**Interfaces:**
- dataclass ProjectionRow: projection_key, scope, owner_user_id, skill_id, version_id, skill_version, enabled, name, description, package_sha256, skill_md_sha256, embedding
- dataclass ProjectionHit: projection_key, scope, owner_user_id, skill_id, version_id, score
- class SkillProjectionStore(Protocol):
  - async ensure_collection(dimensions: int) -> None
  - async upsert_active(row: ProjectionRow) -> None
  - async delete_active(projection_key: str) -> None
  - async search(*, user_id: str, private_skill_ids: Sequence[str], vector: Sequence[float], limit: int) -> list[ProjectionHit]
  - async list_projection_keys() -> list[str]
  - async aclose() -> None
- class MilvusSkillProjection:
  - __init__(config: MilvusConfig, *, dimensions: int, client: Any | None = None)
- Collection name default: myflow_skills_active.
- Filter exactly:
  ~~~text
  (scope == "public" and enabled == true and skill_id not in ["s1", "s2"])
  or (scope == "private" and owner_user_id == "u1")
  ~~~
  When private_skill_ids is empty, emit only the public branch.
- One client.search call per invocation. Set anns_field="embedding", limit, output_fields for all row metadata, and consistency_level="Strong" (or the configured value). Create scalar indexes for scope, owner_user_id, and enabled.

- [ ] **Step 1: Write failing tests**

Use a FakeMilvus client modeled on backend/tests/test_knowledge_vector_store.py. Assert:
- upsert uses pk=projection_key and stores scope/owner/version fields.
- delete uses projection_key.
- search sends exactly one search call and the filter contains the public exclusion and private owner branches.
- no private IDs produces a public-only filter.
- collection creation adds scalar indexes for scope, owner_user_id, enabled.

- [ ] **Step 2: Run tests to verify they fail**

Run: uv run python -m pytest backend/tests/test_skill_projection.py -v

- [ ] **Step 3: Implement the protocol and Milvus adapter**

Import pymilvus lazily and raise a clear error if it is absent. Escape string values with json.dumps. Do not filter by version_id. Store no SKILL.md body.

- [ ] **Step 4: Run tests**

Run: uv run python -m pytest backend/tests/test_skill_projection.py backend/tests/test_knowledge_vector_store.py -v

- [ ] **Step 5: Commit**

~~~bash
git add backend/packages/harness/agentkit/skills/projection.py backend/tests/test_skill_projection.py
git commit -m "feat(skills): add active Milvus projection"
~~~

### Task 11: Projection Outbox Worker and Reconciliation

**Files:**
- Create: backend/packages/harness/agentkit/skills/outbox.py
- Test: backend/tests/test_skill_outbox.py

**Interfaces:**
- class SkillIndexOutboxWorker:
  - __init__(store: SqlSkillStore, content: SkillContentStore, embedder: Embedder, projection: SkillProjectionStore, *, poll_seconds: float = 2.0)
  - async run_once(limit: int = 20) -> int
  - async reconcile() -> int
  - async prune(keep_versions_per_skill: int = 20) -> int
  - async start() -> None
  - async stop() -> None
- Provider function: async build_projection_row(store, content, embedder, event: OutboxEvent) -> ProjectionRow | None. Returns None when the event is stale or the active pointer no longer matches.
- Stale guard: re-read the current public/private active and settings before calling projection. Skip when event.version_id or event.payload.get("enabled") does not match current state.

- [ ] **Step 1: Write failing tests**

Assert:
- a pending upsert event produces one projection.upsert_active call with the current version;
- a delete event calls delete_active;
- an old upsert event after a newer private active pointer is skipped and marked done;
- an embedding failure calls retry_outbox and does not mark done;
- reconcile uses projection.list_projection_keys(), upserts every current active row, and deletes keys that no longer have an active pointer;
- prune deletes commit/cache directories and projection rows not referenced by active, pending, or the most recent N versions of a skill, while keeping all metadata.

- [ ] **Step 2: Run tests to verify they fail**

Run: uv run python -m pytest backend/tests/test_skill_outbox.py -v

- [ ] **Step 3: Implement the worker**

Use run_once in tests and a background asyncio task in start. Coalesce multiple pending events for the same projection_key by processing the newest event. Never write to Milvus inside a PG transaction. Set skill_versions.index_status to ready on successful upsert and failed on terminal failure. Implement prune with content.prune and delete_active; never delete skill_versions, skill_releases, submissions, or audit rows. Log only ids and hashes.

- [ ] **Step 4: Run tests**

Run: uv run python -m pytest backend/tests/test_skill_outbox.py backend/tests/test_skill_projection.py -v

- [ ] **Step 5: Commit**

~~~bash
git add backend/packages/harness/agentkit/skills/outbox.py backend/tests/test_skill_outbox.py
git commit -m "feat(skills): add active projection outbox worker"
~~~

### Task 12: One-Query Retrieval and search_and_load_skills

**Files:**
- Create: backend/packages/harness/agentkit/skills/search.py
- Modify: backend/packages/harness/agentkit/tools/builtin/search_and_load_skills.py
- Test: backend/tests/test_skill_search_service.py
- Test: backend/tests/test_search_and_load_skills_tool.py

**Interfaces:**
- dataclass SkillSearchHit: skill: Skill; body: str; score: float; scope: str; version_id: str; release_id: str | None
- dataclass SkillSearchOutcome: hits: tuple[SkillSearchHit, ...]; candidates: int; warnings: tuple[str, ...]
- class SkillSearchService:
  - __init__(store: SqlSkillStore, projection: SkillProjectionStore, content: SkillContentStore, embedder: Embedder, config: SkillSearchConfig)
  - async search(*, user_id: str, task_query: str, domain_keywords: Sequence[str], available_skills: frozenset[str] | None, prefs: Mapping[str, bool] | None) -> SkillSearchOutcome
- search_and_load_skills gets the service from get_skill_runtime() and returns the same payload shape plus scope/version_id/release_id.

**Rules:**
- Generate query embedding once.
- Read private_skill_ids from the store/snapshot.
- Call projection.search once.
- Confirm hits in one PG join, then read bodies from content, then sort/dedupe.
- Public branch excludes the caller's private skill_ids; private branch is owner-only.
- If both public and private survive confirmation for one skill_id, keep private unconditionally; do not compare scores.
- If a private body is unreadable, do not invent a public fallback; drop it and warn.
- If Milvus/embedding fails, use the existing lexical scorer over visible skills and return lexical_fallback.
- Apply max_body_chars and max_context_chars with the existing clip_body.

- [ ] **Step 1: Write failing service tests**

~~~python
@pytest.mark.asyncio
async def test_one_search_and_private_precedence(service, fake_projection):
    outcome = await service.search(user_id="u1", task_query="pgvector", domain_keywords=[], available_skills=None, prefs=None)
    assert len(fake_projection.searches) == 1
    assert "skill_id not in" in fake_projection.searches[0]["filter"]
    assert "owner_user_id" in fake_projection.searches[0]["filter"]
    assert [hit.scope for hit in outcome.hits] == ["private"]

@pytest.mark.asyncio
async def test_other_users_private_hits_are_rejected(service, fake_projection):
    fake_projection.hits = [hit(scope="private", owner="u2", version="v2")]
    assert await service.search(user_id="u1", ...) == empty_outcome()
~~~

Add tests for one search call, body budget, lexical fallback, and no public fallback when private body is missing.

- [ ] **Step 2: Run tests to verify they fail**

Run: uv run python -m pytest backend/tests/test_skill_search_service.py backend/tests/test_search_and_load_skills_tool.py -v

- [ ] **Step 3: Implement the search service and tool adapter**

Use the existing Embedder protocol and SearchOutcome budget conventions where practical. Keep the one-OR-query filter builder in projection.py, not in the service. Return warnings: vector_unavailable, lexical_fallback, content_unavailable.

- [ ] **Step 4: Run tests**

Run: uv run python -m pytest backend/tests/test_skill_search_service.py backend/tests/test_search_and_load_skills_tool.py -v

- [ ] **Step 5: Commit**

~~~bash
git add backend/packages/harness/agentkit/skills/search.py backend/packages/harness/agentkit/tools/builtin/search_and_load_skills.py backend/tests/test_skill_search_service.py backend/tests/test_search_and_load_skills_tool.py
git commit -m "feat(skills): add active projection retrieval"
~~~

### Task 13: Catalog Snapshot, Prompt Index, and SkillRegistry

**Files:**
- Create: backend/packages/harness/agentkit/skills/catalog.py
- Modify: backend/packages/harness/agentkit/skills/storage.py
- Modify: backend/packages/harness/agentkit/skills/prompt.py
- Modify: backend/packages/harness/agentkit/skills/prefs.py
- Test: backend/tests/test_skill_catalog_snapshot.py
- Test: backend/tests/test_skill_index.py

**Interfaces:**
- dataclass SkillSnapshot: epoch: str; public: tuple[Skill, ...]; private_by_owner: Mapping[str, tuple[Skill, ...]]
  - def visible_for(self, user_id: str | None, *, available_skills: frozenset[str] | None) -> list[Skill]
- class SkillCatalogService:
  - __init__(store: SqlSkillStore, content: SkillContentStore)
  - async refresh() -> SkillSnapshot
  - def current(self) -> SkillSnapshot
- def get_skill_snapshot() -> SkillSnapshot
- def set_skill_snapshot(snapshot: SkillSnapshot) -> None
- Modify SkillRegistry to accept snapshot: SkillSnapshot | None = None and use it when present; otherwise keep the legacy filesystem scanner for tests.
- SqlSkillStore gains async all_active() -> list[VisibleSkill] for snapshot refresh.

**Rules:**
- The snapshot is process-wide and immutable. It contains public active skills plus private active skills for all owners; visible_for filters by owner and personal prefs.
- For an owner, a private active skill suppresses the public row with the same skill_id.
- build_skills_section renders two roots: /mnt/skills/public/<name>/SKILL.md and /mnt/skills/private/<name>/SKILL.md. Mark entries [public] or [private].
- Prompt cache key includes scope, owner_user_id, version_id, and description.
- SkillRegistry.find prefers a private hit for the owner when names collide.

- [ ] **Step 1: Write failing tests**

~~~python
def test_snapshot_visible_for_filters_private_owner(snapshot):
    assert [s.skill_id for s in snapshot.visible_for("u1", available_skills=None)] == ["s1", "s2"]
    assert [s.skill_id for s in snapshot.visible_for("u2", available_skills=None)] == ["s2"]

def test_private_skill_suppresses_public_skill_for_owner(snapshot):
    ids = [s.skill_id for s in snapshot.visible_for("u1", available_skills=None)]
    assert ids.count("s1") == 1
    assert next(s for s in snapshot.visible_for("u1", available_skills=None) if s.skill_id == "s1").scope == "private"

def test_prompt_renders_public_and_private_roots(snapshot):
    text = build_skills_section(config=..., registry=SkillRegistry(snapshot=snapshot, user_id="u1"))
    assert "/mnt/skills/public/" in text
    assert "/mnt/skills/private/" in text
~~~

- [ ] **Step 2: Run tests to verify they fail**

Run: uv run python -m pytest backend/tests/test_skill_catalog_snapshot.py backend/tests/test_skill_index.py -v

- [ ] **Step 3: Implement snapshot loading and prompt rendering**

Load content paths with SkillContentStore.ensure for each active commit; skip a skill whose content is missing and log a warning. Do not read bodies into the snapshot. Keep legacy filesystem behavior when snapshot is None.

- [ ] **Step 4: Run tests**

Run: uv run python -m pytest backend/tests/test_skill_catalog_snapshot.py backend/tests/test_skill_index.py backend/tests/test_skill_loading.py -v

- [ ] **Step 5: Commit**

~~~bash
git add backend/packages/harness/agentkit/skills/catalog.py backend/packages/harness/agentkit/skills/storage.py backend/packages/harness/agentkit/skills/prompt.py backend/packages/harness/agentkit/skills/prefs.py backend/tests/test_skill_catalog_snapshot.py backend/tests/test_skill_index.py
git commit -m "feat(skills): add catalog snapshot and private prompt index"
~~~

### Task 14: Snapshot-Aware Middleware and Per-User Sandbox Mounts

**Files:**
- Modify: backend/packages/harness/agentkit/agents/middlewares/assembly.py
- Modify: backend/packages/harness/agentkit/agents/middlewares/skill_activation_middleware.py
- Modify: backend/packages/harness/agentkit/agents/middlewares/skill_tool_policy_middleware.py
- Modify: backend/packages/harness/agentkit/agents/middlewares/durable_context_middleware.py
- Modify: backend/packages/harness/agentkit/sandbox/local/local_sandbox_provider.py
- Modify: backend/packages/harness/agentkit/config/runtime_paths.py (only if a helper is needed)
- Test: backend/tests/test_skill_private_mounts.py
- Test: backend/tests/test_skill_activation_request_only.py

**Interfaces:**
- build_skill_middlewares passes the current snapshot registry to Activation and Policy.
- SkillActivationMiddleware reads the SKILL.md body from Skill.skill_file; no filesystem rescan.
- SkillToolPolicyMiddleware indexes by Skill.container_file from the snapshot.
- LocalSandboxProvider builds:
  - static /mnt/skills/public -> <cache>/active/public
  - per-sandbox /mnt/skills/private -> <cache>/active/users/<user_id>
  - no process-wide /mnt/skills mapping to the whole cache.
- SkillContentStore.materialize_public and materialize_private create the active trees before sandbox creation.

- [ ] **Step 1: Write failing tests**

Assert:
- user A's sandbox mappings include /mnt/skills/private mapped to A's active tree;
- user B's mapping points to B's tree and never A's;
- a private skill path is absent from B's candidate mappings;
- slash activation of a private skill uses the private body and writes the private container_file to policy context;
- public and private skills with the same name resolve private first for the owner.

- [ ] **Step 2: Run tests to verify they fail**

Run: uv run python -m pytest backend/tests/test_skill_private_mounts.py backend/tests/test_skill_activation_request_only.py -v

- [ ] **Step 3: Implement snapshot-aware middleware and mounts**

Do not widen the tool policy boundary. If a sandbox provider cannot support per-user mounts, omit container_file for private skills and keep body injection only. Update DurableContext path validation only if the new public/private roots require it; both remain under container_path.

- [ ] **Step 4: Run tests**

Run: uv run python -m pytest backend/tests/test_skill_private_mounts.py backend/tests/test_skill_activation_request_only.py backend/tests/test_skill_policy.py backend/tests/test_durable_context.py -v

- [ ] **Step 5: Commit**

~~~bash
git add backend/packages/harness/agentkit/agents/middlewares backend/packages/harness/agentkit/sandbox/local/local_sandbox_provider.py backend/tests/test_skill_private_mounts.py backend/tests/test_skill_activation_request_only.py
git commit -m "feat(skills): wire snapshots and per-user private mounts"
~~~

### Task 15: Skills List, Personal Preference, Global Toggle, Reload, and App Wiring

**Files:**
- Modify: backend/app/gateway/skills_catalog.py
- Modify: backend/app/gateway/routers/skills.py
- Modify: backend/app/gateway/models/response.py
- Modify: backend/app/gateway/models/request.py
- Modify: backend/app/gateway/app.py
- Modify: backend/packages/harness/agentkit/config/skills_prefs.py
- Test: backend/tests/test_skills_catalog_routes.py
- Test: backend/tests/test_skill_runtime_wiring.py

**Interfaces:**
- SkillRuntime dataclass holds store, git, content, author, review, search, catalog, outbox, projection.
- configure_skill_runtime(runtime), get_skill_runtime(), reset_skill_runtime().
- app lifespan builds SkillRuntime when skills.source == "git"; starts outbox worker; stops it and closes projection on shutdown.
- GET /api/skills returns public + caller private from snapshot; fields: skill_id, name, scope, description, skill_version, version_id, git_commit, skill_path, status, enabled, global_enabled (admin only), owner_user_id (self/admin only).
- PUT /api/skills/{skill_id}/preference writes skills_prefs.json keyed by skill_id.
- PATCH /api/skills/{skill_id} writes skill_settings in PG and enqueues public active upsert/delete; admin only; audit.
- POST /api/skills/reload calls catalog.refresh().

- [ ] **Step 1: Write failing tests**

Assert:
- user A sees public + A private, not B private;
- user B sees public + B private;
- personal preference is keyed by skill_id and does not affect another user;
- global toggle writes PG and enqueues an outbox event, not extensions_config.json;
- reload refreshes the snapshot;
- a user without admin gets 403 for global toggle;
- app lifespan starts and stops the outbox task (fake runtime).

- [ ] **Step 2: Run tests to verify they fail**

Run: uv run python -m pytest backend/tests/test_skills_catalog_routes.py backend/tests/test_skill_runtime_wiring.py -v

- [ ] **Step 3: Implement runtime wiring and route behavior**

Keep the old filesystem registry only when source=filesystem. In git mode, missing runtime or empty snapshot is fail-closed. Do not read extensions_config.json skills in git mode. Keep response fields backward compatible; add new fields.

- [ ] **Step 4: Run tests**

Run: uv run python -m pytest backend/tests/test_skills_catalog_routes.py backend/tests/test_skill_runtime_wiring.py backend/tests/test_mcp_skills_routes.py -v

- [ ] **Step 5: Commit**

~~~bash
git add backend/app/gateway/skills_catalog.py backend/app/gateway/routers/skills.py backend/app/gateway/models/response.py backend/app/gateway/models/request.py backend/app/gateway/app.py backend/packages/harness/agentkit/config/skills_prefs.py backend/tests/test_skills_catalog_routes.py backend/tests/test_skill_runtime_wiring.py
git commit -m "feat(skills): wire runtime and visibility routes"
~~~

### Task 16: Cutover Migration, Bootstrap, Config Examples, and End-to-End Backend Test

**Files:**
- Create: backend/scripts/migrate_skill_state.py
- Create: backend/scripts/bootstrap_public_skills.py
- Modify: config.example.yaml
- Modify: config.yaml
- Test: backend/tests/test_skill_catalog_e2e.py

**Interfaces:**
- migrate_skill_state.py reads extensions_config.json and skills_prefs.json, maps old names to public skill_ids, writes skill_settings, and rewrites prefs keys to skill_id. It is idempotent and prints a summary.
- bootstrap_public_skills.py takes a source directory and admin user id, creates private drafts for each public/custom skill, saves them, submits, and approves them through SkillAuthorService/SkillReviewService. It refuses to run unless --confirm is passed.
- config.yaml and config.example.yaml set skills.source: git and include creation/git/review/index/search.retention blocks from the spec. Secrets use $ENV.

- [ ] **Step 1: Write the failing end-to-end test**

~~~python
@pytest.mark.asyncio
async def test_author_private_search_then_approve_then_other_user_search(runtime, tmp_path):
    skill = await runtime.author.create_draft(owner_user_id="u1", name="pdf-tools", description="PDF")
    await runtime.author.put_file(owner_user_id="u1", skill_id=skill.id, path="SKILL.md", content=valid_skill_bytes("pdf-tools"))
    version = await runtime.author.save(owner_user_id="u1", skill_id=skill.id)
    await runtime.outbox.run_once()
    own = await runtime.search.search(user_id="u1", task_query="PDF", domain_keywords=[], available_skills=None, prefs=None)
    assert own.hits[0].scope == "private"
    sub = await runtime.author.submit(owner_user_id="u1", skill_id=skill.id)
    release = await runtime.review.approve(admin_user_id="admin", submission_id=sub.id)
    await runtime.outbox.run_once()
    other = await runtime.search.search(user_id="u2", task_query="PDF", domain_keywords=[], available_skills=None, prefs=None)
    assert other.hits[0].scope == "public"
    assert other.hits[0].release_id == release.id
~~~

Add tests that the bootstrap script refuses without --confirm and that migration is idempotent.

- [ ] **Step 2: Run test to verify it fails**

Run: uv run python -m pytest backend/tests/test_skill_catalog_e2e.py -v

- [ ] **Step 3: Implement scripts, config examples, and test wiring**

Use a local bare Git repository and fake projection/embedder in the test. Do not require live Milvus. The bootstrap script must not execute scripts from the source directory.

- [ ] **Step 4: Run backend regression**

Run: uv run python -m pytest backend/tests/test_skill_catalog_e2e.py backend/tests/test_skill_author_routes.py backend/tests/test_skill_review_routes.py backend/tests/test_skills_catalog_routes.py backend/tests/test_search_and_load_skills_tool.py backend/tests/test_skill_private_mounts.py -v

- [ ] **Step 5: Commit**

~~~bash
git add backend/scripts/migrate_skill_state.py backend/scripts/bootstrap_public_skills.py config.example.yaml config.yaml backend/tests/test_skill_catalog_e2e.py
git commit -m "feat(skills): add cutover scripts and config"
~~~

### Task 17: Frontend Skills API Client and Types

**Files:**
- Create: frontend/src/core/api/skills.ts
- Create: frontend/src/core/api/skills.test.ts
- Modify: frontend/src/core/types.ts only if a shared SkillEntry type is still referenced

**Interfaces:**
- type SkillScope = "public" | "private"
- type SkillStatus = "draft" | "pending" | "approved" | "rejected" | "withdrawn"
- interface SkillListItem { skill_id: string; name: string; description: string; scope: SkillScope; owner_user_id?: string | null; skill_version: string | null; version_id: string | null; git_commit: string | null; skill_path: string | null; status: SkillStatus; enabled: boolean; global_enabled?: boolean | null; container_file?: string | null }
- interface DraftFile { path: string; size_bytes: number; mode: string; sha256: string }
- interface SkillVersion { version_id: string; skill_version: string; package_sha256: string; skill_md_sha256: string; draft_commit: string | null; index_status: string; created_at: string }
- interface ReviewSummary { submission_id: string; skill_id: string; name: string; version: string; owner_user_id: string; submitted_at: string; submitted_commit: string }
- interface ReviewDetail extends ReviewSummary { files: DraftFile[]; skill_md: string; diff: string; metadata: Record<string, unknown>; package_bytes: number }
- Functions: listSkills, listMySkills, createSkill, openDraft, getDraft, listDraftFiles, readDraftFile, writeDraftFile, uploadDraftFiles, deleteDraftFile, saveDraft, submitSkill, withdrawSkill, deleteSkill, listReviews, getReview, approveReview, rejectReview, unpublishSkill, activateRelease, setSkillPreference.
- All errors use a local SkillApiError with status/code/detail/traceId, mirroring KnowledgeApiError.

- [ ] **Step 1: Write failing contract tests**

Use vi.stubGlobal("fetch", ...) and assert:
- createSkill posts to /api/skills with JSON and credentials/csrf through apiFetch;
- openDraft posts to /api/skills/{id}/draft/open and returns the staged file list;
- uploadDraftFiles uses FormData and does not set Content-Type manually;
- approveReview posts to /api/admin/skills/reviews/{id}/approve;
- readDraftFile encodes the path query parameter;
- error bodies produce SkillApiError with code.

- [ ] **Step 2: Run tests to verify they fail**

Run: cd frontend && pnpm vitest run src/core/api/skills.test.ts

- [ ] **Step 3: Implement the typed client**

Use apiFetch and apiUrl from ./base. Keep a single request helper. Do not call fetch directly. Use encodeURIComponent on skill_id and submission_id.

- [ ] **Step 4: Run tests**

Run: cd frontend && pnpm vitest run src/core/api/skills.test.ts

- [ ] **Step 5: Commit**

~~~bash
git add frontend/src/core/api/skills.ts frontend/src/core/api/skills.test.ts
git commit -m "feat(frontend): add skills API client"
~~~

### Task 18: My Skills Page and Create Draft

**Files:**
- Create: frontend/src/app/workspace/skills/page.tsx
- Create: frontend/src/components/skills/my-skills-page.tsx
- Create: frontend/src/components/skills/create-skill-dialog.tsx
- Create: frontend/src/components/skills/skill-status-badge.tsx
- Test: frontend/src/components/skills/my-skills-page.dom.test.tsx

**Interfaces:**
- MySkillsPage uses useQuery(["skills","mine"], listMySkills) and exposes a create dialog.
- CreateSkillDialog fields: name and description; submits createSkill; on success routes to /workspace/skills/<skill_id>.
- Status badge labels: draft=草稿, pending=审核中, approved=已通过, rejected=已驳回, withdrawn=已撤回.
- Page lists both private drafts and public skills; private rows link to the editor; own public rows show an 编辑 action that calls openDraft before routing to the editor.

- [ ] **Step 1: Write failing DOM tests**

Assert:
- empty state renders "还没有技能" and a create button;
- a private draft row shows "私有" and "草稿";
- a public row shows "公共";
- submitting the create dialog calls createSkill with name/description and navigates to the new id;
- an API error renders a human-readable alert.

- [ ] **Step 2: Run tests to verify they fail**

Run: cd frontend && pnpm vitest run src/components/skills/my-skills-page.dom.test.tsx

- [ ] **Step 3: Implement page, dialog, and badge**

Follow the knowledge list page patterns for QueryProvider, loading skeleton, empty state, and error alert. Do not fetch inside the presentational badge.

- [ ] **Step 4: Run tests and typecheck**

Run: cd frontend && pnpm vitest run src/components/skills/my-skills-page.dom.test.tsx && pnpm typecheck

- [ ] **Step 5: Commit**

~~~bash
git add frontend/src/app/workspace/skills/page.tsx frontend/src/components/skills/my-skills-page.tsx frontend/src/components/skills/create-skill-dialog.tsx frontend/src/components/skills/skill-status-badge.tsx frontend/src/components/skills/my-skills-page.dom.test.tsx
git commit -m "feat(frontend): add my skills page"
~~~

### Task 19: Draft Editor, File Tree, Save, Upload, and Submit

**Files:**
- Create: frontend/src/app/workspace/skills/[skill_id]/page.tsx
- Create: frontend/src/components/skills/skill-editor-page.tsx
- Create: frontend/src/components/skills/skill-file-tree.tsx
- Create: frontend/src/components/skills/skill-editor-actions.tsx
- Test: frontend/src/components/skills/skill-editor-page.dom.test.tsx

**Interfaces:**
- SkillEditorPage loads getDraft, listDraftFiles, and readDraftFile for the selected path.
- SKILL.md is editable in a textarea; other text files are editable through the same endpoint.
- File tree has an "上传附件" control that calls uploadDraftFiles.
- Actions: 保存 (saveDraft), 提交审核 (submitSkill), 撤回 (withdrawSkill), 删除草稿 (deleteSkill).
- Save is disabled while a submission is pending; the page shows "审核中，请先撤回".
- The page displays version_id, draft_commit, index_status, and the public review status.

- [ ] **Step 1: Write failing DOM tests**

Assert:
- loading the page renders SKILL.md content in a textarea;
- editing and clicking 保存 calls writeDraftFile then saveDraft;
- uploading a file calls uploadDraftFiles with FormData;
- pending status disables save and shows the withdraw action;
- submit error 409 renders the conflict message;
- deleting requires a confirmation dialog.

- [ ] **Step 2: Run tests to verify they fail**

Run: cd frontend && pnpm vitest run src/components/skills/skill-editor-page.dom.test.tsx

- [ ] **Step 3: Implement editor with explicit loading/error states**

Use local state for the selected path and text. Invalidate ["skills","mine"] and ["skills","draft", skill_id] after successful actions. Do not auto-save; unsaved edits stay local until the user clicks 保存.

- [ ] **Step 4: Run tests and typecheck**

Run: cd frontend && pnpm vitest run src/components/skills/skill-editor-page.dom.test.tsx && pnpm typecheck

- [ ] **Step 5: Commit**

~~~bash
git add "frontend/src/app/workspace/skills/[skill_id]/page.tsx" frontend/src/components/skills/skill-editor-page.tsx frontend/src/components/skills/skill-file-tree.tsx frontend/src/components/skills/skill-editor-actions.tsx frontend/src/components/skills/skill-editor-page.dom.test.tsx
git commit -m "feat(frontend): add private skill editor"
~~~

### Task 20: Admin Review Queue, Sidebar Navigation, and E2E

**Files:**
- Create: frontend/src/app/workspace/admin/skills/page.tsx
- Create: frontend/src/components/skills/review-queue-page.tsx
- Create: frontend/src/components/skills/review-detail.tsx
- Modify: frontend/src/components/workspace/sidebar.tsx
- Modify: frontend/src/app/workspace/sidebar-container.tsx
- Test: frontend/src/components/skills/review-queue-page.dom.test.tsx
- Test: frontend/e2e/skills.spec.ts

**Interfaces:**
- ReviewQueuePage uses useQuery(["skills","reviews"], listReviews) and shows pending count.
- ReviewDetail shows author, version, submitted_commit, file tree, SKILL.md, diff, package size, approve and reject actions.
- Reject requires a non-empty reason.
- Sidebar adds a 技能 link to /workspace/skills; an 审核 link to /workspace/admin/skills is rendered only when useSession().user?.system_role === "admin".
- Active routes highlight via pathname.startsWith.

- [ ] **Step 1: Write failing DOM tests**

Assert:
- a non-admin session does not render the 审核 link;
- an admin session renders it;
- the review queue shows pending submissions and a detail link;
- approve calls approveReview and removes the row;
- reject with empty reason is blocked;
- approve/reject errors render an alert.

- [ ] **Step 2: Run tests to verify they fail**

Run: cd frontend && pnpm vitest run src/components/skills/review-queue-page.dom.test.tsx

- [ ] **Step 3: Implement review UI and nav**

Use the same auth/session hook as UserMenu/SessionGate. Do not hide the route only in the sidebar; the backend admin scope is the real gate. Add Playwright coverage for: user creates draft, saves, submits; admin sees it, approves; another session sees the public skill.

- [ ] **Step 4: Run tests and typecheck**

Run: cd frontend && pnpm vitest run src/components/skills/review-queue-page.dom.test.tsx src/components/workspace/sidebar.dom.test.tsx && pnpm typecheck

- [ ] **Step 5: Commit**

~~~bash
git add frontend/src/app/workspace/admin/skills/page.tsx frontend/src/components/skills/review-queue-page.tsx frontend/src/components/skills/review-detail.tsx frontend/src/components/workspace/sidebar.tsx frontend/src/app/workspace/sidebar-container.tsx frontend/src/components/skills/review-queue-page.dom.test.tsx frontend/e2e/skills.spec.ts
git commit -m "feat(frontend): add admin review and skills navigation"
~~~

## Phase 2：检索漏斗（M11.1）

Tasks 1-20 shipped a working catalog whose retrieval is **单通道**：向量命中 → PG 确认 →
按 Milvus 分数排序；BM25 只是降级兜底（`search.py::_lexical` 仅在向量不可用时运行），
精排（`skills.search.rerank`）在 catalog 模式下完全没接。Phase 2 把它改成设计 §7.4 的
三阶段漏斗：**元数据向量召回 → 正文 BM25 召回 → 正文精排**。

顺序不能颠倒：先接精排（纯增益，不动召回），再加 BM25 融合（改召回，需要观测），最后
才改嵌入文本（让全部存量向量失效，必须配强制重建）。

### Task 21: Rerank 接线（不动召回，先拿精度）

**Files:**
- Modify: backend/packages/harness/agentkit/skills/search.py
- Modify: backend/app/gateway/skills_catalog.py
- Test: backend/tests/test_skill_search_service.py

**Interfaces:**
- `SkillSearchService.__init__(..., reranker: Reranker | None = None)`；`Reranker` 协议
  已在 `skills/router.py` 定义（`rerank(*, query, documents)`）。
- `build_skill_runtime` 按 `skills.search.rerank.enabled` 构建 reranker（复用
  `agentkit.knowledge` 的 cohere/tei 客户端），未启用时为 None。
- 精排发生在 `_prefer_private` 去重**之后**、`_load` **之前**；文档是正文截断到
  `rerank.max_document_chars`；失败回退融合顺序并记 `rerank_unavailable`。

- [ ] **Step 1: Write the failing tests**

~~~python
async def test_rerank_reorders_confirmed_candidates_using_bodies(harness):
    # 向量分数把 A 排第一，但精排说 B 更相关 → B 进 top_k 第一
async def test_rerank_never_reorders_a_private_copy_below_the_public_one(harness):
    # 同一 skill_id 的公共/私有副本：去重后再精排，结果只有私有
async def test_rerank_failure_falls_back_to_the_fused_order(harness, caplog):
    # reranker 抛错 → 顺序不变 + warning + 仍返回结果
async def test_rerank_documents_are_clipped_to_max_document_chars(harness):
~~~

- [ ] **Step 2: Run tests to verify they fail**

Run: uv run python -m pytest backend/tests/test_skill_search_service.py -v

- [ ] **Step 3: Wire the reranker and the clipping**

- [ ] **Step 4: Run the regression**

Run: uv run python -m pytest backend/tests/test_skill_search_service.py backend/tests/test_skill_runtime_wiring.py backend/tests/test_search_and_load_skills_tool.py backend/tests/test_skill_catalog_e2e.py -v

- [ ] **Step 5: Commit**

~~~bash
git commit -m "feat(skills): rerank skill candidates on their bodies"
~~~

### Task 22: BM25 变成并行通道 + RRF 融合

**Files:**
- Modify: backend/packages/harness/agentkit/skills/search.py
- Modify: backend/packages/harness/agentkit/skills/content.py（正文 LRU 缓存）
- Test: backend/tests/test_skill_search_service.py

**Interfaces:**
- 稀疏召回用 `_document(skill, body)` + `lexical_scores(...)`，输入是 PG 可见集合；
  正文按 `(commit, path)` 做进程内 LRU，避免每次检索 O(可见技能数) 次磁盘读。
- 融合：私有去重后，两路名次按 `search.rrf_k` 做 RRF；`min_score` 作用在融合分数上；
  `SearchOutcome.channels` 报告真实命中的通道，`warnings` 区分
  `lexical_unavailable` / `vector_unavailable`。
- 两路都不可用 → 空结果 + 原因（fail-safe，不返回未排序候选）。

- [ ] **Step 1: Write the failing tests**

~~~python
async def test_bm25_and_vector_scores_are_fused_with_rrf(harness):
    # 只在正文里出现的报错原文，BM25 命中、向量不命中 → 融合后进入 top_k
async def test_a_body_read_is_served_from_the_lru_on_the_second_query(harness):
async def test_min_score_applies_to_the_fused_score(harness):
async def test_both_channels_down_returns_no_hits_with_reasons(harness):
~~~

- [ ] **Step 2: Run tests to verify they fail**

Run: uv run python -m pytest backend/tests/test_skill_search_service.py -v

- [ ] **Step 3: Implement the sparse channel, the LRU, and the fusion**

- [ ] **Step 4: Run the regression**

Run: uv run python -m pytest backend/tests/test_skill_search_service.py backend/tests/test_skill_catalog_e2e.py backend/tests/test_search_and_load_skills_tool.py -v

- [ ] **Step 5: Commit**

~~~bash
git commit -m "feat(skills): fuse BM25 body recall with the vector channel"
~~~

### Task 23: 嵌入文本改为 name + description，并加管理员重建

**Files:**
- Modify: backend/packages/harness/agentkit/skills/outbox.py（`_projection_row` 的文本）
- Modify: backend/app/gateway/routers/skill_reviews.py 或 skills_catalog.py（reindex 路由）
- Modify: backend/app/gateway/skills_catalog.py（路由注册、审计）
- Test: backend/tests/test_skill_outbox.py, backend/tests/test_skills_catalog_routes.py

**Interfaces:**
- 投影文本 = `{name}\n{description}`，正文不再参与 embedding（设计 §7.2）。
- `POST /api/admin/skills/reindex`：`skills:write` + 审计，语义 `reconcile(force=True)`，
  返回 `{reconciled, deleted}`；没有运行时 → 503 `skill_runtime_unavailable`。
- 代码里记 `PROJECTION_TEXT_VERSION`，写进日志与 reindex 响应，便于运维核对重建是否做过。

- [ ] **Step 1: Write the failing tests**

~~~python
async def test_projection_text_is_name_and_description_only(harness):
    # 正文改动不改变 embedded text；描述改动才改变
async def test_admin_reindex_forces_a_full_reprojection(harness, client):
    # 健康的行也被重写；多余 key 被删除；审计一条
async def test_reindex_requires_admin_and_a_runtime(client):
~~~

- [ ] **Step 2: Run tests to verify they fail**

Run: uv run python -m pytest backend/tests/test_skill_outbox.py backend/tests/test_skills_catalog_routes.py -v

- [ ] **Step 3: Implement the text change and the admin reindex route**

- [ ] **Step 4: Run the regression**

Run: uv run python -m pytest backend/tests/test_skill_outbox.py backend/tests/test_skills_catalog_routes.py backend/tests/test_skill_search_service.py backend/tests/test_skill_catalog_e2e.py backend/tests/test_skill_review_routes.py -v

- [ ] **Step 5: Commit**

~~~bash
git commit -m "feat(skills): embed skill metadata and add an admin reindex"
~~~

### Phase 2 部署步骤

1. 部署代码（此时检索行为与现在一致，只是多了精排/融合）；
2. 观察 `search_and_load_skills` 的命中质量与延迟；
3. 换模型或换嵌入文本后，调一次 `POST /api/admin/skills/reindex`；不调的话旧向量会一直
   留在 `myflow_skills_active`（对账在稳态会跳过健康行）；
4. 回滚代码不需要重建：旧代码读的仍是同一批向量字段。

## Self-Review

- Spec coverage: tasks 1-16 cover schema, Git, staging, author/review, active projection, outbox, one-query search, snapshot, sandbox, API, cutover; tasks 17-20 cover the author and admin UI.
- Review Focus tests: private isolation is covered by tasks 8, 12, 14, 15; one-query private precedence by task 12; outbox stale guard by task 11; approval hash freeze by task 7; Git-before-PG failure by tasks 6/7.
- Type consistency: version_id is the only globally unique version key; skill_version is always paired with skill_id; projection_key is the active row key; submitted_commit is the reviewed commit.
- Deliberate builder choices: one shared Git repository with per-user draft refs; global immutable catalog snapshot with per-user view; one OR Milvus search with private skill_id exclusion; no public fallback when a private projection is missing; public_name immutable after approval.
- Phase 2（tasks 21-23）把检索从“向量单通道 + BM25 兜底”改成设计 §7.4 的三阶段漏斗：元数据向量召回、正文 BM25 召回、正文精排；嵌入文本只保留 name + description，代价是召回天花板取决于描述质量，由 BM25 正文通道与精排补偿。

## Execution Handoff

The plan is intentionally split into backend tasks 1-16 and frontend tasks 17-20. Implement backend first; it produces a working API, retrieval path, and sandbox behavior. The frontend tasks then consume the API.


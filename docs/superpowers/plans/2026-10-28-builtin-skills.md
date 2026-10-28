# Built-in Skills Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add project-shipped built-in skills under skills/builtin that are always indexed, progressively loaded through read_file or slash, never searched, and shown read-only in the UI.

**Architecture:** Add a builtin overlay beside the Git catalog and filesystem scanner. A cached scanner loads Skill objects with scope builtin; SkillRegistry merges them over catalog rows; the sandbox mounts the builtin directory read-only; the API and UI expose a third scope without adding edit or preference controls.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, LangChain, Next.js 16, React 19, Vitest.

**Spec:** docs/superpowers/specs/2026-10-28-builtin-skills-design.md

## Global Constraints

- Built-in skills live in skills/builtin/<name>/SKILL.md; path config is skills.builtin.path, default skills/builtin.
- Built-in skills do not write Git or PostgreSQL, do not enter review, and require no database migration.
- Built-in skills are always enabled; skills_prefs.json and the global skill settings never hide them.
- Built-in skills do not participate in search_and_load_skills. Discovery is L0 index then read_file or /name.
- The builtin mount is <skills.container_path>/builtin, default /mnt/skills/builtin, and is read-only.
- Built-in skills respect the agent-level available_skills allowlist in registry, slash, and policy paths.
- Built-in names are reserved: runtime collision hides the catalog row and logs a warning; authoring and approval reject the name.
- No symlinks in the builtin tree; no server-side execution of builtin scripts.
- Python tests: uv run python -m pytest backend/tests/<file> -v from the repo root.
- Frontend tests: cd frontend && pnpm vitest run <path>.
- All skill text is UTF-8; skill names match ^[a-z0-9]+(?:-[a-z0-9]+)*$.

## Review Focus

- A missing or unreadable skills/builtin directory must not crash startup, list, prompt, or slash; it must warn once and recover when files appear.
- A stale skills_prefs.json entry with the builtin name set to false must not hide the builtin from prompt, slash, policy, or API.
- A builtin name that collides with an existing public/private skill must produce one deterministic builtin row, and new drafts/approvals with that name must fail with SkillConflict.
- A search_and_load_skills query that would match a builtin body must return no builtin hit and must not load the body.
- With the Git runtime or database unavailable, builtin rows must still appear in the prompt/API/sandbox, while POST /api/skills/reload keeps its existing 503 behavior but clears the builtin cache.

---

### Task 1: Config, enums, and directory

**Files:**
- Modify: backend/packages/harness/agentkit/config/skills_config.py
- Modify: backend/packages/harness/agentkit/config/reload_boundary.py
- Modify: backend/packages/harness/agentkit/skills/types.py
- Modify: backend/packages/harness/agentkit/skills/domain.py
- Create: skills/builtin/.gitkeep
- Test: backend/tests/test_builtin_skills.py

**Interfaces:**
- Produces: BuiltinSkillsConfig with enabled: bool and path: str, plus root_dir() -> Path.
- Produces: SkillsConfig.builtin: BuiltinSkillsConfig.
- Produces: SkillCategory.BUILTIN = "builtin" and SkillScope.BUILTIN = "builtin".

- [ ] **Step 1: Write the failing test**

    from agentkit.config.runtime_paths import resolve_path
    from agentkit.config.skills_config import SkillsConfig
    from agentkit.skills.domain import SkillScope
    from agentkit.skills.types import SkillCategory

    def test_builtin_config_defaults_and_enum_values():
        config = SkillsConfig()
        assert config.builtin.enabled is True
        assert config.builtin.path == "skills/builtin"
        assert config.builtin.root_dir() == resolve_path("skills/builtin")
        assert SkillScope.BUILTIN == "builtin"
        assert SkillCategory.BUILTIN == "builtin"

- [ ] **Step 2: Run test to verify it fails**

Run: uv run python -m pytest backend/tests/test_builtin_skills.py::test_builtin_config_defaults_and_enum_values -v
Expected: FAIL because BuiltinSkillsConfig and the enum values do not exist.

- [ ] **Step 3: Implement config types**

Add DEFAULT_BUILTIN_SKILLS_PATH = "skills/builtin". Add BuiltinSkillsConfig with model_config = ConfigDict(extra="forbid"), enabled=True, path=DEFAULT_BUILTIN_SKILLS_PATH, and root_dir() using resolve_path. Add builtin to SkillsConfig with default_factory.

Add startup-only descriptions for skills.builtin.path and skills.builtin.enabled in reload_boundary.py. Add SkillCategory.BUILTIN and SkillScope.BUILTIN. Create skills/builtin/.gitkeep.

- [ ] **Step 4: Run test to verify it passes**

Run: uv run python -m pytest backend/tests/test_builtin_skills.py::test_builtin_config_defaults_and_enum_values -v
Expected: PASS.

- [ ] **Step 5: Commit**

    git add backend/packages/harness/agentkit/config/skills_config.py backend/packages/harness/agentkit/config/reload_boundary.py backend/packages/harness/agentkit/skills/types.py backend/packages/harness/agentkit/skills/domain.py skills/builtin/.gitkeep backend/tests/test_builtin_skills.py
    git commit -m "feat(skills): add builtin skill config and types"

---

### Task 2: Shared skill-directory scanner

**Files:**
- Create: backend/packages/harness/agentkit/skills/scan.py
- Modify: backend/packages/harness/agentkit/skills/storage.py
- Test: backend/tests/test_skill_scan.py

**Interfaces:**
- Produces: scan_skill_category(category_root: Path, *, category: SkillCategory, container_path: str, scope: SkillScope | None = None, enabled: bool = False, allow_symlink_dirs: bool = True) -> list[Skill].
- Consumes: existing parse_skill_file, Skill, SkillCategory, SkillScope.
- The function returns [] for a missing root, skips bad names and bad frontmatter, and stops at a package's SKILL.md boundary.

- [ ] **Step 1: Write the failing test**

    import os
    from pathlib import Path
    import pytest

    from agentkit.skills.domain import SkillScope
    from agentkit.skills.scan import scan_skill_category
    from agentkit.skills.types import SkillCategory
    from fakes.skill_tree import symlinks_available, write_skill

    def test_scan_skill_category_stops_at_package_boundary(tmp_path: Path):
        write_skill(tmp_path, "builtin", "one", description="one")
        write_skill(tmp_path, "builtin", "one", dir_name="one/examples")

        skills = scan_skill_category(
            tmp_path / "skills" / "builtin",
            category=SkillCategory.BUILTIN,
            container_path="/mnt/skills",
            scope=SkillScope.BUILTIN,
            enabled=True,
            allow_symlink_dirs=False,
        )

        assert [skill.name for skill in skills] == ["one"]
        assert skills[0].skill_file == tmp_path / "skills" / "builtin" / "one" / "SKILL.md"

    def test_scan_skill_category_rejects_a_bad_directory_name(tmp_path: Path):
        write_skill(tmp_path, "builtin", "bad name", dir_name="bad name")
        skills = scan_skill_category(
            tmp_path / "skills" / "builtin",
            category=SkillCategory.BUILTIN,
            container_path="/mnt/skills",
        )
        assert skills == []

    def test_scan_skill_category_rejects_symlinked_dirs_when_disallowed(tmp_path: Path):
        if not symlinks_available(tmp_path):
            pytest.skip("symlinks unavailable")
        target = tmp_path / "outside"
        target.mkdir()
        (target / "SKILL.md").write_text(
            "---\nname: linked\ndescription: linked\n---\n# body\n",
            encoding="utf-8",
        )
        root = tmp_path / "skills" / "builtin"
        root.mkdir(parents=True)
        os.symlink(target, root / "linked", target_is_directory=True)

        assert scan_skill_category(
            root,
            category=SkillCategory.BUILTIN,
            container_path="/mnt/skills",
            allow_symlink_dirs=False,
        ) == []

- [ ] **Step 2: Run test to verify it fails**

Run: uv run python -m pytest backend/tests/test_skill_scan.py -v
Expected: FAIL because agentkit.skills.scan does not exist.

- [ ] **Step 3: Implement the shared scanner**

Move the directory-name regex, entry usability check, and within-root check into scan.py. Implement scan_skill_category with the exact signature above. For allow_symlink_dirs=False, reject both symlinked skill directories and symlinked SKILL.md files. Parse frontmatter with parse_skill_file; a directory name mismatch logs a warning and uses the frontmatter name. Duplicate names in one category keep the last sorted directory and log a warning.

Refactor storage.load_skills to call scan_skill_category for public and custom, preserving custom-over-public override behavior. Apply the enabled map with dataclasses.replace(skill, enabled=enabled_map.get(skill.name, True)) after scanning. Keep validate_skill_file_path in storage.py and import the shared within-root helper from scan.py.

- [ ] **Step 4: Run tests to verify they pass**

Run: uv run python -m pytest backend/tests/test_skill_scan.py backend/tests/test_skill_loading.py backend/tests/test_skill_scope_layers.py -v
Expected: all PASS.

- [ ] **Step 5: Commit**

    git add backend/packages/harness/agentkit/skills/scan.py backend/packages/harness/agentkit/skills/storage.py backend/tests/test_skill_scan.py
    git commit -m "refactor(skills): share the skill directory scanner"

---

### Task 3: Builtin loader and cache

**Files:**
- Create: backend/packages/harness/agentkit/skills/builtin.py
- Test: backend/tests/test_builtin_skills.py

**Interfaces:**
- Produces: load_builtin_skills(config: SkillsConfig) -> tuple[Skill, ...].
- Produces: clear_builtin_cache() -> None.
- Produces: builtin_names(config: SkillsConfig) -> frozenset[str].
- The loader uses scan_skill_category(..., category=SkillCategory.BUILTIN, scope=SkillScope.BUILTIN, enabled=True, allow_symlink_dirs=False).

- [ ] **Step 1: Write the failing tests**

    import os
    from pathlib import Path

    import pytest

    from agentkit.config.skills_config import SkillsConfig
    from agentkit.skills.builtin import builtin_names, clear_builtin_cache, load_builtin_skills
    from agentkit.skills.domain import SkillScope
    from agentkit.skills.types import SkillCategory
    from fakes.skill_tree import skills_config, write_skill

    @pytest.fixture(autouse=True)
    def _clear_builtin_cache():
        clear_builtin_cache()
        yield
        clear_builtin_cache()

    def test_builtin_loader_sets_scope_and_container_path(tmp_path: Path):
        write_skill(tmp_path, "builtin", "demo", description="Demo skill")
        config = skills_config(tmp_path, builtin={"path": str(tmp_path / "skills" / "builtin")})

        skills = load_builtin_skills(config)

        assert [skill.name for skill in skills] == ["demo"]
        assert skills[0].scope is SkillScope.BUILTIN
        assert skills[0].category is SkillCategory.BUILTIN
        assert skills[0].enabled is True
        assert skills[0].container_file == "/mnt/skills/builtin/demo/SKILL.md"
        assert builtin_names(config) == frozenset({"demo"})

    def test_builtin_loader_is_disabled_by_config(tmp_path: Path):
        write_skill(tmp_path, "builtin", "demo")
        config = skills_config(tmp_path, builtin={"enabled": False, "path": str(tmp_path / "skills" / "builtin")})
        assert load_builtin_skills(config) == ()

    def test_builtin_loader_warns_once_for_a_missing_root(tmp_path: Path, caplog):
        config = skills_config(tmp_path, builtin={"path": str(tmp_path / "missing")})
        with caplog.at_level("WARNING", logger="agentkit.skills.builtin"):
            assert load_builtin_skills(config) == ()
            assert load_builtin_skills(config) == ()
        assert sum("built-in skills directory" in record.message for record in caplog.records) == 1

    def test_missing_root_recovers_after_the_directory_appears(tmp_path: Path):
        config = skills_config(
            tmp_path, builtin={"path": str(tmp_path / "skills" / "builtin")}
        )
        assert load_builtin_skills(config) == ()
        write_skill(tmp_path, "builtin", "demo")
        assert [skill.name for skill in load_builtin_skills(config)] == ["demo"]

    def test_builtin_cache_invalidates_when_skill_md_changes(tmp_path: Path):
        write_skill(tmp_path, "builtin", "demo", description="first")
        config = skills_config(tmp_path, builtin={"path": str(tmp_path / "skills" / "builtin")})
        first = load_builtin_skills(config)
        skill_file = tmp_path / "skills" / "builtin" / "demo" / "SKILL.md"
        skill_file.write_text("---
name: demo
description: second
---
# second
", encoding="utf-8")
        future = 2_000_000_000_000_000_000
        os.utime(skill_file, ns=(future, future))

        second = load_builtin_skills(config)

        assert first[0].description == "first"
        assert second[0].description == "second"

- [ ] **Step 2: Run tests to verify they fail**

Run: uv run python -m pytest backend/tests/test_builtin_skills.py -k "loader or cache or missing" -v
Expected: FAIL because agentkit.skills.builtin does not exist.

- [ ] **Step 3: Implement the loader and cache**

Implement a module-level lock and a cache keyed by resolved root. The cache value is (fingerprint, tuple of Skill). The fingerprint is a sorted tuple of (directory name, SKILL.md st_mtime_ns, st_size) for direct child directories. Load only when enabled and the root exists. A missing root logs the warning "built-in skills directory is missing: <path>" once per root; clear_builtin_cache() resets both the cache and the warned-roots set.

Implement builtin_names as a frozenset of names from load_builtin_skills. Keep all scanning in scan_skill_category; builtin.py must not parse frontmatter itself.

- [ ] **Step 4: Run tests to verify they pass**

Run: uv run python -m pytest backend/tests/test_builtin_skills.py -v
Expected: all PASS.

- [ ] **Step 5: Commit**

    git add backend/packages/harness/agentkit/skills/builtin.py backend/tests/test_builtin_skills.py
    git commit -m "feat(skills): load project built-in skills"

---

### Task 4: Registry overlay, prompt index, and always-on behavior

**Files:**
- Modify: backend/packages/harness/agentkit/skills/storage.py
- Modify: backend/packages/harness/agentkit/skills/prefs.py
- Modify: backend/packages/harness/agentkit/skills/prompt.py
- Test: backend/tests/test_skill_index.py
- Test: backend/tests/test_skill_activation_request_only.py
- Test: backend/tests/test_skill_progressive.py
- Modify: backend/tests/fakes/skill_tree.py

**Interfaces:**
- Produces: SkillRegistry.builtin_skills: tuple[Skill, ...] | None = None.
- Produces: SkillRegistry.visible(*, include_builtin: bool = True) -> list[Skill].
- Produces: SkillRegistry.load() and find() include builtins; builtins are ordered before catalog rows.
- Produces: SKILL_FILESYSTEM_BUILTIN_ROOT_CONVENTION and SKILL_CATALOG_BUILTIN_ROOT_CONVENTION.
- effective_enabled(skill, prefs) returns True for scope builtin without reading prefs.

- [ ] **Step 1: Write the failing tests**

    def test_builtin_is_visible_even_when_user_pref_is_false(tmp_path):
        write_skill(tmp_path, "builtin", "demo")
        config = skills_config(tmp_path, builtin={"path": str(tmp_path / "skills" / "builtin")})
        prefs = tmp_path / "skills_prefs.json"
        prefs.write_text('{"u1": {"demo": false}}', encoding="utf-8")

        registry = SkillRegistry(config=config, user_id="u1", prefs_path=prefs)

        assert [skill.name for skill in registry.visible()] == ["demo"]
        assert registry.visible(include_builtin=False) == []

    def test_builtin_wins_a_name_collision(tmp_path):
        write_skill(tmp_path, "public", "demo", body="public body")
        write_skill(tmp_path, "builtin", "demo", body="builtin body")
        config = skills_config(tmp_path, builtin={"path": str(tmp_path / "skills" / "builtin")})

        visible = SkillRegistry(config=config).visible()

        assert [skill.name for skill in visible] == ["demo"]
        assert visible[0].scope is SkillScope.BUILTIN

    def test_builtin_respects_the_agent_allowlist(tmp_path):
        write_skill(tmp_path, "builtin", "demo")
        config = skills_config(tmp_path, builtin={"path": str(tmp_path / "skills" / "builtin")})
        registry = SkillRegistry(config=config, available_skills=frozenset({"other"}))
        assert registry.visible() == []

    def test_search_visibility_keeps_a_same_name_catalog_row(tmp_path):
        write_skill(tmp_path, "public", "demo", body="public body")
        write_skill(tmp_path, "builtin", "demo", body="builtin body")
        config = skills_config(tmp_path, builtin={"path": str(tmp_path / "skills" / "builtin")})
        registry = SkillRegistry(config=config)

        search_view = registry.visible(include_builtin=False)

        assert [skill.name for skill in search_view] == ["demo"]
        assert search_view[0].scope is None
        assert search_view[0].category is SkillCategory.PUBLIC

    def test_builtin_prompt_index_marks_builtin_and_names_its_root(tmp_path):
        write_skill(tmp_path, "builtin", "demo", description="Demo skill")
        config = skills_config(tmp_path, builtin={"path": str(tmp_path / "skills" / "builtin")})
        section = build_skills_section(config=config)
        assert "- demo [builtin]: Demo skill" in section
        assert "内置技能在" in section
        assert "/mnt/skills/builtin/<name>/SKILL.md" in section
        assert "SECRET-BODY" not in section

    def test_filesystem_convention_is_unchanged_without_builtins(tmp_path):
        write_skill(tmp_path, "public", "public-one")
        config = skills_config(tmp_path)
        section = build_skills_section(config=config)
        assert "category 是 public 或 custom" in section
        assert "private" not in section

    def test_git_convention_with_builtin_names_all_three_roots(tmp_path):
        write_skill(tmp_path, "builtin", "demo")
        config = skills_config(
            tmp_path,
            source="git",
            builtin={"path": str(tmp_path / "skills" / "builtin")},
        )
        registry = SkillRegistry(
            config=config,
            builtin_skills=load_builtin_skills(config),
            snapshot=SkillSnapshot("e", (), {}),
        )
        section = build_skills_section(config=config, registry=registry)
        assert "private" in section
        assert "builtin" in section
        assert "/mnt/skills/private/<name>/SKILL.md" in section
        assert "/mnt/skills/builtin/<name>/SKILL.md" in section

Add from agentkit.skills.builtin import load_builtin_skills to the test_skill_index.py imports before running these tests.

- [ ] **Step 2: Run tests to verify they fail**

Run: uv run python -m pytest backend/tests/test_skill_index.py -k builtin -v
Expected: FAIL because builtins are not merged and the prompt has no builtin convention.

- [ ] **Step 3: Implement the overlay and isolate test fixtures**

First update backend/tests/fakes/skill_tree.py: skills_config() must add builtin={"enabled": False} by default, and write_app_config() must write data["skills"]["builtin"] = {"enabled": False} before applying explicit overrides. This keeps temp-root tests from loading the repository's real skills/builtin tree; tests that need builtins pass builtin={"path": ...} explicitly.

Run the existing filesystem tests once after the fixture change to confirm they still pass:

    uv run python -m pytest backend/tests/test_skill_loading.py backend/tests/test_skill_scope_layers.py backend/tests/test_skill_index.py -v

In storage.py, add builtin_skills to SkillRegistry. Implement _builtin_skills() using the injected tuple or load_builtin_skills(self.config). Implement a merge helper that puts builtins first, filters builtins by available_skills only, and drops catalog rows whose names collide with a builtin (warning). visible() accepts include_builtin and applies the merge after existing catalog filtering. If include_builtin=False, return the catalog results unchanged: do not load builtins and do not hide same-name catalog rows; the search tool must still be able to find a public skill that happens to share a builtin name. If catalog/snapshot loading raises, log it and keep the builtin rows when include_builtin=True (the fail-closed behavior applies to the catalog only); with include_builtin=False the result is empty. load() includes builtins. find() and index_by_container_file() continue to call visible().

In prefs.py, return True for builtin before the global/personal checks.

In prompt.py, branch _root_convention on source plus actual scope: source=git uses the catalog text; source=filesystem with an injected public/private snapshot keeps the catalog text (existing tests depend on this; if builtins are also present, use catalog+builtin); source=filesystem with only builtin uses the filesystem+builtin text; source=filesystem with neither keeps the old text. Keep SKILL_ROOT_CONVENTION for filesystem without builtins. Add SKILL_FILESYSTEM_BUILTIN_ROOT_CONVENTION with this exact wording:

    技能根目录 {root}：公共技能在 {root}/public/<name>/SKILL.md，自定义技能在 {root}/custom/<name>/SKILL.md，内置技能在 {root}/builtin/<name>/SKILL.md；目录名就是技能名，同名时 custom 覆盖 public。

Keep the existing catalog public/private text when no builtin exists; add SKILL_CATALOG_BUILTIN_ROOT_CONVENTION that names public, private, and builtin roots in the same style. _entry_marker already uses scope and will emit [builtin].

Add slash and progressive-loading coverage:
- In test_skill_activation_request_only.py: write a builtin with body marker, build middleware with a builtin-aware config, assert /demo injects the body and records /mnt/skills/builtin/demo/SKILL.md as the slash source.
- In test_skill_progressive.py: assert the builtin body is absent from build_skills_section, read_skill_body returns it, and no search runtime is involved.

- [ ] **Step 4: Run tests to verify they pass**

Run: uv run python -m pytest backend/tests/test_skill_index.py backend/tests/test_skill_activation_request_only.py backend/tests/test_skill_progressive.py backend/tests/test_skill_loading.py backend/tests/test_skill_catalog_snapshot.py -v
Expected: all PASS.

- [ ] **Step 5: Commit**

    git add backend/packages/harness/agentkit/skills/storage.py backend/packages/harness/agentkit/skills/prefs.py backend/packages/harness/agentkit/skills/prompt.py backend/tests/fakes/skill_tree.py backend/tests/test_skill_index.py backend/tests/test_skill_activation_request_only.py backend/tests/test_skill_progressive.py
    git commit -m "feat(skills): overlay builtin skills in the registry and prompt"

---

### Task 5: Read-only sandbox mount

**Files:**
- Modify: backend/packages/harness/agentkit/sandbox/local/local_sandbox_provider.py
- Test: backend/tests/test_builtin_mounts.py

**Interfaces:**
- Produces: _builtin_skills_dir() -> Path | None, using config.skills.builtin.enabled and root_dir().
- Produces: _static_mappings(..., builtin_dir: Path | None = None).
- The provider adds PathMapping("<container_path>/builtin", str(builtin_dir), read_only=True) in both git and filesystem source modes.

- [ ] **Step 1: Write the failing test**

    from pathlib import Path
    from types import SimpleNamespace

    import pytest

    from agentkit.config.app_config import AppConfig
    from agentkit.config.skills_config import SkillsConfig
    from agentkit.sandbox.local import local_sandbox_provider as provider_module
    from agentkit.sandbox.local.local_sandbox_provider import LocalSandboxProvider
    from agentkit.sandbox.security import assert_writable, find_path_mapping, resolve_sandbox_path
    from fakes.skill_tree import write_skill

    def test_builtin_tree_is_mounted_read_only(tmp_path: Path, monkeypatch):
        builtin_dir = tmp_path / "skills" / "builtin"
        write_skill(tmp_path, "builtin", "demo", body="body")
        config = AppConfig(skills=SkillsConfig(source="git", builtin={"path": str(builtin_dir)}))
        monkeypatch.setattr(provider_module, "get_app_config", lambda: config)
        provider = LocalSandboxProvider(cache_dir=tmp_path / "cache")
        sandbox = provider.get(provider.acquire("thread-1", user_id="u1"))

        mapping = find_path_mapping("/mnt/skills/builtin", sandbox.path_mappings)
        assert mapping is not None
        assert mapping.local_path == str(builtin_dir.resolve())
        assert mapping.read_only is True
        assert resolve_sandbox_path("/mnt/skills/builtin/demo/SKILL.md", sandbox.path_mappings) == (builtin_dir / "demo" / "SKILL.md").resolve()
        with pytest.raises(PermissionError):
            assert_writable("/mnt/skills/builtin/demo/SKILL.md", sandbox.path_mappings)

- [ ] **Step 2: Run test to verify it fails**

Run: uv run python -m pytest backend/tests/test_builtin_mounts.py -v
Expected: FAIL because no builtin mapping exists.

- [ ] **Step 3: Implement the mount**

Add _builtin_skills_dir near the existing _skills_tree_root helper. In LocalSandboxProvider.__init__, resolve it once and pass it to _static_mappings. Add the exact mapping in both source branches and include the resolved builtin path in the operator-mount protected list. Keep the existing behavior when builtin is disabled or the config is unreadable.

- [ ] **Step 4: Run tests to verify they pass**

Run: uv run python -m pytest backend/tests/test_builtin_mounts.py backend/tests/test_skill_private_mounts.py -v
Expected: all PASS.

- [ ] **Step 5: Commit**

    git add backend/packages/harness/agentkit/sandbox/local/local_sandbox_provider.py backend/tests/test_builtin_mounts.py
    git commit -m "feat(sandbox): mount built-in skills read-only"

---

### Task 6: API list, response, and reload

**Files:**
- Modify: backend/app/gateway/models/response.py
- Modify: backend/app/gateway/skills_catalog.py
- Test: backend/tests/test_skills_catalog_routes.py

**Interfaces:**
- Produces: SkillResponse.category accepts "builtin"; SkillResponse.scope accepts "builtin".
- Produces: builtin_skill_responses() -> list[SkillResponse].
- Produces: merge_builtin_responses(builtin: list[SkillResponse], catalog: list[SkillResponse]) -> list[SkillResponse], builtin rows first and canonical by name.
- list_skills_for_api returns builtin rows even when the Git runtime is None.
- reload_skills calls clear_builtin_cache() first; clear_skill_prompt_cache() runs exactly once per path: before the 503 when runtime is missing, after a successful refresh otherwise.

- [ ] **Step 1: Write the failing tests**

    def test_builtin_rows_are_listed_without_a_git_runtime(tmp_path, monkeypatch):
        _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"}, builtin={"path": str(tmp_path / "skills" / "builtin")})
        write_skill(tmp_path, "builtin", "demo", description="Demo skill")
        client = _client(monkeypatch, runtime=None)

        response = client.get("/api/skills")

        assert response.status_code == 200
        row = next(item for item in response.json()["skills"] if item["name"] == "demo")
        assert row["scope"] == "builtin"
        assert row["category"] == "builtin"
        assert row["status"] == "builtin"
        assert row["enabled"] is True
        assert row["global_enabled"] is None
        assert row["container_file"] == "/mnt/skills/builtin/demo/SKILL.md"

    def test_builtin_rows_are_listed_before_catalog_rows(tmp_path, monkeypatch):
        _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"}, builtin={"path": str(tmp_path / "skills" / "builtin")})
        write_skill(tmp_path, "builtin", "demo")
        runtime = _runtime(_snapshot())
        set_skill_snapshot(runtime.catalog.snapshot)
        client = _client(monkeypatch, runtime=runtime)

        names = [item["name"] for item in client.get("/api/skills").json()["skills"]]
        assert names[0] == "demo"
        assert set(names[1:]) == {"shared", "alice-tools"}

    def test_builtin_hides_a_same_name_catalog_row(tmp_path, monkeypatch):
        _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"}, builtin={"path": str(tmp_path / "skills" / "builtin")})
        write_skill(tmp_path, "builtin", "shared")
        runtime = _runtime(_snapshot())
        set_skill_snapshot(runtime.catalog.snapshot)
        client = _client(monkeypatch, runtime=runtime)

        rows = [
            item
            for item in client.get("/api/skills").json()["skills"]
            if item["name"] == "shared"
        ]

        assert len(rows) == 1
        assert rows[0]["scope"] == "builtin"

    def test_reload_clears_builtin_cache_before_runtime_check(tmp_path, monkeypatch):
        _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"}, builtin={"path": str(tmp_path / "skills" / "builtin")})
        cleared = []
        monkeypatch.setattr(skills_catalog, "clear_builtin_cache", lambda: cleared.append("builtin"))
        client = _client(monkeypatch, runtime=None)

        response = client.post("/api/skills/reload")

        assert response.status_code == 503
        assert cleared == ["builtin"]

- [ ] **Step 2: Run tests to verify they fail**

Run: uv run python -m pytest backend/tests/test_skills_catalog_routes.py -k builtin -v
Expected: FAIL because the response literals reject builtin and the list fails closed.

- [ ] **Step 3: Implement the API changes**

Add "builtin" to the SkillResponse category and scope literals. In skills_catalog.py, load builtins from get_app_config().skills, convert them to SkillResponse rows with enabled=True, global_enabled=None, status="builtin", and container_file from the Skill, and merge them before catalog rows. Filter catalog rows whose name collides with a builtin. Do the merge in both the filesystem branch and the Git branch; when runtime is None in Git mode, return builtin rows instead of an empty list.

Add "_status_for" handling for builtin. In reload_skills, call clear_builtin_cache() at the top. For filesystem, keep reload_skill_caches() (it clears the prompt cache). For Git with runtime None, clear_skill_prompt_cache() once and then raise SkillRuntimeUnavailable, preserving the existing 503. For Git with a runtime, refresh, bump, then clear_skill_prompt_cache() once as today. Do not clear the prompt cache twice on the success path.

- [ ] **Step 4: Run tests to verify they pass**

Run: uv run python -m pytest backend/tests/test_skills_catalog_routes.py backend/tests/test_skill_runtime_wiring.py -v
Expected: all PASS.

- [ ] **Step 5: Commit**

    git add backend/app/gateway/models/response.py backend/app/gateway/skills_catalog.py backend/tests/test_skills_catalog_routes.py
    git commit -m "feat(api): list built-in skills and clear their cache on reload"

---

### Task 7: Reserve builtin names for authoring and approval

**Files:**
- Modify: backend/packages/harness/agentkit/skills/author.py
- Modify: backend/packages/harness/agentkit/skills/review.py
- Modify: backend/app/gateway/skills_catalog.py
- Test: backend/tests/test_skill_author_service.py
- Test: backend/tests/test_skill_review_service.py
- Test: backend/tests/test_skill_runtime_wiring.py

**Interfaces:**
- Produces: SkillAuthorService.__init__(..., reserved_names: Callable[[], frozenset[str]] | None = None).
- Produces: SkillReviewService.__init__(..., reserved_names: Callable[[], frozenset[str]] | None = None).
- create_draft raises SkillConflict("name is reserved by a built-in skill") when the name is reserved.
- approve raises the same SkillConflict when the public name is reserved.
- build_skill_runtime passes lambda: builtin_names(skills) to both services.

- [ ] **Step 1: Write the failing tests**

    def test_create_draft_rejects_a_builtin_name(harness):
        service = SkillAuthorService(
            harness.store,
            harness.git,
            FakeContent(),
            harness.creation,
            reserved_names=lambda: frozenset({"demo"}),
        )
        with pytest.raises(SkillConflict, match="built-in"):
            harness.run(
                lambda: service.create_draft(
                    owner_user_id="u1", name="demo", description="demo"
                )
            )

    def test_approve_rejects_a_reserved_public_name(harness):
        async def scenario():
            _, _, submission = await _submitted(harness, name="demo")
            review = SkillReviewService(
                harness.store,
                harness.git,
                FakeContent(),
                harness.review_config,
                reserved_names=lambda: frozenset({"demo"}),
            )
            with pytest.raises(SkillConflict, match="built-in"):
                await review.approve(
                    admin_user_id="admin", submission_id=submission.id
                )

        harness.run(scenario)

- [ ] **Step 2: Run tests to verify they fail**

Run: uv run python -m pytest backend/tests/test_skill_author_service.py backend/tests/test_skill_review_service.py -k builtin -v
Expected: FAIL because the constructors do not accept reserved_names.

- [ ] **Step 3: Implement the reservation checks**

Add the optional keyword-only reserved_names arguments to both constructors and store the callable. In SkillAuthorService.create_draft, check before store writes. In SkillReviewService._ensure_public_name_free, check before the database query. In build_skill_runtime, pass the same callable to both services.

Update the fake Author and Review constructors in backend/tests/test_skill_runtime_wiring.py to accept reserved_names: Any = None; otherwise build_skill_runtime's new keyword argument breaks that test's fakes.

- [ ] **Step 4: Run tests to verify they pass**

Run: uv run python -m pytest backend/tests/test_skill_author_service.py backend/tests/test_skill_review_service.py backend/tests/test_skill_runtime_wiring.py -v
Expected: all PASS.

- [ ] **Step 5: Commit**

    git add backend/packages/harness/agentkit/skills/author.py backend/packages/harness/agentkit/skills/review.py backend/app/gateway/skills_catalog.py backend/tests/test_skill_author_service.py backend/tests/test_skill_review_service.py backend/tests/test_skill_runtime_wiring.py
    git commit -m "feat(skills): reserve built-in skill names"

---

### Task 8: Exclude builtins from search_and_load_skills

**Files:**
- Modify: backend/packages/harness/agentkit/tools/builtin/search_and_load_skills.py
- Test: backend/tests/test_search_and_load_skills_tool.py

**Interfaces:**
- Consumes: SkillRegistry.visible(include_builtin=False).
- Produces: filesystem-mode search candidates are public/custom only; Git mode is unchanged.

- [ ] **Step 1: Write the failing test**

    def test_builtin_body_is_not_returned_by_search(tmp_path):
        _tree(tmp_path)
        config = SkillsConfig(
            path=str(tmp_path / "skills"),
            builtin={"path": str(tmp_path / "skills" / "builtin")},
        )
        write_skill(tmp_path, "builtin", "builtin-searchable", description="PostgreSQL 多租户 search_path 串库排查", body="# builtin secret\nBODY-MARKER")
        _install(config)

        payload = _call()

        names = [item["name"] for item in payload["skills"]]
        assert "builtin-searchable" not in names
        assert all("BODY-MARKER" not in item["body"] for item in payload["skills"])

- [ ] **Step 2: Run test to verify it fails**

Run: uv run python -m pytest backend/tests/test_search_and_load_skills_tool.py::test_builtin_body_is_not_returned_by_search -v
Expected: FAIL because registry.visible() currently includes the builtin and loads its body.

- [ ] **Step 3: Implement the exclusion**

In search_and_load_skills.py, change the filesystem branch from registry.visible() to registry.visible(include_builtin=False). Do not add builtins to SkillSearchService and do not change router scoring, budgets, or Milvus code.

- [ ] **Step 4: Run tests to verify they pass**

Run: uv run python -m pytest backend/tests/test_search_and_load_skills_tool.py -v
Expected: all PASS.

- [ ] **Step 5: Commit**

    git add backend/packages/harness/agentkit/tools/builtin/search_and_load_skills.py backend/tests/test_search_and_load_skills_tool.py
    git commit -m "feat(skills): keep built-ins out of skill search"

---

### Task 9: Frontend types and read-only UI

**Files:**
- Modify: frontend/src/core/api/skills.ts
- Modify: frontend/src/components/skills/my-skills-page.tsx
- Modify: frontend/src/components/skills/skill-status-badge.tsx
- Test: frontend/src/components/skills/my-skills-page.dom.test.tsx

**Interfaces:**
- Produces: SkillScope includes "builtin"; SkillStatus includes "builtin".
- Produces: the skills page renders a "内置技能" section before "公共技能"; builtin rows have no edit button and no toggle.
- Builtin list keys use skill.skill_id || skill.name.

- [ ] **Step 1: Write the failing test**

    it("内置技能单独回显且没有编辑入口", async () => {
      mockedListMine.mockResolvedValue([]);
      mockedListCatalog.mockResolvedValue([
        makeSkill({
          skill_id: "builtin-demo",
          name: "代码规范",
          description: "内置规范",
          scope: "builtin",
          status: "builtin",
          owner_user_id: null,
        }),
        makeSkill({
          skill_id: "pub-1",
          name: "公开技能",
          scope: "public",
          status: "approved",
          owner_user_id: "u-alice",
        }),
      ]);

      renderPage();

      expect(await screen.findByText("内置技能")).toBeTruthy();
      const builtinRow = screen.getByText("代码规范").closest("li");
      expect(builtinRow?.textContent).toContain("内置");
      expect(builtinRow?.textContent).toContain("内置规范");
      expect(builtinRow?.querySelector("button")).toBeNull();

      const publicRow = screen.getByText("公开技能").closest("li");
      expect(publicRow?.querySelector("button")?.textContent).toContain("编辑");
    });

- [ ] **Step 2: Run test to verify it fails**

Run: cd frontend && pnpm vitest run src/components/skills/my-skills-page.dom.test.tsx
Expected: FAIL because builtin is not in the TypeScript scope and no builtin section exists.

- [ ] **Step 3: Implement the frontend**

Add builtin to SkillScope and SkillStatus. In my-skills-page.tsx, split catalog into builtins and publicSkills, render a "内置技能" section with ScopeTag "内置" and no edit action, and use key={skill.skill_id || skill.name}. Keep the public section behavior unchanged. Add the builtin label/tone to SkillStatusBadge if the badge is rendered.

- [ ] **Step 4: Run test to verify it passes**

Run: cd frontend && pnpm vitest run src/components/skills/my-skills-page.dom.test.tsx
Expected: PASS.

- [ ] **Step 5: Commit**

    git add frontend/src/core/api/skills.ts frontend/src/components/skills/my-skills-page.tsx frontend/src/components/skills/skill-status-badge.tsx frontend/src/components/skills/my-skills-page.dom.test.tsx
    git commit -m "feat(web): show built-in skills read-only"

---

### Task 10: Config, docs, and full verification

**Files:**
- Modify: config.example.yaml
- Modify: config.yaml
- Modify: backend/README.md
- Test: full targeted suites

**Interfaces:**
- Produces: a documented skills.builtin block:
    skills:
      builtin:
        enabled: true
        path: skills/builtin
- Produces: README guidance: add skills/builtin/<name>/SKILL.md, always indexed, read_file or slash only, no search, no personal toggle, UI read-only.

- [ ] **Step 1: Add the config block**

Add the block beside skills.source in config.example.yaml and config.yaml without changing unrelated settings. Keep the comments short and mention that path and enabled are startup-only.

- [ ] **Step 2: Document the behavior**

Update backend/README.md's skills section: builtin directory, progressive loading, no search_and_load_skills, no personal toggle, UI read-only, read-only sandbox mount.

- [ ] **Step 3: Run the backend focused suite**

Run: uv run python -m pytest backend/tests/test_builtin_skills.py backend/tests/test_skill_scan.py backend/tests/test_skill_index.py backend/tests/test_skill_activation_request_only.py backend/tests/test_skill_progressive.py backend/tests/test_builtin_mounts.py backend/tests/test_skills_catalog_routes.py backend/tests/test_skill_author_service.py backend/tests/test_skill_review_service.py backend/tests/test_skill_runtime_wiring.py backend/tests/test_search_and_load_skills_tool.py backend/tests/test_skill_loading.py backend/tests/test_skill_scope_layers.py backend/tests/test_skill_catalog_snapshot.py -v
Expected: all PASS.

- [ ] **Step 4: Run the frontend test**

Run: cd frontend && pnpm vitest run src/components/skills/my-skills-page.dom.test.tsx
Expected: PASS.

- [ ] **Step 5: Commit**

    git add config.example.yaml config.yaml backend/README.md
    git commit -m "docs(skills): document built-in skill configuration"

---

## Self-Review

- Spec coverage: config/types (Task 1), scanner/loader (Tasks 2-3), always-on prompt/registry/slash (Task 4), read-only mount (Task 5), API/UI (Tasks 6 and 9), reserved names (Task 7), no-search boundary (Task 8), docs/verification (Task 10).
- Type consistency: SkillScope.BUILTIN, SkillCategory.BUILTIN, BuiltinSkillsConfig, load_builtin_skills, clear_builtin_cache, builtin_names, and visible(include_builtin=...) are defined once and reused.
- Review Focus: missing root (Task 3), stale prefs (Task 4), name collision (Tasks 4 and 7), search exclusion (Task 8), runtime/database outage (Task 6), symlink/path read-only (Tasks 2 and 5).
- No database migration or Git-catalog changes are planned.

"""Task 15: catalog list, personal preference, global toggle, and reload.

These routes are the integration point between PostgreSQL/Git (through the
SkillRuntime built by the app lifespan) and the process-wide catalog snapshot.
The tests drive the HTTP surface with a fake runtime, so no live Milvus,
PostgreSQL, or Git repository is required.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentkit.config import get_app_config
from agentkit.config.skills_prefs import read_user_skill_prefs
from agentkit.sandbox.local.local_sandbox_provider import LocalSandboxProvider
from agentkit.sandbox.security import find_path_mapping
from agentkit.skills.catalog import SkillSnapshot, set_skill_snapshot
from agentkit.skills.domain import SkillRecord, SkillScope
from agentkit.skills.prompt import build_skills_section
from agentkit.skills.storage import SkillRegistry
from agentkit.skills.types import Skill, SkillCategory
from backend.app.gateway import deps
from backend.app.gateway.app import install_error_handlers, install_middleware
from backend.app.gateway.auth import AuthSettings
from backend.app.gateway.routers import skill_drafts, skills
from backend.app.gateway.run_models import CallerKind, RunCaller
from backend.app.gateway import skills_catalog
from backend.app.gateway.skills_catalog import (
    SkillRuntime,
    configure_skill_runtime,
    reset_skill_runtime,
)
from fakes.skill_tree import write_app_config, write_skill

READ = frozenset({"skills:read"})
WRITE = frozenset({"skills:write"})
ADMIN = frozenset({"admin", "skills:read"})
PUBLIC_COMMIT = "a" * 40
PRIVATE_COMMIT = "b" * 40


# ---------------------------------------------------------------------------
# fake runtime: the real one is built by the lifespan (test_skill_runtime_wiring)
# ---------------------------------------------------------------------------


@dataclass
class FakeStore:
    active: list[Any] = field(default_factory=list)
    settings_calls: list[tuple[str, bool, str]] = field(default_factory=list)
    outbox_events: list[tuple[str, str]] = field(default_factory=list)
    pending: dict[str, Any] = field(default_factory=dict)
    catalog: "FakeCatalog | None" = None

    async def all_active(self) -> list[Any]:
        return list(self.active)

    async def set_global_enabled(self, skill_id: str, enabled: bool, actor: str) -> None:
        self.settings_calls.append((skill_id, enabled, actor))
        self.outbox_events.append(("upsert_active", f"public:{skill_id}"))
        assert self.catalog is not None
        snapshot = self.catalog.snapshot
        public = tuple(
            replace(skill, enabled=enabled) if skill.skill_id == skill_id else skill
            for skill in snapshot.public
        )
        self.catalog.snapshot = replace(snapshot, epoch=f"{snapshot.epoch}+1", public=public)

    async def pending_submission(self, skill_id: str) -> Any:
        return self.pending.get(skill_id)


@dataclass
class FakeCatalog:
    snapshot: SkillSnapshot
    refresh_calls: int = 0

    async def refresh(self) -> SkillSnapshot:
        self.refresh_calls += 1
        set_skill_snapshot(self.snapshot)
        return self.snapshot


@dataclass
class FakeContent:
    public_calls: list[list[Any]] = field(default_factory=list)
    private_calls: list[tuple[str, list[Any]]] = field(default_factory=list)

    async def materialize_public(self, active: list[Any]) -> Path:
        self.public_calls.append(list(active))
        return Path("active/public")

    async def materialize_private(self, owner: str, active: list[Any]) -> Path:
        self.private_calls.append((owner, list(active)))
        return Path("active/users") / owner


def _runtime(snapshot: SkillSnapshot, *, active: list[Any] | None = None) -> SkillRuntime:
    store = FakeStore(active=list(active or ()))
    catalog = FakeCatalog(snapshot)
    store.catalog = catalog
    return SkillRuntime(
        store=store,
        git=None,
        content=FakeContent(),
        author=None,
        review=None,
        search=None,
        catalog=catalog,
    )


# ---------------------------------------------------------------------------
# snapshot rows
# ---------------------------------------------------------------------------


def _skill(
    skill_id: str,
    name: str,
    scope: SkillScope,
    owner: str | None,
    *,
    skill_version: str = "v1",
    version_id: str | None = None,
    commit: str = PUBLIC_COMMIT,
    path: str | None = None,
    enabled: bool = True,
    container_path: str = "/mnt/skills",
) -> Skill:
    return Skill(
        name=name,
        description=f"{name} description",
        license=None,
        skill_dir=Path("cache") / skill_id,
        skill_file=Path("cache") / skill_id / "SKILL.md",
        relative_path=Path(name),
        category=SkillCategory.CUSTOM,
        enabled=enabled,
        skill_id=skill_id,
        scope=scope,
        owner_user_id=owner,
        version_id=version_id or f"ver-{skill_id}",
        skill_version=skill_version,
        git_commit=commit,
        repo_path=path or f"public/{name}/SKILL.md",
        container_path=container_path,
    )


PUBLIC = _skill(
    "s-pub", "shared", SkillScope.PUBLIC, "author-id", skill_version="v3", path="public/shared/SKILL.md"
)
ALICE_PRIVATE = _skill(
    "s-alice",
    "alice-tools",
    SkillScope.PRIVATE,
    "alice",
    commit=PRIVATE_COMMIT,
    path="users/alice/s-alice/SKILL.md",
)
BOB_PRIVATE = _skill(
    "s-bob",
    "bob-tools",
    SkillScope.PRIVATE,
    "bob",
    commit="c" * 40,
    path="users/bob/s-bob/SKILL.md",
)


def _snapshot(
    *,
    public: tuple[Skill, ...] | None = None,
    container_path: str = "/mnt/skills",
) -> SkillSnapshot:
    def at_root(skill: Skill) -> Skill:
        return replace(skill, container_path=container_path)

    return SkillSnapshot(
        epoch="epoch-1",
        public=tuple(at_root(skill) for skill in (public if public is not None else (PUBLIC,))),
        private_by_owner={
            "alice": (at_root(ALICE_PRIVATE),),
            "bob": (at_root(BOB_PRIVATE),),
        },
    )


# ---------------------------------------------------------------------------
# app/client plumbing
# ---------------------------------------------------------------------------


class _Identity(dict):
    """One client can switch users/scopes between requests."""

    def __init__(self, scopes: frozenset[str] | None, user_id: str | None) -> None:
        super().__init__(scopes=scopes, user_id=user_id)

    def as_(self, scopes: frozenset[str] | None, *, user_id: str | None = None) -> "_Identity":
        self["scopes"] = scopes
        self["user_id"] = user_id
        return self


def _client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    scopes: frozenset[str] | None = READ,
    user_id: str | None = "alice",
    runtime: SkillRuntime | None = None,
    author: Any | None = None,
) -> TestClient:
    application = FastAPI(redirect_slashes=False)
    install_middleware(application, auth_settings=AuthSettings.disabled())
    install_error_handlers(application)
    if author is not None:
        application.include_router(skill_drafts.router, prefix="/api")
        application.state.skill_author_service = author
    application.include_router(skills.router, prefix="/api")
    application.state.skill_runtime = runtime
    # 私有 container_file 是沙箱 provider 的能力，不是环境事实：钉住它，测试不再
    # 依赖本机配置或执行顺序。
    monkeypatch.setattr(skills_catalog, "_private_mounts_supported", lambda: True)
    identity = _Identity(scopes, user_id)
    monkeypatch.setattr(
        deps,
        "caller_from_request",
        lambda request: RunCaller(
            kind=CallerKind.HTTP,
            user_id=identity["user_id"],
            scopes=identity["scopes"],
        ),
    )
    client = TestClient(application, raise_server_exceptions=False)
    client.identity = identity  # type: ignore[attr-defined]
    return client


def _configure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **skills_overrides: Any) -> Path:
    """Write one config.yaml and point the process at it; returns the config path."""

    monkeypatch.setenv("MYFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("MYFLOW_SKILLS_PREFS_PATH", str(tmp_path / "skills_prefs.json"))
    monkeypatch.delenv("MYFLOW_EXTENSIONS_CONFIG_PATH", raising=False)
    path = write_app_config(tmp_path, **skills_overrides)
    monkeypatch.setenv("MYFLOW_CONFIG_PATH", str(path))
    return path


@pytest.fixture(autouse=True)
def _reset_runtime_registry():
    reset_skill_runtime()
    yield
    reset_skill_runtime()


# ---------------------------------------------------------------------------
# list: public + caller private from the snapshot
# ---------------------------------------------------------------------------


def test_alice_sees_public_and_her_private_but_not_bobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"})
    runtime = _runtime(_snapshot())
    configure_skill_runtime(runtime)
    set_skill_snapshot(runtime.catalog.snapshot)
    client = _client(monkeypatch, runtime=runtime)

    alice = client.get("/api/skills")
    bob = client.get("/api/skills")
    assert alice.status_code == bob.status_code == 200
    assert {item["name"] for item in alice.json()["skills"]} == {"shared", "alice-tools"}

    client.identity.as_(READ, user_id="bob")
    bob = client.get("/api/skills")
    assert {item["name"] for item in bob.json()["skills"]} == {"shared", "bob-tools"}


def test_list_rows_carry_catalog_fields_and_hide_foreign_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"})
    runtime = _runtime(_snapshot())
    set_skill_snapshot(runtime.catalog.snapshot)
    client = _client(monkeypatch, runtime=runtime)

    rows = {item["name"]: item for item in client.get("/api/skills").json()["skills"]}

    public = rows["shared"]
    assert public["skill_id"] == "s-pub"
    assert public["scope"] == "public"
    assert public["skill_version"] == "v3"
    assert public["version_id"] == "ver-s-pub"
    assert public["git_commit"] == PUBLIC_COMMIT
    assert public["skill_path"] == "public/shared/SKILL.md"
    assert public["status"] == "approved"
    assert public["owner_user_id"] is None  # owned by author-id, not the caller
    assert public["global_enabled"] is None  # only with ?scope=global
    assert public["container_file"] == "/mnt/skills/public/shared/SKILL.md"

    private = rows["alice-tools"]
    assert private["skill_id"] == "s-alice"
    assert private["scope"] == "private"
    assert private["owner_user_id"] == "alice"
    assert private["status"] == "draft"
    assert private["git_commit"] == PRIVATE_COMMIT
    assert private["skill_path"] == "users/alice/s-alice/SKILL.md"
    assert private["container_file"] == "/mnt/skills/private/alice-tools/SKILL.md"


def test_admin_scope_global_sees_owner_and_global_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"})
    runtime = _runtime(_snapshot())
    set_skill_snapshot(runtime.catalog.snapshot)
    client = _client(monkeypatch, scopes=ADMIN, user_id="root", runtime=runtime)

    rows = {item["name"]: item for item in client.get("/api/skills?scope=global").json()["skills"]}

    assert rows["shared"]["global_enabled"] is True
    assert rows["shared"]["owner_user_id"] == "author-id"


def test_scope_global_still_requires_admin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"})
    client = _client(monkeypatch, runtime=_runtime(_snapshot()))

    response = client.get("/api/skills?scope=global")

    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"


# ---------------------------------------------------------------------------
# personal preference
# ---------------------------------------------------------------------------


def test_personal_preference_is_keyed_by_skill_id_and_isolated_per_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"})
    runtime = _runtime(_snapshot())
    set_skill_snapshot(runtime.catalog.snapshot)
    client = _client(monkeypatch, runtime=runtime)

    response = client.put("/api/skills/s-alice/preference", json={"enabled": False})
    assert response.status_code == 200
    body = response.json()
    assert body["skill_id"] == "s-alice"
    assert body["enabled"] is False

    prefs_path = tmp_path / "skills_prefs.json"
    assert json.loads(prefs_path.read_text(encoding="utf-8")) == {"alice": {"s-alice": False}}
    assert read_user_skill_prefs("alice") == {"s-alice": False}
    assert read_user_skill_prefs("bob") == {}

    alice_rows = {item["name"]: item for item in client.get("/api/skills").json()["skills"]}
    assert alice_rows["alice-tools"]["enabled"] is False
    assert alice_rows["shared"]["enabled"] is True

    client.identity.as_(READ, user_id="bob")
    bob_rows = {item["name"]: item for item in client.get("/api/skills").json()["skills"]}
    assert bob_rows["bob-tools"]["enabled"] is True
    assert read_user_skill_prefs("bob") == {}


def test_public_skill_preference_does_not_leak_to_another_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"})
    runtime = _runtime(_snapshot())
    set_skill_snapshot(runtime.catalog.snapshot)
    client = _client(monkeypatch, runtime=runtime)

    assert client.put("/api/skills/s-pub/preference", json={"enabled": False}).status_code == 200

    client.identity.as_(READ, user_id="bob")
    rows = {item["name"]: item for item in client.get("/api/skills").json()["skills"]}
    assert rows["shared"]["enabled"] is True


def test_preference_of_an_unknown_skill_is_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"})
    runtime = _runtime(_snapshot())
    set_skill_snapshot(runtime.catalog.snapshot)
    client = _client(monkeypatch, runtime=runtime)

    response = client.put("/api/skills/ghost/preference", json={"enabled": False})

    assert response.status_code == 404
    assert response.json()["code"] == "skill_not_found"
    assert not (tmp_path / "skills_prefs.json").exists()


def test_preference_needs_the_enabled_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"})
    client = _client(monkeypatch, runtime=_runtime(_snapshot()))

    response = client.put("/api/skills/s-pub/preference", json={})

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"


# ---------------------------------------------------------------------------
# global toggle
# ---------------------------------------------------------------------------


def test_global_toggle_writes_pg_and_outbox_not_extensions_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"})
    extensions = tmp_path / "extensions_config.json"
    extensions.write_text(
        json.dumps({"skills": {"shared": {"enabled": True}}}), encoding="utf-8"
    )
    monkeypatch.setenv("MYFLOW_EXTENSIONS_CONFIG_PATH", str(extensions))
    before = extensions.read_text(encoding="utf-8")

    runtime = _runtime(_snapshot())
    set_skill_snapshot(runtime.catalog.snapshot)
    cleared: list[str] = []
    monkeypatch.setattr(
        skills_catalog, "clear_skill_prompt_cache", lambda: cleared.append("clear")
    )
    client = _client(monkeypatch, scopes=ADMIN, user_id="root", runtime=runtime)

    response = client.patch("/api/skills/s-pub", json={"enabled": True})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["skill_id"] == "s-pub"
    assert body["global_enabled"] is True
    assert runtime.store.settings_calls == [("s-pub", True, "root")]
    assert runtime.store.outbox_events == [("upsert_active", "public:s-pub")]
    assert runtime.catalog.refresh_calls == 1
    assert cleared == ["clear"]
    assert extensions.read_text(encoding="utf-8") == before


def test_global_toggle_disables_the_skill_for_everyone_but_not_private_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"})
    runtime = _runtime(_snapshot())
    set_skill_snapshot(runtime.catalog.snapshot)
    client = _client(monkeypatch, scopes=ADMIN, user_id="root", runtime=runtime)

    response = client.patch("/api/skills/s-pub", json={"enabled": False})

    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is False
    assert response.json()["global_enabled"] is False

    client.identity.as_(READ, user_id="alice")
    rows = {item["name"]: item for item in client.get("/api/skills").json()["skills"]}
    assert rows["shared"]["enabled"] is False
    assert rows["alice-tools"]["enabled"] is True


def test_global_toggle_requires_admin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"})
    runtime = _runtime(_snapshot())
    set_skill_snapshot(runtime.catalog.snapshot)
    client = _client(monkeypatch, runtime=runtime)

    response = client.patch("/api/skills/s-pub", json={"enabled": False})

    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"
    assert runtime.store.settings_calls == []


def test_global_toggle_of_a_private_skill_is_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Private skills are not affected by the global switch (spec 5.7)."""

    _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"})
    runtime = _runtime(_snapshot())
    set_skill_snapshot(runtime.catalog.snapshot)
    client = _client(monkeypatch, scopes=ADMIN, user_id="root", runtime=runtime)

    response = client.patch("/api/skills/s-alice", json={"enabled": False})

    assert response.status_code == 404
    assert response.json()["code"] == "skill_not_found"
    assert runtime.store.settings_calls == []


# ---------------------------------------------------------------------------
# reload
# ---------------------------------------------------------------------------


def test_reload_refreshes_the_snapshot_and_clears_the_prompt_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"})
    runtime = _runtime(_snapshot())
    set_skill_snapshot(runtime.catalog.snapshot)
    cleared: list[str] = []
    monkeypatch.setattr(
        skills_catalog, "clear_skill_prompt_cache", lambda: cleared.append("clear")
    )
    client = _client(monkeypatch, scopes=ADMIN, user_id="root", runtime=runtime)
    assert "shared" in {item["name"] for item in client.get("/api/skills").json()["skills"]}

    renamed = _skill("s-pub", "renamed", SkillScope.PUBLIC, "author-id", path="public/renamed/SKILL.md")
    runtime.catalog.snapshot = SkillSnapshot(epoch="epoch-2", public=(renamed,), private_by_owner={})
    response = client.post("/api/skills/reload")

    assert response.status_code == 200
    assert response.json() == {"reloaded": True}
    assert runtime.catalog.refresh_calls == 1
    assert cleared == ["clear"]
    assert {item["name"] for item in client.get("/api/skills").json()["skills"]} == {"renamed"}


def test_reload_requires_admin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"})
    runtime = _runtime(_snapshot())
    client = _client(monkeypatch, runtime=runtime)

    response = client.post("/api/skills/reload")

    assert response.status_code == 403
    assert runtime.catalog.refresh_calls == 0


# ---------------------------------------------------------------------------
# fail closed: git mode never scans disk
# ---------------------------------------------------------------------------


def test_git_mode_with_an_empty_snapshot_lists_nothing_even_when_disk_has_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"})
    write_skill(tmp_path, "public", "disk-skill")
    runtime = _runtime(SkillSnapshot(epoch="", public=(), private_by_owner={}))
    client = _client(monkeypatch, runtime=runtime)

    response = client.get("/api/skills")

    assert response.status_code == 200
    assert response.json() == {"skills": []}


def test_git_mode_without_a_runtime_does_not_fall_back_to_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"})
    write_skill(tmp_path, "public", "disk-skill")
    set_skill_snapshot(SkillSnapshot(epoch="", public=(), private_by_owner={}))
    client = _client(monkeypatch, runtime=None)

    assert client.get("/api/skills").json() == {"skills": []}


def test_filesystem_source_keeps_the_legacy_scanner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch)
    write_skill(tmp_path, "public", "legacy-tool", description="Legacy tool")
    client = _client(monkeypatch, runtime=None)

    response = client.get("/api/skills")

    assert response.status_code == 200
    rows = response.json()["skills"]
    assert [item["name"] for item in rows] == ["legacy-tool"]
    assert rows[0]["category"] == "public"
    assert rows[0]["enabled"] is True
    assert rows[0]["skill_id"] is None
    assert rows[0]["status"] is None


# ---------------------------------------------------------------------------
# configured container path
# ---------------------------------------------------------------------------


def test_container_file_uses_the_configured_container_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(
        tmp_path,
        monkeypatch,
        source="git",
        git={"repository": "https://example.test/s.git"},
        container_path="/opt/skills",
    )
    runtime = _runtime(_snapshot(container_path="/opt/skills"))
    set_skill_snapshot(runtime.catalog.snapshot)
    client = _client(monkeypatch, runtime=runtime)

    rows = {item["name"]: item for item in client.get("/api/skills").json()["skills"]}

    assert rows["shared"]["container_file"] == "/opt/skills/public/shared/SKILL.md"
    assert rows["alice-tools"]["container_file"] == "/opt/skills/private/alice-tools/SKILL.md"

    config = get_app_config().skills
    registry = SkillRegistry(
        config=config, user_id="alice", snapshot=runtime.catalog.snapshot
    )
    prompt = build_skills_section(config=config, registry=registry)
    assert "/opt/skills/public" in prompt
    assert "/opt/skills/private" in prompt

    provider = LocalSandboxProvider(cache_dir=tmp_path / "cache")
    sandbox = provider.get(provider.acquire("thread-a", user_id="alice"))
    assert sandbox is not None
    public = find_path_mapping("/opt/skills/public", sandbox.path_mappings)
    private = find_path_mapping("/opt/skills/private", sandbox.path_mappings)
    assert public is not None and Path(public.local_path).parts[-2:] == ("active", "public")
    assert private is not None and Path(private.local_path).parts[-3:] == (
        "active",
        "users",
        "alice",
    )
    # The row's container_file resolves under the exact same mount.
    row = rows["alice-tools"]["container_file"]
    assert find_path_mapping(row.rsplit("/SKILL.md", 1)[0], sandbox.path_mappings) is private


def test_default_container_path_still_yields_mnt_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"})
    runtime = _runtime(_snapshot())
    set_skill_snapshot(runtime.catalog.snapshot)
    client = _client(monkeypatch, runtime=runtime)

    rows = {item["name"]: item for item in client.get("/api/skills").json()["skills"]}

    assert rows["shared"]["container_file"] == "/mnt/skills/public/shared/SKILL.md"
    assert rows["alice-tools"]["container_file"] == "/mnt/skills/private/alice-tools/SKILL.md"
    provider = LocalSandboxProvider(cache_dir=tmp_path / "cache")
    sandbox = provider.get(provider.acquire("thread-a", user_id="alice"))
    assert sandbox is not None
    assert find_path_mapping("/mnt/skills/public", sandbox.path_mappings) is not None
    assert find_path_mapping("/mnt/skills/private", sandbox.path_mappings) is not None


@dataclass
class _AuthorStub:
    async def create_draft(
        self, *, owner_user_id: str, name: str, description: str
    ) -> SkillRecord:
        now = datetime.now(UTC)
        return SkillRecord(
            id="skill-1",
            owner_user_id=owner_user_id,
            private_name=name,
            public_name=None,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )


def test_draft_route_container_file_uses_the_configured_container_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(
        tmp_path,
        monkeypatch,
        source="git",
        container_path="/opt/skills",
        git={"repository": "https://example.test/s.git"},
    )
    client = _client(monkeypatch, scopes=WRITE, author=_AuthorStub())

    response = client.post("/api/skills", json={"name": "alice-tools", "description": "Alice"})

    assert response.status_code == 201, response.text
    assert response.json()["container_file"] == "/opt/skills/private/alice-tools/SKILL.md"

# ---------------------------------------------------------------------------
# fail closed without a runtime: stale snapshots are not an answer (Fix 6)
# ---------------------------------------------------------------------------


def test_git_mode_without_a_runtime_lists_nothing_even_with_a_live_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A populated snapshot is stale state, not catalog truth: fail closed."""
    _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"})
    set_skill_snapshot(_snapshot())
    client = _client(monkeypatch, runtime=None)

    response = client.get("/api/skills")

    assert response.status_code == 200
    assert response.json() == {"skills": []}


def test_git_mode_without_a_runtime_refuses_to_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reporting success without refreshing anything is the bug: fail with 503."""
    _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"})
    set_skill_snapshot(_snapshot())
    client = _client(monkeypatch, scopes=ADMIN, user_id="root", runtime=None)

    response = client.post("/api/skills/reload")

    assert response.status_code == 503
    assert response.json()["code"] == "skill_runtime_unavailable"
    assert response.json().get("reloaded") is not True


# ---------------------------------------------------------------------------
# mutation refresh helper (Fix 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_skill_runtime_noops_for_filesystem_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(skills_catalog, "_skill_source", lambda: "filesystem")
    monkeypatch.setattr(
        skills_catalog,
        "get_skill_runtime",
        lambda: (_ for _ in ()).throw(AssertionError("runtime must not be read")),
    )

    await skills_catalog.refresh_skill_runtime()


@pytest.mark.asyncio
async def test_refresh_skill_runtime_refreshes_and_clears_prompt_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(SkillSnapshot("e", (), {}))
    cleared: list[str] = []
    monkeypatch.setattr(skills_catalog, "_skill_source", lambda: "git")
    monkeypatch.setattr(skills_catalog, "get_skill_runtime", lambda: runtime)
    monkeypatch.setattr(
        skills_catalog, "clear_skill_prompt_cache", lambda: cleared.append("cleared")
    )

    await skills_catalog.refresh_skill_runtime()

    assert runtime.catalog.refresh_calls == 1
    assert cleared == ["cleared"]


@pytest.mark.asyncio
async def test_refresh_skill_runtime_logs_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class BrokenCatalog:
        async def refresh(self) -> SkillSnapshot:
            raise RuntimeError("snapshot database is gone")

    runtime = _runtime(SkillSnapshot("e", (), {}))
    runtime.catalog = BrokenCatalog()
    monkeypatch.setattr(skills_catalog, "_skill_source", lambda: "git")
    monkeypatch.setattr(skills_catalog, "get_skill_runtime", lambda: runtime)
    caplog.set_level(logging.WARNING, logger="backend.app.gateway.skills_catalog")

    await skills_catalog.refresh_skill_runtime()

    assert "refresh failed" in caplog.text

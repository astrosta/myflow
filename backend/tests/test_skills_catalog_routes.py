"""Catalog list, personal preference, reload, and fail-closed runtime behavior."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentkit.config import get_app_config
from agentkit.sandbox.local.local_sandbox_provider import LocalSandboxProvider
from agentkit.sandbox.security import find_path_mapping
from agentkit.skills.catalog import SkillSnapshot, set_skill_snapshot
from agentkit.skills.domain import SkillConflict, SkillRecord, SkillScope
from agentkit.skills.prompt import build_skills_section
from agentkit.skills.storage import SkillRegistry
from agentkit.skills.types import Skill, SkillCategory
from backend.app.gateway import deps, skills_catalog
from backend.app.gateway.app import install_error_handlers, install_middleware
from backend.app.gateway.auth import AuthSettings
from backend.app.gateway.routers import skill_drafts, skills
from backend.app.gateway.run_models import CallerKind, RunCaller
from backend.app.gateway.skills_catalog import SkillRuntime
from fakes.skill_tree import write_app_config, write_skill

READ = frozenset({"skills:read"})
WRITE = frozenset({"skills:write"})
ADMIN = frozenset({"admin", "skills:read"})
PRIVATE_COMMIT = "b" * 40


@dataclass
class FakeStore:
    active: list[Any] = field(default_factory=list)
    catalog: "FakeCatalog | None" = None
    epoch_bumps: int = 0

    async def bump_catalog_epoch(self) -> int:
        self.epoch_bumps += 1
        return self.epoch_bumps

    async def all_active(self) -> list[Any]:
        return list(self.active)


@dataclass
class FakeCatalog:
    snapshot: SkillSnapshot
    refresh_calls: int = 0

    async def refresh(self) -> SkillSnapshot:
        self.refresh_calls += 1
        set_skill_snapshot(self.snapshot)
        return self.snapshot


@dataclass
class FakeAuthor:
    runtime: SkillRuntime

    async def set_enabled(
        self,
        *,
        owner_user_id: str,
        skill_id: str,
        enabled: bool,
        index_enabled: bool = True,
    ) -> None:
        del index_enabled
        snapshot = self.runtime.catalog.snapshot
        found = False
        updated: dict[str, tuple[Skill, ...]] = {}
        for owner, skills in snapshot.private_by_owner.items():
            rows: list[Skill] = []
            for skill in skills:
                if owner == owner_user_id and skill.skill_id == skill_id:
                    found = True
                    rows.append(replace(skill, enabled=bool(enabled)))
                else:
                    rows.append(skill)
            updated[owner] = tuple(rows)
        if not found:
            raise SkillConflict(f"skill {skill_id!r} does not exist")
        self.runtime.catalog.snapshot = SkillSnapshot(
            epoch=snapshot.epoch + "-toggled",
            private_by_owner=updated,
        )
        set_skill_snapshot(self.runtime.catalog.snapshot)


@dataclass
class FakeContent:
    private_calls: list[tuple[str, list[Any]]] = field(default_factory=list)

    async def materialize_private(self, owner: str, active: list[Any]) -> Path:
        self.private_calls.append((owner, list(active)))
        return Path("active/users") / owner

    def existing_private_owners(self) -> list[str]:
        return []


def _runtime(snapshot: SkillSnapshot) -> SkillRuntime:
    store = FakeStore()
    catalog = FakeCatalog(snapshot)
    store.catalog = catalog
    return SkillRuntime(
        store=store,
        git=None,
        content=FakeContent(),
        author=None,
        search=None,
        catalog=catalog,
    )


def _skill(
    skill_id: str,
    name: str,
    owner: str,
    *,
    skill_version: str = "v1",
    version_id: str | None = None,
    commit: str = PRIVATE_COMMIT,
    path: str | None = None,
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
        enabled=True,
        skill_id=skill_id,
        scope=SkillScope.PRIVATE,
        owner_user_id=owner,
        version_id=version_id or f"ver-{skill_id}",
        skill_version=skill_version,
        git_commit=commit,
        repo_path=path or f"users/{owner}/{skill_id}/SKILL.md",
        container_path=container_path,
    )


ALICE_SKILL = _skill("s-alice", "alice-tools", "alice")
BOB_SKILL = _skill("s-bob", "bob-tools", "bob", commit="c" * 40)


def _snapshot(*, container_path: str = "/mnt/skills") -> SkillSnapshot:
    def at_root(skill: Skill) -> Skill:
        return replace(skill, container_path=container_path)

    return SkillSnapshot(
        epoch="epoch-1",
        private_by_owner={
            "alice": (at_root(ALICE_SKILL),),
            "bob": (at_root(BOB_SKILL),),
        },
    )


class _Identity(dict):
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
    monkeypatch.setenv("MYFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("MYFLOW_SKILLS_PREFS_PATH", str(tmp_path / "skills_prefs.json"))
    monkeypatch.delenv("MYFLOW_EXTENSIONS_CONFIG_PATH", raising=False)
    path = write_app_config(tmp_path, **skills_overrides)
    monkeypatch.setenv("MYFLOW_CONFIG_PATH", str(path))
    return path


@pytest.fixture(autouse=True)
def _clean_runtime(monkeypatch: pytest.MonkeyPatch):
    skills_catalog.reset_skill_runtime()
    yield
    skills_catalog.reset_skill_runtime()


def test_alice_sees_only_her_active_skill_and_builtin_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"})
    runtime = _runtime(_snapshot())
    set_skill_snapshot(runtime.catalog.snapshot)
    client = _client(monkeypatch, runtime=runtime)

    rows = client.get("/api/skills").json()["skills"]

    assert [row["name"] for row in rows] == ["alice-tools"]
    assert rows[0]["skill_id"] == "s-alice"
    assert rows[0]["scope"] == "private"
    assert rows[0]["status"] == "active"
    assert rows[0]["owner_user_id"] == "alice"

    bob = _client(monkeypatch, runtime=runtime, user_id="bob")
    assert [row["name"] for row in bob.get("/api/skills").json()["skills"]] == [
        "bob-tools"
    ]


def test_catalog_rows_carry_container_paths_and_no_foreign_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"})
    runtime = _runtime(_snapshot())
    set_skill_snapshot(runtime.catalog.snapshot)
    client = _client(monkeypatch, runtime=runtime)

    rows = client.get("/api/skills").json()["skills"]
    row = rows[0]

    assert row["container_file"] == "/mnt/skills/private/alice-tools/SKILL.md"
    assert row["version_id"] == "ver-s-alice"
    assert row["git_commit"] == PRIVATE_COMMIT
    assert row["skill_path"] == "users/alice/s-alice/SKILL.md"


def test_db_preference_is_isolated_per_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"})
    runtime = _runtime(_snapshot())
    runtime.author = FakeAuthor(runtime)
    set_skill_snapshot(runtime.catalog.snapshot)
    client = _client(monkeypatch, runtime=runtime, scopes=READ | WRITE)

    response = client.put("/api/skills/s-alice/preference", json={"enabled": False})
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is False

    bob = _client(
        monkeypatch, runtime=runtime, user_id="bob", scopes=READ | WRITE
    )
    bob_response = bob.put("/api/skills/s-bob/preference", json={"enabled": True})
    assert bob_response.status_code == 200
    assert bob_response.json()["enabled"] is True

    alice = _client(monkeypatch, runtime=runtime, user_id="alice")
    row = alice.get("/api/skills").json()["skills"][0]
    assert row["name"] == "alice-tools"
    assert row["enabled"] is False


def test_preference_rejects_unknown_skill_and_missing_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"})
    runtime = _runtime(_snapshot())
    runtime.author = FakeAuthor(runtime)
    set_skill_snapshot(runtime.catalog.snapshot)
    client = _client(monkeypatch, runtime=runtime, scopes=READ | WRITE)

    unknown = client.put("/api/skills/s-bob/preference", json={"enabled": False})
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "skill_not_found"

    missing = client.put("/api/skills/s-alice/preference", json={})
    assert missing.status_code == 422


def test_reload_requires_admin_and_refreshes_the_private_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"})
    runtime = _runtime(_snapshot())
    set_skill_snapshot(runtime.catalog.snapshot)
    client = _client(monkeypatch, runtime=runtime, scopes=READ, user_id="alice")

    assert client.post("/api/skills/reload").status_code == 403

    admin = _client(monkeypatch, runtime=runtime, scopes=ADMIN, user_id="root")
    assert admin.post("/api/skills/reload").status_code == 200
    assert runtime.catalog.refresh_calls == 1
    assert runtime.store.epoch_bumps == 1


def test_empty_snapshot_never_falls_back_to_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"})
    write_skill(tmp_path, "public", "disk-skill")
    runtime = _runtime(SkillSnapshot(epoch=""))
    set_skill_snapshot(runtime.catalog.snapshot)
    client = _client(monkeypatch, runtime=runtime)

    assert client.get("/api/skills").json() == {"skills": []}


def test_git_mode_without_a_runtime_fails_closed_and_refuses_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"})
    set_skill_snapshot(_snapshot())
    client = _client(monkeypatch, runtime=None)

    assert client.get("/api/skills").json() == {"skills": []}

    admin = _client(monkeypatch, runtime=None, scopes=ADMIN, user_id="root")
    response = admin.post("/api/skills/reload")
    assert response.status_code == 503
    assert response.json()["code"] == "skill_runtime_unavailable"


def test_builtin_rows_are_merged_and_hide_a_same_name_catalog_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_skill(tmp_path, "builtin", "alice-tools", description="Builtin")
    _configure(
        tmp_path,
        monkeypatch,
        source="git",
        builtin={"path": str(tmp_path / "skills" / "builtin")},
        git={"repository": "https://example.test/s.git"},
    )
    runtime = _runtime(_snapshot())
    set_skill_snapshot(runtime.catalog.snapshot)
    client = _client(monkeypatch, runtime=runtime)

    rows = client.get("/api/skills").json()["skills"]
    assert [row["name"] for row in rows] == ["alice-tools"]
    assert rows[0]["category"] == "builtin"
    assert rows[0]["scope"] == "builtin"


def test_filesystem_source_keeps_the_legacy_public_scanner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_skill(tmp_path, "public", "legacy-tool", description="Legacy tool")
    _configure(tmp_path, monkeypatch, source="filesystem", builtin={"enabled": False})
    client = _client(monkeypatch)

    response = client.get("/api/skills")

    rows = response.json()["skills"]
    assert [item["name"] for item in rows] == ["legacy-tool"]
    assert rows[0]["category"] == "public"
    assert rows[0]["enabled"] is True
    assert rows[0]["skill_id"] is None


def test_container_file_uses_the_configured_private_root(
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
    assert rows["alice-tools"]["container_file"] == "/opt/skills/private/alice-tools/SKILL.md"

    config = get_app_config().skills
    registry = SkillRegistry(
        config=config, user_id="alice", snapshot=runtime.catalog.snapshot
    )
    prompt = build_skills_section(config=config, registry=registry)
    assert "/opt/skills/private" in prompt
    assert "/opt/skills/public" not in prompt

    provider = LocalSandboxProvider(cache_dir=tmp_path / "cache")
    sandbox = provider.get(provider.acquire("thread-a", user_id="alice"))
    assert sandbox is not None
    assert find_path_mapping("/opt/skills/public", sandbox.path_mappings) is None
    private = find_path_mapping("/opt/skills/private", sandbox.path_mappings)
    assert private is not None and Path(private.local_path).parts[-3:] == (
        "active",
        "users",
        "alice",
    )


def test_default_container_path_still_yields_mnt_skills_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path, monkeypatch, source="git", git={"repository": "https://example.test/s.git"})
    runtime = _runtime(_snapshot())
    set_skill_snapshot(runtime.catalog.snapshot)
    client = _client(monkeypatch, runtime=runtime)

    row = client.get("/api/skills").json()["skills"][0]
    assert row["container_file"] == "/mnt/skills/private/alice-tools/SKILL.md"


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
    runtime = _runtime(SkillSnapshot("e"))
    cleared: list[str] = []
    monkeypatch.setattr(skills_catalog, "_skill_source", lambda: "git")
    monkeypatch.setattr(skills_catalog, "get_skill_runtime", lambda: runtime)
    monkeypatch.setattr(
        skills_catalog, "clear_skill_prompt_cache", lambda: cleared.append("cleared")
    )

    await skills_catalog.refresh_skill_runtime()

    assert runtime.store.epoch_bumps == 1
    assert runtime.catalog.refresh_calls == 1
    assert cleared == ["cleared"]


@pytest.mark.asyncio
async def test_a_failed_epoch_bump_still_refreshes_locally_and_clears_the_cache(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    runtime = _runtime(SkillSnapshot("e"))

    async def failing_bump() -> int:
        raise RuntimeError("catalog epoch table is unavailable")

    runtime.store.bump_catalog_epoch = failing_bump  # type: ignore[method-assign]
    cleared: list[str] = []
    monkeypatch.setattr(skills_catalog, "_skill_source", lambda: "git")
    monkeypatch.setattr(skills_catalog, "get_skill_runtime", lambda: runtime)
    monkeypatch.setattr(
        skills_catalog, "clear_skill_prompt_cache", lambda: cleared.append("cleared")
    )
    caplog.set_level(logging.WARNING, logger="backend.app.gateway.skills_catalog")

    await skills_catalog.refresh_skill_runtime()

    assert runtime.catalog.refresh_calls == 1
    assert cleared == ["cleared"]
    assert "epoch bump failed" in caplog.text


@pytest.mark.asyncio
async def test_refresh_skill_runtime_logs_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class BrokenCatalog:
        async def refresh(self) -> SkillSnapshot:
            raise RuntimeError("snapshot database is gone")

    runtime = _runtime(SkillSnapshot("e"))
    runtime.catalog = BrokenCatalog()
    monkeypatch.setattr(skills_catalog, "_skill_source", lambda: "git")
    monkeypatch.setattr(skills_catalog, "get_skill_runtime", lambda: runtime)
    caplog.set_level(logging.WARNING, logger="backend.app.gateway.skills_catalog")

    await skills_catalog.refresh_skill_runtime()

    assert "refresh failed" in caplog.text

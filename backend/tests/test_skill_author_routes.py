"""Author HTTP surface: draft save -> scan -> activate, owner isolation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentkit.config.skills_config import SkillCreationConfig
from agentkit.skills.domain import (
    InvalidSkillPackage,
    SecurityScanRecord,
    SkillConflict,
    SkillRecord,
    SkillScanUnavailable,
    SkillVersionRecord,
    StagedFile,
    StagedFileInfo,
)
from agentkit.skills.package import render_skill_md
from backend.app.gateway import deps, skills_catalog
from backend.app.gateway.app import install_error_handlers, install_middleware
from backend.app.gateway.auth import AuthSettings
from backend.app.gateway.routers import skill_drafts, skills
from backend.app.gateway.run_models import CallerKind, RunCaller

WRITE = frozenset({"skills:write"})
READ = frozenset({"skills:read"})


def _now() -> datetime:
    return datetime.now(UTC)


def _info(path: str, content: bytes, mode: str = "text") -> StagedFileInfo:
    return StagedFileInfo(
        path=path,
        size_bytes=len(content),
        mode=mode,
        sha256=hashlib.sha256(content).hexdigest(),
    )


@dataclass
class FakeAuthorService:
    _next: int = 1
    skills: dict[str, SkillRecord] = field(default_factory=dict)
    files: dict[str, list[StagedFile]] = field(default_factory=dict)
    versions: dict[str, list[SkillVersionRecord]] = field(default_factory=dict)
    scans: dict[str, list[SecurityScanRecord]] = field(default_factory=dict)
    active: dict[str, str] = field(default_factory=dict)
    safe_scan: bool = True
    scan_status: str = "completed"

    def _owned(self, owner_user_id: str, skill_id: str) -> SkillRecord:
        skill = self.skills.get(skill_id)
        if skill is None or skill.owner_user_id != owner_user_id or skill.deleted_at is not None:
            raise SkillConflict(f"skill {skill_id!r} does not exist")
        return skill

    async def create_draft(
        self, *, owner_user_id: str, name: str, description: str
    ) -> SkillRecord:
        skill_id = f"skill-{self._next}"
        self._next += 1
        now = _now()
        record = SkillRecord(
            id=skill_id,
            owner_user_id=owner_user_id,
            private_name=name,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        self.skills[skill_id] = record
        self.files[skill_id] = [
            StagedFile(
                path="SKILL.md",
                content=render_skill_md(name, description).encode(),
                mode="text",
            )
        ]
        self.versions[skill_id] = []
        self.scans[skill_id] = []
        return record

    async def list_mine(self, *, owner_user_id: str) -> list[SkillRecord]:
        return [
            skill
            for skill in self.skills.values()
            if skill.owner_user_id == owner_user_id and skill.deleted_at is None
        ]

    async def get_owned_skill(
        self,
        *,
        owner_user_id: str,
        skill_id: str,
        include_deleted: bool = False,
    ) -> SkillRecord | None:
        skill = self.skills.get(skill_id)
        if (
            skill is None
            or skill.owner_user_id != owner_user_id
            or (skill.deleted_at is not None and not include_deleted)
        ):
            return None
        return skill

    async def open_draft(self, *, owner_user_id: str, skill_id: str):
        skill = self._owned(owner_user_id, skill_id)
        if skill.deleted_at is not None:
            self.skills[skill_id] = replace(skill, deleted_at=None, updated_at=_now())
        return [
            _info(file.path, file.content, file.mode)
            for file in self.files[skill_id]
        ]

    async def list_files(self, *, owner_user_id: str, skill_id: str):
        self._owned(owner_user_id, skill_id)
        return [
            _info(file.path, file.content, file.mode)
            for file in self.files[skill_id]
        ]

    async def read_file(self, *, owner_user_id: str, skill_id: str, path: str):
        self._owned(owner_user_id, skill_id)
        return next((file for file in self.files[skill_id] if file.path == path), None)

    async def put_file(
        self,
        *,
        owner_user_id: str,
        skill_id: str,
        path: str,
        content: bytes,
        mode: str = "text",
    ):
        self._owned(owner_user_id, skill_id)
        self.files[skill_id] = [
            file for file in self.files[skill_id] if file.path != path
        ]
        self.files[skill_id].append(StagedFile(path=path, content=content, mode=mode))
        return _info(path, content, mode)

    async def delete_file(self, *, owner_user_id: str, skill_id: str, path: str) -> None:
        self._owned(owner_user_id, skill_id)
        self.files[skill_id] = [
            file for file in self.files[skill_id] if file.path != path
        ]

    async def save(self, *, owner_user_id: str, skill_id: str) -> SkillVersionRecord:
        self._owned(owner_user_id, skill_id)
        seq = len(self.versions[skill_id]) + 1
        version = SkillVersionRecord(
            id=f"version-{seq}",
            skill_id=skill_id,
            skill_version=f"v{seq}",
            version_seq=seq,
            package_sha256="a" * 64,
            skill_md_sha256="b" * 64,
            metadata={"description": "Alice's tools"},
            draft_ref=f"refs/heads/myflow/drafts/{owner_user_id}",
            draft_commit=seq.to_bytes(20, "big").hex(),
            skill_path=f"users/{owner_user_id}/{skill_id}/SKILL.md",
            index_status="unindexed",
            created_at=_now(),
        )
        self.versions[skill_id].append(version)
        return version

    async def list_versions(self, *, owner_user_id: str, skill_id: str):
        self._owned(owner_user_id, skill_id)
        return list(self.versions[skill_id])

    async def latest_version(self, *, owner_user_id: str, skill_id: str):
        self._owned(owner_user_id, skill_id)
        versions = self.versions[skill_id]
        return versions[-1] if versions else None

    async def active_version(self, *, owner_user_id: str, skill_id: str):
        self._owned(owner_user_id, skill_id)
        active_id = self.active.get(skill_id)
        return next(
            (version for version in self.versions[skill_id] if version.id == active_id),
            None,
        )

    async def latest_scan(
        self,
        *,
        owner_user_id: str,
        skill_id: str,
        version_id: str | None = None,
    ):
        self._owned(owner_user_id, skill_id)
        scans = [
            scan
            for scan in self.scans[skill_id]
            if version_id is None or scan.version_id == version_id
        ]
        return scans[-1] if scans else None

    async def list_scans(self, *, owner_user_id: str, skill_id: str, limit: int = 20):
        self._owned(owner_user_id, skill_id)
        return list(reversed(self.scans[skill_id]))[:limit]

    async def scan_security(
        self,
        *,
        owner_user_id: str,
        skill_id: str,
        version_id: str | None = None,
        on_status: Any = None,
    ) -> SecurityScanRecord:
        if on_status is not None:
            on_status("正在做规则检查，并交给模型分析")
        self._owned(owner_user_id, skill_id)
        version = await self.latest_version(owner_user_id=owner_user_id, skill_id=skill_id)
        if version is None:
            raise SkillConflict("no saved version")
        if version_id is not None and version_id != version.id:
            raise SkillConflict("not the latest version")
        scan = SecurityScanRecord(
            id=f"scan-{len(self.scans[skill_id]) + 1}",
            skill_id=skill_id,
            version_id=version.id,
            owner_user_id=owner_user_id,
            package_sha256=version.package_sha256,
            status=self.scan_status,
            is_safe=self.safe_scan,
            max_severity="SAFE" if self.safe_scan else "HIGH",
            findings_count=0 if self.safe_scan else 1,
            findings=[]
            if self.safe_scan
            else [
                {
                    "rule_id": "R1",
                    "title": "Issue",
                    "severity": "HIGH",
                    "category": "prompt_injection",
                    "file_path": "SKILL.md",
                    "line_number": 1,
                    "remediation": "fix",
                }
            ],
            scanner="cisco-ai-skill-scanner",
            scanner_version="2.3.2",
            duration_ms=12,
            error=None if self.scan_status == "completed" else "boom",
            created_at=_now(),
        )
        self.scans[skill_id].append(scan)
        return scan

    async def activate(
        self,
        *,
        owner_user_id: str,
        skill_id: str,
        version_id: str | None = None,
        index_enabled: bool = True,
    ) -> SkillVersionRecord:
        self._owned(owner_user_id, skill_id)
        version = await self.latest_version(owner_user_id=owner_user_id, skill_id=skill_id)
        if version is None:
            raise SkillConflict("no saved version")
        scan = await self.latest_scan(
            owner_user_id=owner_user_id, skill_id=skill_id, version_id=version.id
        )
        if scan is None or scan.status != "completed" or not scan.is_safe:
            raise SkillConflict("security scan did not pass")
        activated = replace(
            version,
            index_status="pending" if index_enabled else "disabled",
            draft_ref=f"refs/heads/myflow/drafts/{owner_user_id}",
            draft_commit="c" * 40,
        )
        self.versions[skill_id][-1] = activated
        self.active[skill_id] = version.id
        return activated

    async def delete_draft(self, *, owner_user_id: str, skill_id: str) -> None:
        skill = self._owned(owner_user_id, skill_id)
        self.active.pop(skill_id, None)
        self.skills[skill_id] = replace(skill, deleted_at=_now(), updated_at=_now())


class _Identity(dict):
    def __init__(self, scopes: frozenset[str] | None, user_id: str | None) -> None:
        super().__init__(scopes=scopes, user_id=user_id)

    def as_(self, scopes: frozenset[str] | None, *, user_id: str | None = None) -> "_Identity":
        self["scopes"] = scopes
        self["user_id"] = user_id
        return self


def _client(
    monkeypatch: pytest.MonkeyPatch,
    service: FakeAuthorService,
    *,
    scopes: frozenset[str] | None = WRITE,
    user_id: str = "alice",
) -> tuple[TestClient, _Identity]:
    application = FastAPI(redirect_slashes=False)
    install_middleware(application, auth_settings=AuthSettings.disabled())
    install_error_handlers(application)
    application.state.skill_author_service = service
    application.include_router(skill_drafts.router, prefix="/api")
    application.include_router(skills.router, prefix="/api")

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
    return TestClient(application, raise_server_exceptions=False), identity


def test_authoring_is_a_503_when_the_git_catalog_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = FastAPI(redirect_slashes=False)
    install_middleware(application, auth_settings=AuthSettings.disabled())
    install_error_handlers(application)
    application.include_router(skill_drafts.router, prefix="/api")
    identity = _Identity(READ | WRITE, "alice")
    monkeypatch.setattr(
        deps,
        "caller_from_request",
        lambda request: RunCaller(
            kind=CallerKind.HTTP,
            user_id=identity["user_id"],
            scopes=identity["scopes"],
        ),
    )
    response = TestClient(application, raise_server_exceptions=False).get("/api/skills/mine")

    assert response.status_code == 503
    assert response.json()["code"] == "skill_runtime_unavailable"


def _create(client: TestClient, name: str = "alice-tools") -> dict[str, Any]:
    response = client.post(
        "/api/skills", json={"name": name, "description": "Alice's tools"}
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_create_is_private_and_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeAuthorService()
    client, identity = _client(monkeypatch, service)

    created = _create(client)

    assert created["id"] == "skill-1"
    assert created["scope"] == "private"
    assert created["owner_user_id"] == "alice"
    assert created["status"] == "draft"
    assert created["enabled"] is True
    assert created["can_activate"] is False
    assert created["container_file"] == "/mnt/skills/private/alice-tools/SKILL.md"

    draft = client.get("/api/skills/skill-1/draft")
    assert draft.status_code == 200
    assert [file["path"] for file in draft.json()["files"]] == ["SKILL.md"]

    identity.as_(WRITE, user_id="bob")
    hidden = client.get("/api/skills/skill-1/draft")
    assert hidden.status_code == 404
    assert hidden.json()["code"] == "skill_not_found"


def test_skill_md_frontmatter_drives_name_and_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeAuthorService()
    client, identity = _client(monkeypatch, service)
    _create(client, "new-skill-abc12345")

    replacement = render_skill_md("daily-report", "整理日报")
    response = client.put(
        "/api/skills/skill-1/draft/file",
        json={"path": "SKILL.md", "content": replacement},
    )
    assert response.status_code == 200, response.text

    draft = client.get("/api/skills/skill-1/draft")
    assert draft.status_code == 200
    assert draft.json()["skill"]["name"] == "daily-report"
    assert draft.json()["skill"]["description"] == "整理日报"

    identity.as_(READ, user_id="alice")
    mine = client.get("/api/skills/mine")
    assert mine.status_code == 200
    assert mine.json()["skills"][0]["name"] == "daily-report"
    assert mine.json()["skills"][0]["description"] == "整理日报"


def test_legacy_pending_index_is_reported_disabled_without_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeAuthorService()
    client, identity = _client(monkeypatch, service)
    _create(client)
    assert client.post("/api/skills/skill-1/draft/save").status_code == 200
    version = service.versions["skill-1"][0]
    service.versions["skill-1"][0] = replace(version, index_status="pending")
    service.active["skill-1"] = version.id

    identity.as_(READ, user_id="alice")
    row = client.get("/api/skills/mine").json()["skills"][0]

    assert row["status"] == "active"
    assert row["index_status"] == "disabled"


def test_mine_lists_only_the_callers_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeAuthorService()
    client, identity = _client(monkeypatch, service)
    alice_skill = _create(client, "alice-tools")

    identity.as_(WRITE, user_id="bob")
    bob_skill = _create(client, "bob-tools")

    identity.as_(READ, user_id="alice")
    mine = client.get("/api/skills/mine")
    assert [item["id"] for item in mine.json()["skills"]] == [alice_skill["id"]]

    identity.as_(READ, user_id="bob")
    mine = client.get("/api/skills/mine")
    assert [item["id"] for item in mine.json()["skills"]] == [bob_skill["id"]]


def test_save_cleans_up_the_skill_commit_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeAuthorService()
    client, _identity = _client(monkeypatch, service, scopes=READ | WRITE)
    _create(client)
    calls: list[str] = []

    async def forget_commits(skill_id: str) -> int:
        calls.append(f"forget:{skill_id}")
        return 0

    monkeypatch.setattr(
        skills_catalog,
        "get_skill_runtime",
        lambda: SimpleNamespace(forget_commits=forget_commits),
    )

    assert client.post("/api/skills/skill-1/draft/save").status_code == 200

    # 保存是事件驱动的清理时机：新版本产生时只收拾这个技能自己的旧缓存。
    assert calls == ["forget:skill-1"]

def test_scan_stream_forwards_status_then_the_result(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeAuthorService()
    client, _identity = _client(monkeypatch, service, scopes=READ | WRITE)
    _create(client)
    assert client.post("/api/skills/skill-1/draft/save").status_code == 200

    streamed = client.post(
        "/api/skills/skill-1/scan",
        headers={"Accept": "text/event-stream"},
    )
    assert streamed.status_code == 200, streamed.text
    assert "text/event-stream" in streamed.headers["content-type"]
    assert streamed.text.index("event: status") < streamed.text.index("event: result")
    assert "正在做规则检查，并交给模型分析" in streamed.text
    assert '"is_safe": true' in streamed.text


def test_save_scan_activate_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeAuthorService()
    client, _identity = _client(monkeypatch, service, scopes=READ | WRITE)
    _create(client)
    assert client.post("/api/skills/skill-1/draft/save").status_code == 200

    row = client.get("/api/skills/mine").json()["skills"][0]
    assert row["status"] == "unscanned"
    assert row["can_activate"] is False
    assert row["latest_scan"] is None

    scanned = client.post("/api/skills/skill-1/scan")
    assert scanned.status_code == 200, scanned.text
    assert scanned.json()["is_safe"] is True
    assert scanned.json()["scanner"] == "cisco-ai-skill-scanner"

    row = client.get("/api/skills/mine").json()["skills"][0]
    assert row["status"] == "scanned"
    assert row["can_activate"] is True

    activated = client.post("/api/skills/skill-1/activate", json={})
    assert activated.status_code == 200, activated.text
    assert activated.json()["active_version_id"] == "version-1"
    assert activated.json()["index_status"] == "disabled"

    row = client.get("/api/skills/mine").json()["skills"][0]
    assert row["status"] == "active"
    assert row["active"] is True


def test_extracted_skill_is_listed_with_provenance_and_unscanned_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeAuthorService()
    client, _identity = _client(monkeypatch, service, scopes=READ | WRITE)
    _create(client, "session-helper")
    client.post("/api/skills/skill-1/draft/save")
    service.skills["skill-1"] = replace(
        service.skills["skill-1"],
        source="extracted",
        source_uri="viking://user/u1/skills/session-helper",
    )

    row = client.get("/api/skills/mine").json()["skills"][0]

    assert row["source"] == "extracted"
    assert row["source_uri"] == "viking://user/u1/skills/session-helper"
    assert row["status"] == "unscanned"


def test_activation_without_a_passing_scan_is_409(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeAuthorService()
    client, _identity = _client(monkeypatch, service)
    _create(client)
    client.post("/api/skills/skill-1/draft/save")

    missing = client.post("/api/skills/skill-1/activate", json={})
    assert missing.status_code == 409
    assert missing.json()["code"] == "skill_conflict"

    service.safe_scan = False
    client.post("/api/skills/skill-1/scan")
    failed = client.post("/api/skills/skill-1/activate", json={})
    assert failed.status_code == 409
    assert "did not pass" in failed.json()["detail"]


def test_scan_requires_a_saved_version(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeAuthorService()
    client, _identity = _client(monkeypatch, service)
    _create(client)

    response = client.post("/api/skills/skill-1/scan")

    assert response.status_code == 409
    assert response.json()["code"] == "skill_conflict"


def test_missing_scanner_package_is_a_503(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeAuthorService()
    client, _identity = _client(monkeypatch, service)
    _create(client)
    client.post("/api/skills/skill-1/draft/save")

    async def missing_scanner(**_kwargs):
        raise SkillScanUnavailable("cisco-ai-skill-scanner is not installed")

    monkeypatch.setattr(service, "scan_security", missing_scanner)

    response = client.post("/api/skills/skill-1/scan")

    assert response.status_code == 503
    assert response.json()["code"] == "skill_scanner_unavailable"


def test_draft_file_path_traversal_is_422(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeAuthorService()
    client, _identity = _client(monkeypatch, service)
    _create(client)

    response = client.put(
        "/api/skills/skill-1/draft/file",
        json={"path": "../escape.txt", "content": "no"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_skill_package"


def test_write_routes_require_skills_write(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeAuthorService()
    client, _identity = _client(monkeypatch, service, scopes=READ)

    response = client.post(
        "/api/skills", json={"name": "alice-tools", "description": "Alice's tools"}
    )

    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"


def test_another_users_scan_and_activate_are_404(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeAuthorService()
    client, identity = _client(monkeypatch, service)
    _create(client)
    client.post("/api/skills/skill-1/draft/save")

    identity.as_(WRITE, user_id="bob")
    for url in (
        "/api/skills/skill-1/scan",
        "/api/skills/skill-1/activate",
    ):
        response = client.post(url, json={})
        assert response.status_code == 404, (url, response.text)
        assert response.json()["code"] == "skill_not_found"


def test_activate_refreshes_the_catalog_and_save_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeAuthorService()
    client, _identity = _client(monkeypatch, service)
    _create(client)
    calls: list[str] = []

    async def refresh() -> None:
        calls.append("refresh")

    monkeypatch.setattr(skills_catalog, "refresh_skill_runtime", refresh)
    monkeypatch.setattr(skills_catalog, "get_skill_runtime", lambda: None)

    assert client.post("/api/skills/skill-1/draft/save").status_code == 200
    assert calls == []

    client.post("/api/skills/skill-1/scan")
    assert calls == []

    assert client.post("/api/skills/skill-1/activate", json={}).status_code == 200
    assert calls == ["refresh"]


def test_delete_drains_the_projection_outbox_after_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeAuthorService()
    client, _identity = _client(monkeypatch, service)
    _create(client)
    calls: list[str] = []

    class _Outbox:
        async def run_once(self) -> int:
            calls.append("outbox")
            return 1

    async def refresh() -> None:
        calls.append("refresh")

    async def forget_commits(skill_id: str) -> int:
        calls.append(f"forget:{skill_id}")
        return 0

    monkeypatch.setattr(skills_catalog, "refresh_skill_runtime", refresh)
    monkeypatch.setattr(
        skills_catalog,
        "get_skill_runtime",
        lambda: SimpleNamespace(outbox=_Outbox(), forget_commits=forget_commits),
    )

    assert client.delete("/api/skills/skill-1").status_code == 204
    # 删除先收掉该技能的提交缓存，再刷新运行时，最后 drain 掉投影删除事件。
    assert calls == ["forget:skill-1", "refresh", "outbox"]


def test_delete_removes_the_skill_and_refreshes(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeAuthorService()
    client, _identity = _client(monkeypatch, service)
    _create(client)
    calls: list[str] = []

    async def refresh() -> None:
        calls.append("refresh")

    monkeypatch.setattr(skills_catalog, "refresh_skill_runtime", refresh)

    assert client.delete("/api/skills/skill-1").status_code == 204
    assert calls == ["refresh"]
    assert client.get("/api/skills/skill-1/draft").status_code == 404


@pytest.mark.parametrize("supported", [True, False])
def test_private_container_file_requires_provider_support(
    monkeypatch: pytest.MonkeyPatch, supported: bool
) -> None:
    service = FakeAuthorService()
    client, _identity = _client(monkeypatch, service)
    monkeypatch.setattr(
        skills_catalog, "_private_mounts_supported", lambda: supported
    )

    created = _create(client)

    assert created["container_file"] == (
        "/mnt/skills/private/alice-tools/SKILL.md" if supported else None
    )

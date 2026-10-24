"""Task 8: the author-facing HTTP surface for private skill drafts."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentkit.config.skills_config import SkillCreationConfig
from agentkit.skills.author import SkillAuthorService
from agentkit.skills.domain import (
    InvalidSkillPackage,
    SkillConflict,
    SkillRecord,
    SkillSubmissionRecord,
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
    """A narrow fake of SkillAuthorService; route tests assert HTTP behavior."""

    _next: int = 1
    skills: dict[str, SkillRecord] = field(default_factory=dict)
    files: dict[str, list[StagedFile]] = field(default_factory=dict)
    versions: dict[str, list[SkillVersionRecord]] = field(default_factory=dict)
    pending: dict[str, SkillSubmissionRecord] = field(default_factory=dict)
    put_calls: list[tuple[str, str, bytes]] = field(default_factory=list)

    def _owned(self, owner_user_id: str, skill_id: str) -> SkillRecord:
        skill = self.skills.get(skill_id)
        if skill is None or skill.owner_user_id != owner_user_id:
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
            public_name=None,
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
        return record

    async def list_mine(self, *, owner_user_id: str) -> list[SkillRecord]:
        return [
            skill
            for skill in self.skills.values()
            if skill.owner_user_id == owner_user_id and skill.deleted_at is None
        ]

    async def get_owned_skill(
        self, *, owner_user_id: str, skill_id: str
    ) -> SkillRecord | None:
        skill = self.skills.get(skill_id)
        if (
            skill is None
            or skill.owner_user_id != owner_user_id
            or skill.deleted_at is not None
        ):
            return None
        return skill

    async def open_draft(self, *, owner_user_id: str, skill_id: str) -> list[StagedFileInfo]:
        self._owned(owner_user_id, skill_id)
        return [
            _info(file.path, file.content, file.mode)
            for file in self.files[skill_id]
        ]

    async def list_files(self, *, owner_user_id: str, skill_id: str) -> list[StagedFileInfo]:
        self._owned(owner_user_id, skill_id)
        return [
            _info(file.path, file.content, file.mode)
            for file in self.files[skill_id]
        ]

    async def read_file(
        self, *, owner_user_id: str, skill_id: str, path: str
    ) -> StagedFile | None:
        self._owned(owner_user_id, skill_id)
        return next(
            (file for file in self.files[skill_id] if file.path == path), None
        )

    async def put_file(
        self,
        *,
        owner_user_id: str,
        skill_id: str,
        path: str,
        content: bytes,
        mode: str = "text",
    ) -> StagedFileInfo:
        self._owned(owner_user_id, skill_id)
        self.put_calls.append((skill_id, path, content))
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
        version = SkillVersionRecord(
            id=f"version-{len(self.versions[skill_id]) + 1}",
            skill_id=skill_id,
            skill_version=f"v{len(self.versions[skill_id]) + 1}",
            version_seq=len(self.versions[skill_id]) + 1,
            package_sha256="a" * 64,
            skill_md_sha256="b" * 64,
            metadata={},
            draft_ref=f"refs/heads/myflow/drafts/{owner_user_id}",
            draft_commit="c" * 40,
            skill_path=f"users/{owner_user_id}/{skill_id}/SKILL.md",
            index_status="pending",
            created_at=_now(),
        )
        self.versions[skill_id].append(version)
        return version

    async def list_versions(
        self, *, owner_user_id: str, skill_id: str
    ) -> list[SkillVersionRecord]:
        self._owned(owner_user_id, skill_id)
        return list(self.versions[skill_id])

    async def submission_status(
        self, *, owner_user_id: str, skill_id: str
    ) -> str | None:
        self._owned(owner_user_id, skill_id)
        submission = self.pending.get(skill_id)
        return submission.status if submission is not None else None

    async def submit(self, *, owner_user_id: str, skill_id: str) -> SkillSubmissionRecord:
        self._owned(owner_user_id, skill_id)
        if not self.versions[skill_id]:
            raise SkillConflict(f"skill {skill_id!r} has no saved version")
        submission = SkillSubmissionRecord(
            id=f"submission-{skill_id}",
            skill_id=skill_id,
            version_id=self.versions[skill_id][-1].id,
            submitted_commit="c" * 40,
            owner_user_id=owner_user_id,
            status="pending",
            submitted_at=_now(),
            reviewed_by=None,
            reviewed_at=None,
            reject_reason=None,
            public_release_id=None,
        )
        self.pending[skill_id] = submission
        return submission

    async def withdraw(
        self, *, owner_user_id: str, skill_id: str
    ) -> SkillSubmissionRecord:
        self._owned(owner_user_id, skill_id)
        submission = self.pending.pop(skill_id, None)
        if submission is None:
            raise SkillConflict(f"skill {skill_id!r} has no pending submission")
        return SkillSubmissionRecord(
            id=submission.id,
            skill_id=submission.skill_id,
            version_id=submission.version_id,
            submitted_commit=submission.submitted_commit,
            owner_user_id=submission.owner_user_id,
            status="withdrawn",
            submitted_at=submission.submitted_at,
            reviewed_by=None,
            reviewed_at=None,
            reject_reason=None,
            public_release_id=None,
        )

    async def delete_draft(self, *, owner_user_id: str, skill_id: str) -> None:
        self._owned(owner_user_id, skill_id)
        self.skills[skill_id] = SkillRecord(
            id=self.skills[skill_id].id,
            owner_user_id=self.skills[skill_id].owner_user_id,
            private_name=self.skills[skill_id].private_name,
            public_name=self.skills[skill_id].public_name,
            created_at=self.skills[skill_id].created_at,
            updated_at=_now(),
            deleted_at=_now(),
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
    service: FakeAuthorService,
    *,
    scopes: frozenset[str] | None = WRITE,
    user_id: str = "alice",
) -> tuple[TestClient, _Identity]:
    application = FastAPI(redirect_slashes=False)
    install_middleware(application, auth_settings=AuthSettings.disabled())
    install_error_handlers(application)
    application.state.skill_author_service = service
    # The new router must be included first so /skills/mine wins over later
    # /skills/{name} routes and the old filesystem routes keep working.
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


def _create(client: TestClient, name: str = "alice-tools") -> dict[str, Any]:
    response = client.post(
        "/api/skills", json={"name": name, "description": "Alice's tools"}
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_create_returns_201_and_only_the_owner_can_read_the_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeAuthorService()
    alice, identity = _client(monkeypatch, service)

    created = _create(alice)

    assert created["id"] == "skill-1"
    assert created["scope"] == "private"
    assert created["owner_user_id"] == "alice"
    assert created["status"] == "draft"
    assert created["container_file"] == "/mnt/skills/private/alice-tools/SKILL.md"
    assert "D:\\" not in str(created) and "/tmp/" not in str(created)

    draft = alice.get("/api/skills/skill-1/draft")
    assert draft.status_code == 200
    assert draft.json()["skill"]["id"] == "skill-1"
    assert [file["path"] for file in draft.json()["files"]] == ["SKILL.md"]

    identity.as_(WRITE, user_id="bob")
    hidden = alice.get("/api/skills/skill-1/draft")
    assert hidden.status_code == 404
    assert hidden.json()["code"] == "skill_not_found"


def test_mine_lists_private_skills_for_the_caller_only(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeAuthorService()
    alice_client, identity = _client(monkeypatch, service)
    alice_skill = _create(alice_client, "alice-tools")

    identity.as_(WRITE, user_id="bob")
    bob_skill = _create(alice_client, "bob-tools")

    identity.as_(READ, user_id="alice")
    alice_mine = alice_client.get("/api/skills/mine")
    assert alice_mine.status_code == 200
    assert [item["id"] for item in alice_mine.json()["skills"]] == [alice_skill["id"]]

    identity.as_(READ, user_id="bob")
    bob_mine = alice_client.get("/api/skills/mine")
    assert [item["id"] for item in bob_mine.json()["skills"]] == [bob_skill["id"]]


def test_put_draft_file_rejects_path_traversal_with_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeAuthorService()
    client, _identity = _client(monkeypatch, service)
    _create(client)

    response = client.put(
        "/api/skills/skill-1/draft/file",
        json={"path": "../escape.txt", "content": "not allowed"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_skill_package"
    assert service.put_calls == []


def test_submit_without_a_saved_version_is_409_skill_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeAuthorService()
    client, _identity = _client(monkeypatch, service)
    _create(client)

    response = client.post("/api/skills/skill-1/submit")

    assert response.status_code == 409
    assert response.json()["code"] == "skill_conflict"


def test_write_routes_require_skills_write(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeAuthorService()
    client, _identity = _client(monkeypatch, service, scopes=READ)

    response = client.post(
        "/api/skills", json={"name": "alice-tools", "description": "Alice's tools"}
    )

    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"


def test_another_users_draft_is_404_not_403(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeAuthorService()
    client, identity = _client(monkeypatch, service)
    _create(client)

    identity.as_(WRITE, user_id="bob")
    for method, url, body in (
        ("get", "/api/skills/skill-1/draft", None),
        ("get", "/api/skills/skill-1/draft/tree", None),
        ("get", "/api/skills/skill-1/draft/file?path=SKILL.md", None),
        ("put", "/api/skills/skill-1/draft/file", {"path": "SKILL.md", "content": "x"}),
        ("post", "/api/skills/skill-1/draft/save", None),
        ("post", "/api/skills/skill-1/submit", None),
        ("delete", "/api/skills/skill-1", None),
    ):
        response = getattr(client, method)(url, json=body) if body is not None else getattr(client, method)(url)
        assert response.status_code == 404, (method, url, response.text)
        assert response.json()["code"] == "skill_not_found"


def test_open_draft_returns_the_staged_file_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeAuthorService()
    client, _identity = _client(monkeypatch, service)
    _create(client)

    response = client.post("/api/skills/skill-1/draft/open")

    assert response.status_code == 200
    assert [file["path"] for file in response.json()["files"]] == ["SKILL.md"]


def test_file_crud_save_versions_and_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeAuthorService()
    client, _identity = _client(monkeypatch, service)
    _create(client)

    written = client.put(
        "/api/skills/skill-1/draft/file",
        json={"path": "notes.txt", "content": "hello"},
    )
    assert written.status_code == 200
    assert written.json()["path"] == "notes.txt"

    listing = client.get("/api/skills/skill-1/draft/tree")
    assert [file["path"] for file in listing.json()["files"]] == ["SKILL.md", "notes.txt"]

    read = client.get("/api/skills/skill-1/draft/file?path=notes.txt")
    assert read.status_code == 200
    assert read.json()["content"] == "hello"

    saved = client.post("/api/skills/skill-1/draft/save")
    assert saved.status_code == 200
    assert saved.json()["version"]["skill_version"] == "v1"

    versions = client.get("/api/skills/skill-1/versions")
    assert [item["skill_version"] for item in versions.json()["versions"]] == ["v1"]

    deleted = client.delete("/api/skills/skill-1/draft/file?path=notes.txt")
    assert deleted.status_code == 204

    assert client.delete("/api/skills/skill-1").status_code == 204
    assert client.get("/api/skills/skill-1/draft").status_code == 404


def test_upload_draft_files_stages_multipart_attachments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeAuthorService()
    client, _identity = _client(monkeypatch, service)
    _create(client)

    response = client.post(
        "/api/skills/skill-1/draft/files",
        files={"files": ("scripts/run.sh", b"echo ok\n", "text/plain")},
    )

    assert response.status_code == 200
    assert [file["path"] for file in response.json()["files"]] == ["SKILL.md", "scripts/run.sh"]


def test_save_and_delete_refresh_the_catalog_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeAuthorService()
    client, _identity = _client(monkeypatch, service)
    _create(client)
    calls: list[str] = []

    async def refresh() -> None:
        calls.append("refresh")

    monkeypatch.setattr(skills_catalog, "refresh_skill_runtime", refresh)

    assert client.post("/api/skills/skill-1/draft/save").status_code == 200
    assert calls == ["refresh"]
    assert client.delete("/api/skills/skill-1").status_code == 204
    assert calls == ["refresh", "refresh"]


def test_submit_and_withdraw_do_not_refresh_the_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeAuthorService()
    client, _identity = _client(monkeypatch, service)
    _create(client)
    assert client.post("/api/skills/skill-1/draft/save").status_code == 200
    calls: list[str] = []

    async def refresh() -> None:
        calls.append("refresh")

    monkeypatch.setattr(skills_catalog, "refresh_skill_runtime", refresh)

    assert client.post("/api/skills/skill-1/submit").status_code == 200
    assert client.post("/api/skills/skill-1/withdraw").status_code == 200
    # Pending state lives in PostgreSQL and is read straight from the store, so
    # rebuilding the snapshot and mount trees here would be pure overhead.
    assert calls == []


@pytest.mark.parametrize("supported", [True, False])
def test_mine_omits_private_container_file_when_mounts_are_unsupported(
    monkeypatch: pytest.MonkeyPatch, supported: bool
) -> None:
    service = FakeAuthorService()
    client, identity = _client(monkeypatch, service)
    _create(client)
    identity.as_(READ, user_id="alice")
    monkeypatch.setattr(
        skills_catalog, "_private_mounts_supported", lambda: supported
    )

    rows = client.get("/api/skills/mine").json()["skills"]

    assert rows[0]["container_file"] == (
        "/mnt/skills/private/alice-tools/SKILL.md" if supported else None
    )


def test_route_order_keeps_mine_before_skill_id_routes() -> None:
    paths = [route.path for route in skill_drafts.router.routes]
    assert paths.index("/skills/mine") < paths.index("/skills/{skill_id}/draft")

class _ReadStore:
    """Minimal store for the public read helpers added to SkillAuthorService."""

    def __init__(
        self,
        skills: dict[str, SkillRecord],
        files: dict[str, list[StagedFile]],
        pending: dict[str, SkillSubmissionRecord] | None = None,
    ) -> None:
        self.skills = skills
        self.files = files
        self.pending = pending or {}

    async def list_owned(self, owner_user_id: str) -> list[SkillRecord]:
        return [
            skill
            for skill in self.skills.values()
            if skill.owner_user_id == owner_user_id and skill.deleted_at is None
        ]

    async def get_skill(self, skill_id: str) -> SkillRecord | None:
        return self.skills.get(skill_id)

    async def pending_submission(self, skill_id: str) -> SkillSubmissionRecord | None:
        return self.pending.get(skill_id)

    async def list_draft_files(self, skill_id: str) -> list[StagedFile]:
        return self.files[skill_id]


def _record(skill_id: str, owner: str, name: str) -> SkillRecord:
    now = _now()
    return SkillRecord(
        id=skill_id,
        owner_user_id=owner,
        private_name=name,
        public_name=None,
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )


def _service(store: _ReadStore) -> SkillAuthorService:
    return SkillAuthorService(store, object(), object(), SkillCreationConfig())  # type: ignore[arg-type]


def test_author_read_helpers_scope_mine_and_ownership() -> None:
    alice = _record("skill-1", "alice", "alice-tools")
    bob = _record("skill-2", "bob", "bob-tools")
    service = _service(
        _ReadStore(
            {alice.id: alice, bob.id: bob},
            {alice.id: [], bob.id: []},
        )
    )

    async def scenario() -> None:
        assert [item.id for item in await service.list_mine(owner_user_id="alice")] == [
            alice.id
        ]
        assert await service.get_owned_skill(
            owner_user_id="alice", skill_id=bob.id
        ) is None
        assert await service.get_owned_skill(
            owner_user_id="alice", skill_id=alice.id
        ) == alice

    asyncio.run(scenario())


def test_author_read_file_returns_staged_bytes_and_rejects_traversal() -> None:
    alice = _record("skill-1", "alice", "alice-tools")
    service = _service(
        _ReadStore(
            {alice.id: alice},
            {alice.id: [StagedFile("notes.txt", b"hello", "text")]},
        )
    )

    async def scenario() -> None:
        staged = await service.read_file(
            owner_user_id="alice", skill_id=alice.id, path="notes.txt"
        )
        assert staged is not None and staged.content == b"hello"
        assert await service.read_file(
            owner_user_id="alice", skill_id=alice.id, path="missing.txt"
        ) is None
        with pytest.raises(InvalidSkillPackage):
            await service.read_file(
                owner_user_id="alice", skill_id=alice.id, path="../escape.txt"
            )

    asyncio.run(scenario())


def test_author_submission_status_reports_only_pending() -> None:
    alice = _record("skill-1", "alice", "alice-tools")
    submission = SkillSubmissionRecord(
        id="submission-1",
        skill_id=alice.id,
        version_id="version-1",
        submitted_commit="c" * 40,
        owner_user_id="alice",
        status="pending",
        submitted_at=_now(),
        reviewed_by=None,
        reviewed_at=None,
        reject_reason=None,
        public_release_id=None,
    )
    service = _service(
        _ReadStore(
            {alice.id: alice},
            {alice.id: []},
            {alice.id: submission},
        )
    )

    async def scenario() -> None:
        assert await service.submission_status(
            owner_user_id="alice", skill_id=alice.id
        ) == "pending"

    asyncio.run(scenario())

"""Task 9: the admin review HTTP surface."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentkit.skills.domain import (
    ReviewDetail,
    ReviewItem,
    SkillConflict,
    SkillReleaseRecord,
    SkillSubmissionRecord,
    StagedFileInfo,
)
from backend.app.gateway import deps, skills_catalog
from backend.app.gateway.app import install_error_handlers, install_middleware
from backend.app.gateway.auth import AuthSettings
from backend.app.gateway.routers import skill_reviews
from backend.app.gateway.run_models import CallerKind, RunCaller

ADMIN = frozenset({"admin"})
READ = frozenset({"skills:read"})


def _now() -> datetime:
    return datetime(2026, 10, 23, 12, 34, 56, tzinfo=UTC)


def _summary(
    submission_id: str = "submission-1",
    *,
    skill_id: str = "skill-1",
    name: str = "pdf-tools",
    version: str = "v3",
    owner_user_id: str = "alice",
    submitted_commit: str = "a" * 40,
) -> ReviewItem:
    return ReviewItem(
        submission_id=submission_id,
        skill_id=skill_id,
        name=name,
        version=version,
        owner_user_id=owner_user_id,
        submitted_at=_now(),
        submitted_commit=submitted_commit,
    )


def _detail() -> ReviewDetail:
    item = _summary()
    return ReviewDetail(
        item=item,
        files=[
            StagedFileInfo(
                path="SKILL.md", size_bytes=111, mode="text", sha256="b" * 64
            ),
            StagedFileInfo(
                path="scripts/run.sh",
                size_bytes=9,
                mode="executable",
                sha256="c" * 64,
            ),
        ],
        skill_md="---\nname: pdf-tools\n---\nBody\n",
        diff="--- a/SKILL.md\n+++ b/SKILL.md\n+Body\n",
        metadata={"name": "pdf-tools", "description": "Does things"},
        package_bytes=120,
    )


def _release() -> SkillReleaseRecord:
    return SkillReleaseRecord(
        id="release-1",
        batch_id="batch-1",
        version_id="version-1",
        skill_id="skill-1",
        skill_version="v3",
        git_repository="https://example.invalid/skills.git",
        git_commit="d" * 40,
        skill_path="public/pdf-tools/SKILL.md",
        public_name="pdf-tools",
        source_commit="a" * 40,
        approved_by="admin",
        created_at=_now(),
    )


def _submission(status: str = "rejected", reason: str | None = "needs work") -> SkillSubmissionRecord:
    return SkillSubmissionRecord(
        id="submission-1",
        skill_id="skill-1",
        version_id="version-1",
        submitted_commit="a" * 40,
        owner_user_id="alice",
        status=status,
        submitted_at=_now(),
        reviewed_by="admin",
        reviewed_at=_now(),
        reject_reason=reason,
        public_release_id=None,
    )


@dataclass
class FakeReviewService:
    """Narrow fake: these tests pin the HTTP boundary, not review.py."""

    items: list[ReviewItem] = field(default_factory=lambda: [_summary()])
    detail_by_id: dict[str, ReviewDetail] = field(
        default_factory=lambda: {"submission-1": _detail()}
    )
    release: SkillReleaseRecord = field(default_factory=_release)
    rejected: SkillSubmissionRecord = field(default_factory=_submission)
    conflict: SkillConflict | None = None
    calls: list[tuple[Any, ...]] = field(default_factory=list)

    def _raise_if_conflict(self) -> None:
        if self.conflict is not None:
            raise self.conflict

    async def list_pending(self, limit: int = 50) -> list[ReviewItem]:
        self.calls.append(("list_pending", limit))
        self._raise_if_conflict()
        return list(self.items)

    async def detail(self, submission_id: str) -> ReviewDetail:
        self.calls.append(("detail", submission_id))
        self._raise_if_conflict()
        detail = self.detail_by_id.get(submission_id)
        if detail is None:
            raise SkillConflict(f"submission {submission_id!r} does not exist")
        return detail

    async def approve(
        self, *, admin_user_id: str, submission_id: str
    ) -> SkillReleaseRecord:
        self.calls.append(("approve", admin_user_id, submission_id))
        self._raise_if_conflict()
        return self.release

    async def reject(
        self, *, admin_user_id: str, submission_id: str, reason: str
    ) -> SkillSubmissionRecord:
        self.calls.append(("reject", admin_user_id, submission_id, reason))
        self._raise_if_conflict()
        return self.rejected

    async def unpublish(self, *, admin_user_id: str, skill_id: str) -> None:
        self.calls.append(("unpublish", admin_user_id, skill_id))
        self._raise_if_conflict()

    async def activate(
        self, *, admin_user_id: str, skill_id: str, release_id: str
    ) -> None:
        self.calls.append(("activate", admin_user_id, skill_id, release_id))
        self._raise_if_conflict()


class _Identity(dict):
    def __init__(self, scopes: frozenset[str] | None, user_id: str | None) -> None:
        super().__init__(scopes=scopes, user_id=user_id)

    def as_(self, scopes: frozenset[str] | None, *, user_id: str | None = None) -> "_Identity":
        self["scopes"] = scopes
        self["user_id"] = user_id
        return self


def _client(
    monkeypatch: pytest.MonkeyPatch,
    service: FakeReviewService,
    *,
    scopes: frozenset[str] | None = ADMIN,
    user_id: str = "admin",
) -> tuple[TestClient, _Identity]:
    application = FastAPI(redirect_slashes=False)
    install_middleware(application, auth_settings=AuthSettings.disabled())
    install_error_handlers(application)
    application.state.skill_review_service = service
    application.include_router(skill_reviews.router, prefix="/api")

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


def test_normal_user_gets_403_on_every_admin_route(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeReviewService()
    client, _identity = _client(monkeypatch, service, scopes=READ, user_id="alice")

    requests = (
        ("get", "/api/admin/skills/reviews", None),
        ("get", "/api/admin/skills/reviews/submission-1", None),
        ("post", "/api/admin/skills/reviews/submission-1/approve", None),
        (
            "post",
            "/api/admin/skills/reviews/submission-1/reject",
            {"reason": "needs work"},
        ),
        ("post", "/api/admin/skills/skill-1/unpublish", None),
        (
            "post",
            "/api/admin/skills/skill-1/activate",
            {"release_id": "release-1"},
        ),
    )

    for method, url, body in requests:
        response = (
            getattr(client, method)(url, json=body)
            if body is not None
            else getattr(client, method)(url)
        )
        assert response.status_code == 403, (method, url, response.text)
        assert response.json()["code"] == "permission_denied"
    assert service.calls == []


def test_queue_returns_review_summaries(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeReviewService(
        items=[
            _summary("submission-2", submitted_commit="e" * 40),
            _summary("submission-1", version="v2"),
        ]
    )
    client, _identity = _client(monkeypatch, service)

    response = client.get("/api/admin/skills/reviews")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "items": [
            {
                "submission_id": "submission-2",
                "skill_id": "skill-1",
                "name": "pdf-tools",
                "version": "v3",
                "owner_user_id": "alice",
                "submitted_at": "2026-10-23T12:34:56+00:00",
                "submitted_commit": "e" * 40,
            },
            {
                "submission_id": "submission-1",
                "skill_id": "skill-1",
                "name": "pdf-tools",
                "version": "v2",
                "owner_user_id": "alice",
                "submitted_at": "2026-10-23T12:34:56+00:00",
                "submitted_commit": "a" * 40,
            },
        ]
    }
    assert service.calls == [("list_pending", 50)]


def test_detail_returns_the_submitted_commit_tree_diff_and_package_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeReviewService()
    client, _identity = _client(monkeypatch, service)

    response = client.get("/api/admin/skills/reviews/submission-1")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["submission_id"] == "submission-1"
    assert body["submitted_commit"] == "a" * 40
    assert body["package_bytes"] == 120
    assert body["metadata"] == {
        "name": "pdf-tools",
        "description": "Does things",
    }
    assert body["files"] == [
        {
            "path": "SKILL.md",
            "size_bytes": 111,
            "mode": "text",
            "sha256": "b" * 64,
        },
        {
            "path": "scripts/run.sh",
            "size_bytes": 9,
            "mode": "executable",
            "sha256": "c" * 64,
        },
    ]
    assert "+Body" in body["diff"]
    assert body["skill_md"].startswith("---")
    assert "D:\\" not in response.text and "/tmp/" not in response.text
    assert service.calls == [("detail", "submission-1")]


def test_approve_returns_the_public_release(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeReviewService()
    client, _identity = _client(monkeypatch, service)

    response = client.post("/api/admin/skills/reviews/submission-1/approve")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["release_id"] == "release-1"
    assert body["skill_id"] == "skill-1"
    assert body["skill_version"] == "v3"
    assert body["git_repository"] == "https://example.invalid/skills.git"
    assert body["git_commit"] == "d" * 40
    assert body["skill_path"] == "public/pdf-tools/SKILL.md"
    assert body["public_name"] == "pdf-tools"
    assert body["approved_by"] == "admin"
    assert service.calls == [("approve", "admin", "submission-1")]


def test_approve_maps_skill_conflict_to_409(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeReviewService(conflict=SkillConflict("submission is not pending"))
    client, _identity = _client(monkeypatch, service)

    response = client.post("/api/admin/skills/reviews/submission-1/approve")

    assert response.status_code == 409
    assert response.json()["code"] == "skill_conflict"


def test_reject_requires_a_non_empty_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeReviewService()
    client, _identity = _client(monkeypatch, service)

    for reason in ("", "   "):
        response = client.post(
            "/api/admin/skills/reviews/submission-1/reject",
            json={"reason": reason},
        )
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "invalid_request"

    assert service.calls == []


def test_reject_returns_the_rejected_submission(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeReviewService()
    client, _identity = _client(monkeypatch, service)

    response = client.post(
        "/api/admin/skills/reviews/submission-1/reject",
        json={"reason": "needs a clearer procedure"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["submission_id"] == "submission-1"
    assert body["status"] == "rejected"
    assert body["owner_user_id"] == "alice"
    assert service.calls == [
        ("reject", "admin", "submission-1", "needs a clearer procedure")
    ]


def test_unpublish_changes_the_public_pointer(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeReviewService()
    client, _identity = _client(monkeypatch, service)

    response = client.post("/api/admin/skills/skill-1/unpublish")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "skill_id": "skill-1",
        "unpublished": True,
    }
    assert service.calls == [("unpublish", "admin", "skill-1")]


def test_activate_requires_a_release_id_and_returns_no_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeReviewService()
    client, _identity = _client(monkeypatch, service)

    missing = client.post("/api/admin/skills/skill-1/activate", json={})
    assert missing.status_code == 422
    assert missing.json()["code"] == "invalid_request"

    response = client.post(
        "/api/admin/skills/skill-1/activate",
        json={"release_id": "release-1"},
    )
    assert response.status_code == 204, response.text
    assert response.content == b""
    assert service.calls == [("activate", "admin", "skill-1", "release-1")]


def test_activate_maps_skill_conflict_to_409(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeReviewService(conflict=SkillConflict("release does not exist"))
    client, _identity = _client(monkeypatch, service)

    response = client.post(
        "/api/admin/skills/skill-1/activate",
        json={"release_id": "release-1"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "skill_conflict"


def test_every_state_changing_review_route_refreshes_the_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeReviewService()
    client, _identity = _client(monkeypatch, service)
    calls: list[str] = []

    async def refresh() -> None:
        calls.append("refresh")

    monkeypatch.setattr(skills_catalog, "refresh_skill_runtime", refresh)

    assert client.post(
        "/api/admin/skills/reviews/submission-1/approve"
    ).status_code == 200
    assert client.post(
        "/api/admin/skills/reviews/submission-1/reject",
        json={"reason": "needs a clearer procedure"},
    ).status_code == 200
    assert client.post("/api/admin/skills/skill-1/unpublish").status_code == 200
    assert client.post(
        "/api/admin/skills/skill-1/activate", json={"release_id": "release-1"}
    ).status_code == 204
    assert calls == ["refresh", "refresh", "refresh", "refresh"]


def test_a_failed_refresh_does_not_fail_a_committed_approval(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    service = FakeReviewService()
    client, _identity = _client(monkeypatch, service)
    real_refresh = skills_catalog.refresh_skill_runtime

    class BrokenRuntime:
        async def refresh(self) -> None:
            raise RuntimeError("database went away after commit")

    async def failing() -> None:
        monkeypatch.setattr(skills_catalog, "_skill_source", lambda: "git")
        monkeypatch.setattr(skills_catalog, "get_skill_runtime", lambda: BrokenRuntime())
        await real_refresh()

    monkeypatch.setattr(skills_catalog, "refresh_skill_runtime", failing)
    caplog.set_level("WARNING", logger="backend.app.gateway.skills_catalog")

    response = client.post("/api/admin/skills/reviews/submission-1/approve")

    assert response.status_code == 200
    assert "refresh failed" in caplog.text


def test_reviews_are_declared_before_generic_admin_skill_routes() -> None:
    paths = [route.path for route in skill_reviews.router.routes]
    assert paths.index("/admin/skills/reviews") < paths.index(
        "/admin/skills/{skill_id}/unpublish"
    )
    assert paths.index("/admin/skills/reviews/{submission_id}") < paths.index(
        "/admin/skills/{skill_id}/unpublish"
    )

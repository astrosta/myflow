"""Admin review and public-release HTTP API (M11 task 9).

Every route requires the admin scope.  Ownership is irrelevant here: the
service is the only authority for which submissions and releases may change.
Review detail is built by SkillReviewService from the submitted Git commit, so
this router never reads private staging rows or emits a host cache path.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, Path, Response
from fastapi import status as http_status

from agentkit.skills.domain import (
    InvalidSkillPackage,
    ReviewDetail,
    ReviewItem,
    SkillConflict,
    SkillReleaseRecord,
    SkillSubmissionRecord,
)
from agentkit.skills.outbox import PROJECTION_TEXT_VERSION
from backend.app.gateway import skills_catalog
from backend.app.gateway.deps import get_skill_review_service, require_scope
from backend.app.gateway.errors import (
    SkillConflictAPI,
    SkillPackageInvalid,
    SkillRuntimeUnavailable,
)
from backend.app.gateway.models.skills import (
    ActivateRequest,
    DraftFileInfo,
    ReindexResponse,
    RejectRequest,
    ReleaseResponse,
    ReviewDetailResponse,
    ReviewQueueResponse,
    ReviewSummary,
    SubmissionResponse,
    UnpublishResponse,
)
from backend.app.gateway.run_models import RunCaller

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/skills", tags=["skill-reviews"])

SubmissionId = Annotated[
    str,
    Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]{1,128}$"),
]
SkillId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
Admin = Annotated[RunCaller, Depends(require_scope("admin"))]
ReviewService = Annotated[Any, Depends(get_skill_review_service)]

T = TypeVar("T")


async def _invoke(action: Awaitable[T]) -> T:
    """Translate domain failures to the gateway's shared error bodies.

    SkillNotFound already carries its own 404 gateway body.  Only the domain
    exceptions with no HTTP mapping need translating here.
    """

    try:
        return await action
    except SkillConflict as exc:
        raise SkillConflictAPI(str(exc)) from exc
    except InvalidSkillPackage as exc:
        raise SkillPackageInvalid(str(exc)) from exc


def _summary(item: ReviewItem) -> ReviewSummary:
    return ReviewSummary(
        submission_id=item.submission_id,
        skill_id=item.skill_id,
        name=item.name,
        version=item.version,
        owner_user_id=item.owner_user_id,
        submitted_at=item.submitted_at.isoformat(),
        submitted_commit=item.submitted_commit,
    )


def _detail(detail: ReviewDetail) -> ReviewDetailResponse:
    summary = _summary(detail.item)
    return ReviewDetailResponse(
        **summary.model_dump(),
        files=[
            DraftFileInfo(
                path=file.path,
                size_bytes=file.size_bytes,
                mode=file.mode,
                sha256=file.sha256,
            )
            for file in detail.files
        ],
        skill_md=detail.skill_md,
        diff=detail.diff,
        metadata=detail.metadata,
        package_bytes=detail.package_bytes,
    )


def _release(release: SkillReleaseRecord) -> ReleaseResponse:
    return ReleaseResponse(
        release_id=release.id,
        skill_id=release.skill_id,
        skill_version=release.skill_version,
        git_repository=release.git_repository,
        git_commit=release.git_commit,
        skill_path=release.skill_path,
        public_name=release.public_name,
        source_commit=release.source_commit,
        approved_by=release.approved_by,
        created_at=release.created_at.isoformat(),
    )


def _submission(submission: SkillSubmissionRecord) -> SubmissionResponse:
    return SubmissionResponse(
        submission_id=submission.id,
        skill_id=submission.skill_id,
        version_id=submission.version_id,
        status=submission.status,
        submitted_at=submission.submitted_at.isoformat(),
        owner_user_id=submission.owner_user_id,
        reject_reason=submission.reject_reason,
    )


# Declare the literal /reviews routes before any /{skill_id} route.  A future
# generic admin skill route must stay below these so "reviews" is never parsed
# as a skill id.
@router.get("/reviews", response_model=ReviewQueueResponse)
async def list_reviews(
    caller: Admin,
    service: ReviewService,
) -> ReviewQueueResponse:
    del caller
    items = await _invoke(service.list_pending())
    return ReviewQueueResponse(items=[_summary(item) for item in items])


@router.get("/reviews/{submission_id}", response_model=ReviewDetailResponse)
async def get_review(
    submission_id: SubmissionId,
    caller: Admin,
    service: ReviewService,
) -> ReviewDetailResponse:
    del caller
    detail = await _invoke(service.detail(submission_id))
    return _detail(detail)


@router.post(
    "/reviews/{submission_id}/approve", response_model=ReleaseResponse
)
async def approve_review(
    submission_id: SubmissionId,
    caller: Admin,
    service: ReviewService,
) -> ReleaseResponse:
    release = await _invoke(
        service.approve(
            admin_user_id=caller.user_id or "",
            submission_id=submission_id,
        )
    )
    await skills_catalog.refresh_skill_runtime()
    return _release(release)


@router.post(
    "/reviews/{submission_id}/reject", response_model=SubmissionResponse
)
async def reject_review(
    submission_id: SubmissionId,
    body: RejectRequest,
    caller: Admin,
    service: ReviewService,
) -> SubmissionResponse:
    submission = await _invoke(
        service.reject(
            admin_user_id=caller.user_id or "",
            submission_id=submission_id,
            reason=body.reason,
        )
    )
    # 驳回只写 submission 行和审计行，不移动任何 active 指针；刷新会重建公共树
    # 和每个作者的私有树，这里纯属浪费。
    return _submission(submission)


@router.post("/reindex", response_model=ReindexResponse)
async def reindex_projection(caller: Admin) -> ReindexResponse:
    """强制重建整张 active projection（运维入口，设计 §7.6）。

    嵌入文本、模型或维度一变，存量向量就全部失效；对账在稳态会跳过健康行，
    所以只改代码不会自愈。这里显式跑一次 reconcile(force=True)：重写全部 active
    行、删掉多余 key，并记一条审计。重建期间检索仍可用（Milvus 只是候选来源）。
    """
    runtime = skills_catalog.get_skill_runtime()
    outbox = getattr(runtime, "outbox", None)
    if runtime is None or outbox is None:
        raise SkillRuntimeUnavailable(
            "skills projection is unavailable: this deployment has no active "
            "projection (skills.search.embedding.enabled=false or no Milvus endpoint)"
        )

    repaired = await outbox.reconcile(force=True)
    store = getattr(runtime, "store", None)
    if store is not None:
        await store.audit(
            actor=caller.user_id or "",
            action="projection_reindex",
            detail={"repaired": repaired, "text_version": PROJECTION_TEXT_VERSION},
        )
    logger.info(
        "Forced skill projection rebuild: %s row(s) repaired", repaired
    )
    return ReindexResponse(
        repaired=repaired, projection_text_version=PROJECTION_TEXT_VERSION
    )


@router.post("/{skill_id}/unpublish", response_model=UnpublishResponse)
async def unpublish_skill(
    skill_id: SkillId,
    caller: Admin,
    service: ReviewService,
) -> UnpublishResponse:
    await _invoke(
        service.unpublish(
            admin_user_id=caller.user_id or "",
            skill_id=skill_id,
        )
    )
    await skills_catalog.refresh_skill_runtime()
    return UnpublishResponse(skill_id=skill_id)


@router.post(
    "/{skill_id}/activate", status_code=http_status.HTTP_204_NO_CONTENT
)
async def activate_release(
    skill_id: SkillId,
    body: ActivateRequest,
    caller: Admin,
    service: ReviewService,
) -> Response:
    """Switch the public active pointer; the service never writes Git here."""

    await _invoke(
        service.activate(
            admin_user_id=caller.user_id or "",
            skill_id=skill_id,
            release_id=body.release_id,
        )
    )
    await skills_catalog.refresh_skill_runtime()
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


__all__ = ["router"]

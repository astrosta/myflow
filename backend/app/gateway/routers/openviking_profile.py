"""Profile API for the OpenViking long-term-memory adapter.

Mounted instead of the canonical memory router when
`memory.openviking.enabled` is true. The response keeps the canonical field
metadata shape so the existing profile page can switch backends without a
separate client; the authoritative concurrency token is the etag.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentkit.agents.memory.openviking.onboarding import OnboardingService
from agentkit.agents.memory.openviking.profile import ProfileConflict, ProfileService
from agentkit.agents.memory.profile_schema import PROFILE_FIELD_SPECS, ProfileFieldSpec
from agentkit.agents.memory.openviking.suggestions import (
    ProfileSuggester,
    ProfileSuggestion,
    SuggestionConflict,
    SuggestionNotFound,
)
from backend.app.gateway.deps import (
    get_openviking_onboarding_service,
    get_openviking_onboarding_service_optional,
    get_openviking_profile_service,
    get_openviking_suggestion_service,
    has_openviking_active_run,
    require_scope,
)
from backend.app.gateway.errors import (
    InvalidMemoryProfile,
    MemoryNotFoundAPI,
    ProfileConflictAPI,
)
from backend.app.gateway.run_models import RunCaller

router = APIRouter(prefix="/memory", tags=["openviking-memory"])


class ProfilePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: dict[str, Any]
    etag: str | None = None
    expected_revision: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _one_concurrency_token(self) -> "ProfilePatchRequest":
        if (self.etag is None) == (self.expected_revision is None):
            raise ValueError("provide exactly one of etag or expected_revision")
        return self


class ProfileDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    etag: str | None = None


class ProfileFieldResponse(BaseModel):
    value: Any
    source: str = "user_manual"
    evidence_tier: str | None = "USER_CONFIRMED"
    confidence: float = 1.0
    observed_at: str


class ProfileResponse(BaseModel):
    schema_version: int = 1
    revision: int
    etag: str
    updated_at: str
    fields: dict[str, ProfileFieldResponse] = Field(default_factory=dict)


class ProfileFieldSpecResponse(BaseModel):
    path: str
    label: str
    group: str
    group_label: str
    kind: str
    description: str
    placeholder: str = ""
    max_length: int
    max_items: int
    item_max_length: int
    explicit_only: bool


class ProfileSchemaResponse(BaseModel):
    """Every supported profile field; the form is rendered from this payload."""

    schema_version: int = 1
    fields: list[ProfileFieldSpecResponse]


class PendingPage(BaseModel):
    items: list[Any] = Field(default_factory=list)
    next_cursor: str | None = None


class OnboardingResponse(BaseModel):
    status: str
    should_show: bool
    profile_field_count: int
    snooze_until: str | None
    dismissed_at: str | None
    updated_at: str


class OnboardingActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["seen", "snooze", "dismiss", "complete"]


class SuggestionResponse(BaseModel):
    id: str
    field_path: str
    value: Any
    evidence_uri: str
    evidence_quote: str
    confidence: float
    reason: str
    status: str
    profile_etag: str
    expires_at: str


class SuggestionPage(BaseModel):
    items: list[SuggestionResponse] = Field(default_factory=list)


class SuggestionActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    etag: str | None = None


class SuggestionAcceptResponse(BaseModel):
    suggestion: SuggestionResponse
    profile: ProfileResponse


def _user_id(caller: RunCaller) -> str:
    return caller.user_id or "default"


def _revision(etag: str) -> int:
    """Stable revision number for clients that still send expected_revision.

    The etag remains authoritative. This integer is only a compatibility view;
    a stale value can only cause a conservative 409, never a blind write.
    """

    try:
        return int(etag[:8], 16)
    except (TypeError, ValueError):
        return 0


def _suggestion_response(item: ProfileSuggestion) -> SuggestionResponse:
    return SuggestionResponse(
        id=item.id,
        field_path=item.field_path,
        value=item.value,
        evidence_uri=item.evidence_uri,
        evidence_quote=item.evidence_quote,
        confidence=item.confidence,
        reason=item.reason,
        status=item.status,
        profile_etag=item.profile_etag,
        expires_at=item.expires_at,
    )


def _field_spec_response(spec: ProfileFieldSpec) -> ProfileFieldSpecResponse:
    return ProfileFieldSpecResponse(
        path=spec.path,
        label=spec.label,
        group=spec.group,
        group_label=spec.group_label,
        kind=spec.kind,
        description=spec.description,
        placeholder=spec.placeholder,
        max_length=spec.max_length,
        max_items=spec.max_items,
        item_max_length=spec.item_max_length,
        explicit_only=spec.explicit_only,
    )


def _onboarding_response(view: Any) -> OnboardingResponse:
    return OnboardingResponse(
        status=view.status,
        should_show=view.should_show,
        profile_field_count=view.profile_field_count,
        snooze_until=view.snooze_until,
        dismissed_at=view.dismissed_at,
        updated_at=view.updated_at,
    )


def _response(view: Any) -> ProfileResponse:
    fields = {
        path: ProfileFieldResponse(value=value, observed_at=view.updated_at)
        for path, value in view.fields.items()
    }
    return ProfileResponse(
        revision=_revision(view.etag),
        etag=view.etag,
        updated_at=view.updated_at,
        fields=fields,
    )


async def _expected_etag(
    service: ProfileService,
    user_id: str,
    *,
    etag: str | None,
    expected_revision: int | None,
) -> str:
    if etag is not None:
        return etag
    current = await service.get_snapshot(user_id)
    if expected_revision is not None and expected_revision != _revision(current.etag):
        raise ProfileConflictAPI("profile etag conflict")
    return current.etag


@router.get("/pending", response_model=PendingPage)
async def pending_memory(
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("memory:read"))],
) -> PendingPage:
    """The OpenViking authority has no canonical pending-review queue.

    The navigation still exists while the cutover is rolled out; returning an
    empty page is honest and keeps the old UI from landing on a 404.
    """

    return PendingPage()


@router.get("/skill-candidates", response_model=list[dict[str, Any]])
async def skill_candidates(
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("memory:read"))],
) -> list[dict[str, Any]]:
    """OpenViking does not own the skill-candidate review surface."""

    return []


@router.get("/profile/onboarding", response_model=OnboardingResponse)
async def get_onboarding(
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("memory:read"))],
    service: Annotated[OnboardingService, Depends(get_openviking_onboarding_service)],
) -> OnboardingResponse:
    user_id = _user_id(caller)
    return _onboarding_response(
        await service.get(user_id, active_run=await has_openviking_active_run(request, user_id))
    )


@router.post("/profile/onboarding", response_model=OnboardingResponse)
async def update_onboarding(
    request: Request,
    body: OnboardingActionRequest,
    caller: Annotated[RunCaller, Depends(require_scope("memory:write"))],
    service: Annotated[OnboardingService, Depends(get_openviking_onboarding_service)],
) -> OnboardingResponse:
    user_id = _user_id(caller)
    return _onboarding_response(
        await service.update(user_id, body.action)
    )


@router.get("/profile/suggestions", response_model=SuggestionPage)
async def list_profile_suggestions(
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("memory:read"))],
    service: Annotated[ProfileSuggester, Depends(get_openviking_suggestion_service)],
) -> SuggestionPage:
    items = await service.get(_user_id(caller))
    return SuggestionPage(items=[_suggestion_response(item) for item in items])


@router.post(
    "/profile/suggestions/{suggestion_id}/accept",
    response_model=SuggestionAcceptResponse,
)
async def accept_profile_suggestion(
    request: Request,
    suggestion_id: str,
    body: SuggestionActionRequest,
    caller: Annotated[RunCaller, Depends(require_scope("memory:write"))],
    service: Annotated[ProfileSuggester, Depends(get_openviking_suggestion_service)],
) -> SuggestionAcceptResponse:
    try:
        suggestion, profile = await service.accept(
            _user_id(caller), suggestion_id, etag=body.etag
        )
    except SuggestionNotFound as exc:
        raise MemoryNotFoundAPI(str(exc)) from exc
    except SuggestionConflict as exc:
        raise ProfileConflictAPI(str(exc)) from exc
    return SuggestionAcceptResponse(
        suggestion=_suggestion_response(suggestion),
        profile=_response(profile),
    )


@router.post(
    "/profile/suggestions/{suggestion_id}/dismiss",
    response_model=SuggestionResponse,
)
async def dismiss_profile_suggestion(
    request: Request,
    suggestion_id: str,
    body: SuggestionActionRequest,
    caller: Annotated[RunCaller, Depends(require_scope("memory:write"))],
    service: Annotated[ProfileSuggester, Depends(get_openviking_suggestion_service)],
) -> SuggestionResponse:
    del body
    try:
        suggestion = await service.dismiss(_user_id(caller), suggestion_id)
    except SuggestionNotFound as exc:
        raise MemoryNotFoundAPI(str(exc)) from exc
    except SuggestionConflict as exc:
        raise ProfileConflictAPI(str(exc)) from exc
    return _suggestion_response(suggestion)


@router.get("/profile", response_model=ProfileResponse)
async def get_profile(
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("memory:read"))],
    service: Annotated[ProfileService, Depends(get_openviking_profile_service)],
) -> ProfileResponse:
    return _response(await service.get_snapshot(_user_id(caller)))


@router.get("/profile/schema", response_model=ProfileSchemaResponse)
async def get_profile_schema(
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("memory:read"))],
) -> ProfileSchemaResponse:
    """The bounded profile schema; the page renders all fields from it.

    This endpoint is static, so it works even before the caller has a profile row
    and regardless of the OpenViking switch.
    """

    del request, caller
    return ProfileSchemaResponse(
        fields=[_field_spec_response(spec) for spec in PROFILE_FIELD_SPECS]
    )


@router.patch("/profile", response_model=ProfileResponse)
async def patch_profile(
    request: Request,
    body: ProfilePatchRequest,
    caller: Annotated[RunCaller, Depends(require_scope("memory:write"))],
    service: Annotated[ProfileService, Depends(get_openviking_profile_service)],
) -> ProfileResponse:
    user_id = _user_id(caller)
    try:
        etag = await _expected_etag(
            service,
            user_id,
            etag=body.etag,
            expected_revision=body.expected_revision,
        )
        view = await service.patch(user_id, body.fields, etag=etag)
    except ProfileConflict as exc:
        raise ProfileConflictAPI(str(exc)) from exc
    except ValueError as exc:
        raise InvalidMemoryProfile(str(exc)) from exc
    onboarding = get_openviking_onboarding_service_optional(request)
    if onboarding is not None and sum(
        1 for value in view.fields.values() if value not in (None, "", [])
    ) >= 2:
        try:
            await onboarding.update(user_id, "complete")
        except Exception:
            pass
    return _response(view)


@router.delete("/profile", response_model=ProfileResponse)
async def clear_profile(
    request: Request,
    caller: Annotated[RunCaller, Depends(require_scope("memory:write"))],
    service: Annotated[ProfileService, Depends(get_openviking_profile_service)],
    body: ProfileDeleteRequest | None = None,
) -> ProfileResponse:
    user_id = _user_id(caller)
    try:
        etag = await _expected_etag(
            service,
            user_id,
            etag=body.etag if body is not None else None,
            expected_revision=None,
        )
        view = await service.clear(user_id, etag=etag)
    except ProfileConflict as exc:
        raise ProfileConflictAPI(str(exc)) from exc
    return _response(view)


@router.delete("/profile/{field_path:path}", response_model=ProfileResponse)
async def delete_profile_field(
    request: Request,
    field_path: str,
    caller: Annotated[RunCaller, Depends(require_scope("memory:write"))],
    service: Annotated[ProfileService, Depends(get_openviking_profile_service)],
    body: ProfileDeleteRequest | None = None,
) -> ProfileResponse:
    user_id = _user_id(caller)
    try:
        etag = await _expected_etag(
            service,
            user_id,
            etag=body.etag if body is not None else None,
            expected_revision=None,
        )
        view = await service.delete_field(user_id, field_path, etag=etag)
    except ProfileConflict as exc:
        raise ProfileConflictAPI(str(exc)) from exc
    except ValueError as exc:
        raise InvalidMemoryProfile(str(exc)) from exc
    return _response(view)


__all__ = ["router"]

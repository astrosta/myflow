"""Canonical profile reads, manual edits, and B direct-write validation."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from agentkit.agents.memory.canonical.domain import (
    MemoryConflictError,
    MemoryNotFoundError,
    MutationContext,
    ProfileApplyResult,
    ProfileFieldView,
    ProfileSnapshotView,
)
from agentkit.agents.memory.canonical.evidence import (
    confidence_for_tier,
    is_profile_confidence,
)
from agentkit.agents.memory.canonical.proposals import ProfileProposal
from agentkit.agents.memory.canonical.repository import MemoryRepository
from agentkit.agents.memory.types import PROFILE_FIELD_PATHS, validate_profile_value


class ProfileService:
    def __init__(
        self,
        repo: MemoryRepository,
        operators: Any,
        *,
        auto_write_threshold: float = 0.85,
    ) -> None:
        self._repo = repo
        self._operators = operators
        self._auto_write_threshold = float(auto_write_threshold)

    async def get(self, user_id: str) -> ProfileSnapshotView:
        profile = await self._repo.get_profile(user_id) or await self._repo.ensure_profile(
            user_id
        )
        fields: dict[str, ProfileFieldView] = {}
        for path in PROFILE_FIELD_PATHS:
            raw = _field_dict(profile.profile, path)
            if raw is None:
                continue
            fields[path] = _field_view(raw)
        return ProfileSnapshotView(
            revision=profile.revision,
            fields=fields,
            updated_at=profile.updated_at,
        )

    async def extract_and_apply(self, user_id: str, current_message: str) -> ProfileApplyResult:
        """Run B on the current message only, then apply the host-validated fields."""
        if self._operators is None:
            return ProfileApplyResult(applied=False, snapshot=await self.get(user_id))
        current = await self._repo.get_profile(user_id) or await self._repo.ensure_profile(
            user_id
        )
        proposal = await self._operators.extract_profile(
            current_message, dict(current.profile or {})
        )
        return await self.apply_extraction(
            user_id, current_message=current_message, proposal=proposal
        )

    async def patch_manual(
        self,
        user_id: str,
        fields: Mapping[str, Any],
        *,
        expected_revision: int,
    ) -> ProfileSnapshotView:
        current = await self._repo.get_profile(user_id) or await self._repo.ensure_profile(
            user_id
        )
        profile = _deepcopy_profile(current.profile)
        tombstones = dict(current.tombstones or {})
        now = _now()
        for path, value in fields.items():
            if path not in PROFILE_FIELD_PATHS:
                raise ValueError(f"unknown profile field: {path}")
            cleaned = validate_profile_value(path, value)
            _set_path(profile, path, _profile_value(cleaned, "user_manual", "EXPLICIT_MANDATE", now))
            tombstones.pop(path, None)
        updated = await self._repo.cas_profile(
            user_id,
            expected_revision=expected_revision,
            profile=profile,
            tombstones=tombstones,
            mutation=MutationContext("USER", user_id, "UPDATE", "profile_manual", ()),
        )
        return await self.get(user_id) if updated.revision >= 0 else await self.get(user_id)

    async def clear_field(
        self, user_id: str, field_path: str, *, expected_revision: int | None = None
    ) -> ProfileSnapshotView:
        if field_path not in PROFILE_FIELD_PATHS:
            raise ValueError(f"unknown profile field: {field_path}")
        current = await self._repo.get_profile(user_id) or await self._repo.ensure_profile(
            user_id
        )
        profile = _deepcopy_profile(current.profile)
        tombstones = dict(current.tombstones or {})
        _delete_path(profile, field_path)
        tombstones[field_path] = {"cleared_at": _now().isoformat()}
        await self._repo.cas_profile(
            user_id,
            expected_revision=expected_revision
            if expected_revision is not None
            else current.revision,
            profile=profile,
            tombstones=tombstones,
            mutation=MutationContext("USER", user_id, "DELETE", "profile_clear", ()),
        )
        return await self.get(user_id)

    async def apply_extraction(
        self,
        user_id: str,
        *,
        current_message: str,
        proposal: ProfileProposal,
    ) -> ProfileApplyResult:
        current = await self._repo.get_profile(user_id) or await self._repo.ensure_profile(
            user_id
        )
        snapshot = await self.get(user_id)
        if not proposal.fields:
            return ProfileApplyResult(applied=False, snapshot=snapshot)

        message = _normalize(current_message)
        pending: dict[str, ProfileFieldView] = {}
        for path, value in proposal.fields.items():
            if path not in PROFILE_FIELD_PATHS:
                return ProfileApplyResult(applied=False, snapshot=snapshot)
            if _normalize(value.evidence_quote) not in message:
                return ProfileApplyResult(applied=False, snapshot=snapshot)
            try:
                cleaned = validate_profile_value(path, value.value)
            except ValueError:
                return ProfileApplyResult(applied=False, snapshot=snapshot)
            confidence = confidence_for_tier(value.evidence_tier)
            if confidence < self._auto_write_threshold or not is_profile_confidence(confidence):
                existing = snapshot.fields.get(path)
                conflict = await self._repo.write_conflict(
                    user_id,
                    kind="profile",
                    field_path=path,
                    current_snapshot=existing.model_dump() if existing else {},
                    candidate_snapshot={
                        "value": cleaned,
                        "source": "conversation_extracted",
                        "evidence_tier": value.evidence_tier,
                        "confidence": confidence,
                        "evidence_quote": value.evidence_quote,
                    },
                    evidence_refs=[value.evidence_quote],
                )
                return ProfileApplyResult(
                    applied=False, snapshot=snapshot, conflict_id=conflict.id
                )
            pending[path] = ProfileFieldView(
                value=cleaned,
                source="conversation_extracted",
                evidence_tier=value.evidence_tier,
                confidence=confidence,
                observed_at=_now(),
            )

        profile = _deepcopy_profile(current.profile)
        tombstones = dict(current.tombstones or {})
        for path, field in pending.items():
            _set_path(
                profile,
                path,
                {
                    "value": field.value,
                    "source": field.source,
                    "evidence_tier": field.evidence_tier,
                    "confidence": field.confidence,
                    "observed_at": field.observed_at.isoformat(),
                    "confirmed_at": None,
                    "tombstoned_at": None,
                },
            )
            tombstones.pop(path, None)
        try:
            await self._repo.cas_profile(
                user_id,
                expected_revision=current.revision,
                profile=profile,
                tombstones=tombstones,
                mutation=MutationContext(
                    "SYSTEM", "profile-worker", "UPDATE", "profile_extraction", ()
                ),
            )
        except MemoryConflictError:
            raise
        return ProfileApplyResult(applied=True, snapshot=await self.get(user_id))


def _profile_value(
    value: Any, source: str, evidence_tier: str, observed_at: datetime
) -> dict[str, Any]:
    return {
        "value": value,
        "source": source,
        "evidence_tier": evidence_tier,
        "confidence": confidence_for_tier(evidence_tier),
        "observed_at": observed_at.isoformat(),
        "confirmed_at": observed_at.isoformat() if source == "user_manual" else None,
        "tombstoned_at": None,
    }


def _field_view(raw: dict[str, Any]) -> ProfileFieldView:
    return ProfileFieldView(
        value=raw.get("value"),
        source=str(raw.get("source") or "conversation_extracted"),
        evidence_tier=raw.get("evidence_tier"),
        confidence=float(raw.get("confidence") or 0.0),
        observed_at=_parse_dt(raw.get("observed_at")),
        tombstoned_at=_parse_dt(raw.get("tombstoned_at"), required=False),
    )


def _parse_dt(value: Any, *, required: bool = True) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    if required:
        return _now()
    return None


def _field_dict(profile: dict[str, Any], path: str) -> dict[str, Any] | None:
    section, _, name = path.partition(".")
    if not name:
        value = profile.get(section)
    else:
        parent = profile.get(section)
        value = parent.get(name) if isinstance(parent, dict) else None
    return dict(value) if isinstance(value, dict) else None


def _set_path(profile: dict[str, Any], path: str, value: dict[str, Any]) -> None:
    section, _, name = path.partition(".")
    if not name:
        profile[section] = value
        return
    parent = profile.get(section)
    if not isinstance(parent, dict):
        parent = {}
        profile[section] = parent
    parent[name] = value


def _delete_path(profile: dict[str, Any], path: str) -> None:
    section, _, name = path.partition(".")
    if not name:
        profile.pop(section, None)
        return
    parent = profile.get(section)
    if isinstance(parent, dict):
        parent.pop(name, None)


def _deepcopy_profile(profile: dict[str, Any]) -> dict[str, Any]:
    import copy

    return copy.deepcopy(dict(profile or {}))


def _normalize(value: str) -> str:
    return " ".join(str(value).lower().split())


def _now() -> datetime:
    from agentkit.agents.memory.types import utc_now

    return utc_now()

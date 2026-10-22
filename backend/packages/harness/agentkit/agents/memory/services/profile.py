"""Bounded user profile service (M10 section 2.3)."""

from __future__ import annotations

import threading
from typing import Any, Literal
from uuid import uuid4

from agentkit.agents.memory.backend import MemoryBackend
from agentkit.agents.memory.context import ProfileContextBuilder
from agentkit.agents.memory.resolver import MemoryResolver
from agentkit.agents.memory.services.base import MemoryTypeService
from agentkit.privacy import safe_memory_evidence
from agentkit.agents.memory.types import (
    EXPLICIT_ONLY_FIELDS,
    PROFILE_FIELD_PATHS,
    MemoryConflict,
    MemoryConflictError,
    MemoryKind,
    MemoryKindSpec,
    MemoryProposal,
    MemoryQuery,
    MemoryRecord,
    MemoryScope,
    MemorySearchPage,
    ProfilePatch,
    ProfileField,
    ProfileSnapshot,
    UserProfile,
    ensure_utc,
    profile_field,
    set_profile_field,
    source_rank,
    utc_now,
    validate_profile_value,
)


class ProfileService(MemoryTypeService):
    kind = MemoryKind.PROFILE
    spec = MemoryKindSpec(
        kind=MemoryKind.PROFILE,
        display_name="User profile",
        schema_ref="UserProfile",
        searchable=False,
        injectable=True,
        evictable=False,
    )

    def __init__(
        self,
        backend: MemoryBackend,
        resolver: MemoryResolver,
        *,
        max_tokens: int = 800,
        token_counting: str = "char",
        ingestors: list | None = None,
    ) -> None:
        super().__init__(backend, resolver, ingestors=ingestors)
        self._lock = threading.RLock()
        self._max_tokens = max_tokens
        self._builder = ProfileContextBuilder(max_tokens=max_tokens, token_counting=token_counting)

    # -- reads --------------------------------------------------------------

    def get(self, scope: MemoryScope, record_id: str = "profile") -> MemoryRecord | None:
        if record_id != "profile":
            return None
        return self.backend.get_record(scope, self.kind, "profile")

    def snapshot(self, scope: MemoryScope) -> ProfileSnapshot | None:
        record = self.get(scope)
        if record is None:
            return None
        profile = _profile_from_record(record)
        rendered = self._builder.build(profile)
        return ProfileSnapshot(
            profile=profile,
            schema_version=profile.schema_version,
            revision=record.revision,
            token_count=rendered.token_count,
            truncated=rendered.truncated,
        )

    def search(self, scope: MemoryScope, query: MemoryQuery) -> MemorySearchPage:
        del scope, query
        return MemorySearchPage()

    # -- writes -------------------------------------------------------------

    def update(
        self,
        scope: MemoryScope,
        patch: ProfilePatch,
        *,
        expected_revision: int | None = None,
    ) -> ProfileSnapshot:
        if not patch.fields:
            snapshot = self.snapshot(scope)
            if snapshot is None:
                raise ValueError("profile does not exist yet")
            return snapshot
        if patch.source == "inferred":
            raise ValueError("inferred values are facts and must not be written to the profile")
        prepared: dict[str, Any] = {}
        for path, value in patch.fields.items():
            if path in EXPLICIT_ONLY_FIELDS and patch.source not in {
                "user_manual",
                "user_explicit_durable",
            }:
                raise ValueError(f"{path} only accepts explicit or manual values")
            prepared[path] = validate_profile_value(path, value)

        evidence = safe_memory_evidence(patch.evidence)
        with self._lock:
            for _attempt in range(3):
                current = self.get(scope)
                current_revision = current.revision if current is not None else 0
                if expected_revision is not None and current_revision != expected_revision:
                    raise MemoryConflictError(
                        "profile revision changed",
                        current_revision=current_revision,
                    )
                profile = _profile_from_record(current) if current is not None else UserProfile()
                metadata = dict(current.metadata) if current is not None else {}
                tombstones = dict(metadata.get("tombstones") or {})
                history = list(metadata.get("profile_history") or [])
                conflicts: list[MemoryConflict] = []
                now = utc_now()

                for path, value in prepared.items():
                    existing = profile_field(profile, path)
                    if existing is not None and _same_value(existing.value, value):
                        refreshed = existing.model_copy(
                            update={
                                "confidence": max(existing.confidence, patch.confidence),
                                "confirmed_at": now
                                if patch.source in {"user_manual", "user_confirmed"}
                                else existing.confirmed_at,
                                "source": patch.source
                                if source_rank(patch.source) >= source_rank(existing.source)
                                else existing.source,
                                "pinned": existing.pinned or patch.source == "user_manual",
                            }
                        )
                        profile = set_profile_field(profile, path, refreshed)
                        tombstones.pop(path, None)
                        continue

                    if existing is not None:
                        current_field_record = MemoryRecord(
                            id="profile",
                            user_id=scope.user_id,
                            kind=self.kind,
                            source=existing.source,
                            structured={"value": existing.value},
                        )
                        resolution = self.resolver.resolve(
                            self.kind,
                            scope,
                            current_field_record,
                            MemoryProposal(
                                id=f"profile:{path}",
                                kind=self.kind,
                                payload={"source": patch.source, "value": value},
                                evidence=patch.evidence or "",
                            ),
                        )
                        if resolution.action in {"reject", "conflict"}:
                            conflicts.append(
                                self._new_conflict(
                                    scope,
                                    field_path=path,
                                    current_id=current.id if current is not None else None,
                                    current_source=existing.source,
                                    candidate_source=patch.source,
                                    evidence=evidence,
                                )
                            )
                            continue

                    if existing is not None:
                        history.append(
                            {
                                "field_path": path,
                                "previous_value": existing.value,
                                "previous_source": existing.source,
                                "replaced_at": now.isoformat(),
                            }
                        )
                    field = ProfileField(
                        value=value,
                        source=patch.source,
                        confidence=patch.confidence,
                        observed_at=now,
                        confirmed_at=now if patch.source == "user_confirmed" else None,
                        pinned=patch.source == "user_manual",
                    )
                    profile = set_profile_field(profile, path, field)
                    tombstones.pop(path, None)

                if not prepared and not conflicts:
                    return self.snapshot(scope)  # type: ignore[return-value]

                next_revision = (current.revision + 1) if current is not None else 1
                profile = profile.model_copy(
                    update={"revision": next_revision, "updated_at": now}
                )
                metadata["profile_history"] = history[-50:]
                metadata["tombstones"] = tombstones
                record = MemoryRecord(
                    id="profile",
                    user_id=scope.user_id,
                    kind=self.kind,
                    content="",
                    structured=profile.model_dump(mode="json"),
                    source=patch.source,
                    status="active",
                    revision=next_revision,
                    created_at=current.created_at if current is not None else now,
                    updated_at=now,
                    provenance={"evidence": evidence} if evidence else {},
                    metadata=metadata,
                )
                try:
                    stored = self.backend.put_record(
                        scope,
                        record,
                        expected_revision=current.revision if current is not None else None,
                    )
                except MemoryConflictError:
                    if expected_revision is not None:
                        raise
                    continue
                for conflict in conflicts:
                    self._store_conflict(scope, conflict)
                stored_profile = _profile_from_record(stored)
                rendered = self._builder.build(stored_profile)
                return ProfileSnapshot(
                    profile=stored_profile,
                    schema_version=stored_profile.schema_version,
                    revision=stored.revision,
                    token_count=rendered.token_count,
                    truncated=rendered.truncated,
                )
        raise MemoryConflictError("profile update kept losing the CAS race")

    def write(
        self,
        scope: MemoryScope,
        proposals: list[MemoryProposal],
        *,
        expected_revision: int | None = None,
    ) -> list[MemoryRecord]:
        written: list[MemoryRecord] = []
        for proposal in proposals:
            if proposal.kind != self.kind:
                continue
            if proposal.operation != "upsert":
                raise ValueError(f"profile does not support {proposal.operation} proposals")
            payload = dict(proposal.payload)
            fields = payload.pop("fields", None)
            if fields is None:
                field_path = payload.pop("field", None)
                if field_path is None:
                    raise ValueError("profile proposal must contain fields or field")
                fields = {field_path: payload.pop("value")}
            source = payload.pop("source", None)
            if source not in {"user_manual", "user_explicit_durable", "user_confirmed"}:
                raise ValueError("profile proposal is missing a resident source")
            snapshot = self.update(
                scope,
                ProfilePatch(
                    fields=fields,
                    source=source,
                    evidence=proposal.evidence,
                    confidence=proposal.confidence,
                ),
                expected_revision=expected_revision if len(proposals) == 1 else None,
            )
            record = self.get(scope)
            if record is not None:
                written.append(record)
            del snapshot
        return written

    def clear(self, scope: MemoryScope, *, field_path: str | None = None) -> None:
        with self._lock:
            current = self.get(scope)
            if current is None:
                return
            profile = _profile_from_record(current)
            metadata = dict(current.metadata)
            tombstones = dict(metadata.get("tombstones") or {})
            now = utc_now()
            if field_path is not None:
                if field_path not in PROFILE_FIELD_PATHS:
                    raise ValueError(f"unknown profile field: {field_path}")
                profile = set_profile_field(profile, field_path, None)
                tombstones[field_path] = {
                    "cleared_at": now.isoformat(),
                    "source": "user_manual",
                }
            else:
                for path in list(tombstones):
                    tombstones.pop(path)
                profile = UserProfile()
            revision = current.revision + 1
            profile = profile.model_copy(update={"revision": revision, "updated_at": now})
            metadata["tombstones"] = tombstones
            record = current.model_copy(
                update={
                    "structured": profile.model_dump(mode="json"),
                    "revision": revision,
                    "updated_at": now,
                    "source": "user_manual",
                    "metadata": metadata,
                }
            )
            self.backend.put_record(scope, record, expected_revision=current.revision)

    def delete(
        self,
        scope: MemoryScope,
        record_id: str,
        *,
        mode: Literal["soft", "hard"] = "soft",
        reason: str = "user_forget",
    ) -> None:
        del mode, reason
        if record_id != "profile":
            return
        self.clear(scope)

    # -- conflicts ----------------------------------------------------------

    def _new_conflict(
        self,
        scope: MemoryScope,
        *,
        field_path: str,
        current_id: str | None,
        current_source: str,
        candidate_source: str,
        evidence: str | None,
    ) -> MemoryConflict:
        return MemoryConflict(
            id=f"conflict_{uuid4().hex}",
            user_id=scope.user_id,
            kind=self.kind,
            field_path=field_path,
            current_record_id=current_id,
            candidate_record_id=None,
            current_source=current_source,
            candidate_source=candidate_source,
            evidence=[evidence] if evidence else [],
        )

    def _store_conflict(self, scope: MemoryScope, conflict: MemoryConflict) -> None:
        now = conflict.created_at
        record = MemoryRecord(
            id=conflict.id,
            user_id=scope.user_id,
            kind="conflict",
            subtype=conflict.kind,
            structured=conflict.model_dump(mode="json"),
            source="system",
            status="active",
            created_at=now,
            updated_at=now,
        )
        self.backend.put_record(scope, record)


def _profile_from_record(record: MemoryRecord) -> UserProfile:
    profile = UserProfile.model_validate(record.structured)
    if profile.revision != record.revision:
        profile = profile.model_copy(update={"revision": record.revision})
    return profile


def _same_value(left: Any, right: Any) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return " ".join(left.lower().split()) == " ".join(right.lower().split())
    if isinstance(left, list) and isinstance(right, list):
        return [str(item).lower() for item in left] == [str(item).lower() for item in right]
    return left == right



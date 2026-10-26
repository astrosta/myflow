"""Host-gated skill candidate discovery and existing-skill bridge.

LangMem D only drafts a package.  The host decides whether a cluster is the
same task, validates the package statically, and requires an explicit user
confirmation before calling the existing skill authoring service.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Sequence

from agentkit.agents.memory.canonical.domain import (
    MemoryConflictError,
    MemoryNotFoundError,
    MemoryRecordView,
    SkillCandidateView,
    SkillConfirmResult,
)
from agentkit.agents.memory.canonical.proposals import DehydratedEpisode, SkillProposal
from agentkit.agents.memory.canonical.repository import MemoryRepository
from agentkit.config.memory_config import MemoryConfig
from agentkit.config.skills_config import SkillCreationConfig
from agentkit.skills.domain import InvalidSkillPackage, StagedFile
from agentkit.skills.package import build_package


class SkillCandidateService:
    def __init__(
        self,
        repo: MemoryRepository,
        operators: Any,
        embedder: Any,
        skill_author: Any,
        config: MemoryConfig,
        *,
        creation: SkillCreationConfig | None = None,
    ) -> None:
        self._repo = repo
        self._operators = operators
        self._embedder = embedder
        self._skill_author = skill_author
        self._config = config
        self._creation = creation or SkillCreationConfig()

    async def generate(
        self,
        user_id: str,
        *,
        explicit_episode_ids: Sequence[str] | None = None,
        explicit_user_request: bool = False,
    ) -> list[SkillCandidateView]:
        episodes = await self._load_episodes(user_id, explicit_episode_ids)
        successful = [record for record in episodes if _is_success(record)]
        if not successful:
            return []
        groups: dict[tuple[str, str], list[MemoryRecordView]] = {}
        for record in successful:
            payload = record.payload
            task_class = str(payload.get("task_class") or "").strip()
            environment = json.dumps(payload.get("environment") or {}, sort_keys=True)
            if not task_class:
                continue
            groups.setdefault((task_class, environment), []).append(record)

        candidates: list[SkillCandidateView] = []
        for group in groups.values():
            if len(group) < (1 if explicit_user_request else self._config.skill_candidates.min_source_episodes):
                continue
            threads = {record.thread_id or record.id for record in group}
            if not explicit_user_request and len(threads) < self._config.skill_candidates.min_distinct_threads:
                continue
            success_rate = len([record for record in group if _is_success(record)]) / len(group)
            if success_rate < self._config.skill_candidates.min_success_rate:
                continue
            trigger_similarity = await self._trigger_similarity(group)
            if trigger_similarity < self._config.skill_candidates.trigger_similarity_threshold:
                continue
            common_steps = set(
                group[0].payload.get("key_steps") or []
            ).intersection(*(set(item.payload.get("key_steps") or []) for item in group[1:])) if len(group) > 1 else set(group[0].payload.get("key_steps") or [])
            if len(common_steps) < self._config.skill_candidates.min_common_steps:
                continue
            proposal = await self._operators.propose_skill("", _episodes(group))
            if not isinstance(proposal, SkillProposal) or not proposal.same_sop:
                continue
            candidate = await self._create_candidate(user_id, group, proposal)
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    async def _load_episodes(
        self, user_id: str, explicit_ids: Sequence[str] | None
    ) -> list[MemoryRecordView]:
        if explicit_ids:
            records = []
            for record_id in explicit_ids:
                record = await self._repo.get_record(user_id, record_id)
                if record is not None and record.kind == "episodic":
                    records.append(record)
            return records
        return await self._repo.list_records(
            user_id, kind="episodic", statuses=["active"], limit=1000
        )

    async def _trigger_similarity(self, group: Sequence[MemoryRecordView]) -> float:
        triggers = [str(record.payload.get("trigger_text") or record.content) for record in group]
        if len(triggers) <= 1:
            return 1.0
        try:
            vectors = await self._embedder.embed_documents(triggers)
        except Exception:
            vectors = []
        if len(vectors) == len(triggers):
            pairs = []
            for index in range(1, len(vectors)):
                pairs.append(_cosine(vectors[0], vectors[index]))
            return sum(pairs) / len(pairs) if pairs else 1.0
        pairs = [
            SequenceMatcher(None, triggers[0], trigger).ratio()
            for trigger in triggers[1:]
        ]
        return sum(pairs) / len(pairs) if pairs else 1.0

    async def _create_candidate(
        self,
        user_id: str,
        group: Sequence[MemoryRecordView],
        proposal: SkillProposal,
    ) -> SkillCandidateView | None:
        files = dict(proposal.files or {})
        if "SKILL.md" not in files and proposal.markdown:
            files["SKILL.md"] = proposal.markdown
        staged = {
            path: StagedFile(path=path, content=content.encode("utf-8"), mode="text")
            for path, content in files.items()
        }
        try:
            manifest = build_package(
                staged,
                max_files=self._creation.max_files,
                max_file_bytes=self._creation.max_file_bytes,
                max_package_bytes=self._creation.max_package_bytes,
            )
        except (InvalidSkillPackage, ValueError):
            return None
        ids = sorted(record.id for record in group)
        key = hashlib.sha256(
            (user_id + "|" + "|".join(ids)).encode("utf-8")
        ).hexdigest()
        snapshot = {
            "files": {path: base64.b64encode(file.content).decode("ascii") for path, file in staged.items()},
            "markdown": proposal.markdown,
        }
        return await self._repo.create_skill_candidate(
            user_id,
            private_name=manifest.parsed.name,
            description=manifest.parsed.description,
            trigger_text=str(group[0].payload.get("trigger_text") or group[0].content),
            package_snapshot=snapshot,
            package_sha256=manifest.package_sha256,
            skill_md_sha256=manifest.skill_md_sha256,
            source_episode_ids=ids,
            origin_idempotency_key=key,
            validation_report={"valid": True},
        )

    async def list(self, user_id: str, *, status: str | None = None) -> list[SkillCandidateView]:
        return await self._repo.list_skill_candidates(user_id, status=status)

    async def confirm(
        self,
        user_id: str,
        candidate_id: str,
        *,
        expected_revision: int,
        package_sha256: str,
        idempotency_key: str,
    ) -> SkillConfirmResult:
        candidate = await self._repo.get_skill_candidate(user_id, candidate_id)
        if candidate is None:
            raise MemoryNotFoundError(f"skill candidate {candidate_id} not found")
        if candidate.status == "saved" and candidate.skill_id and candidate.skill_version_id:
            return SkillConfirmResult(
                candidate.id, candidate.skill_id, candidate.skill_version_id, candidate.id
            )
        if candidate.package_sha256 != package_sha256:
            raise MemoryConflictError("skill candidate package hash changed")
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        if candidate.status != "pending_confirmation":
            raise MemoryConflictError("skill candidate is not pending confirmation")
        creating = await self._repo.update_skill_candidate(
            user_id,
            candidate.id,
            expected_revision=expected_revision,
            changes={"status": "creating"},
        )
        try:
            skill_id = creating.skill_id
            if not skill_id:
                skill = await self._skill_author.create_draft(
                    owner_user_id=user_id,
                    name=creating.private_name,
                    description=creating.description or "",
                )
                skill_id = getattr(skill, "id", None) or getattr(skill, "skill_id")
            for path, encoded in (creating.package_snapshot.get("files") or {}).items():
                await self._skill_author.put_file(
                    owner_user_id=user_id,
                    skill_id=skill_id,
                    path=path,
                    content=base64.b64decode(encoded),
                    mode="text",
                )
            version = await self._skill_author.save(
                owner_user_id=user_id,
                skill_id=skill_id,
                extra_metadata={"origin_candidate_id": creating.id},
            )
            version_id = getattr(version, "id", None) or getattr(version, "version_id")
            saved = await self._repo.update_skill_candidate(
                user_id,
                creating.id,
                expected_revision=creating.revision,
                changes={"status": "saved", "skill_id": skill_id, "skill_version_id": version_id},
            )
            return SkillConfirmResult(saved.id, skill_id, version_id, saved.id)
        except Exception:
            try:
                await self._repo.update_skill_candidate(
                    user_id,
                    creating.id,
                    expected_revision=creating.revision,
                    changes={"status": "failed"},
                )
            except Exception:
                pass
            raise

    async def reject(
        self, user_id: str, candidate_id: str, *, expected_revision: int, reason: str
    ) -> SkillCandidateView:
        return await self._repo.update_skill_candidate(
            user_id,
            candidate_id,
            expected_revision=expected_revision,
            changes={"status": "rejected", "rejected_reason": reason},
        )


def _is_success(record: MemoryRecordView) -> bool:
    payload = record.payload
    outcome = str(payload.get("final_outcome") or "").upper()
    feedback = str(payload.get("feedback_signal") or "").upper()
    if bool(payload.get("blocker")):
        return False
    return outcome == "SUCCESS" or (outcome == "PARTIAL" and feedback == "ACCEPTED")


def _episodes(group: Sequence[MemoryRecordView]) -> list[DehydratedEpisode]:
    episodes: list[DehydratedEpisode] = []
    for record in group:
        try:
            episodes.append(DehydratedEpisode.model_validate(record.payload))
        except Exception:
            continue
    return episodes


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


def _now() -> datetime:
    from agentkit.agents.memory.types import utc_now

    return utc_now()

"""把每一张 ORM 表登记进 `Base.metadata` 的**唯一**入口。

`create_all`、alembic `autogenerate` 和 0001_baseline 都只看 `Base.metadata`：
漏 import 一个模型模块，那张表就**建不出来**，而且没有任何报错——
CI 的「空 diff」测试照样绿，上线才 500 `no such table`。

所以这里**显式列出每一个模型模块**，而不是指望"别处总会 import 到它"。
新加一张表的唯一动作：在上面加一行 import，在这里加进 `__all__`。

M4 之前这份清单住在 `persistence/models/__init__.py`；行定义已经跟着各自的仓储
搬进 `thread_meta/model.py`、`run/model.py`、`events/model.py`，这里只留清单。
"""

from __future__ import annotations

from agentkit.agents.memory.openviking.models import (
    MemoryCaptureDedupRow,
    MemoryProfileDocumentRow,
    MemoryProfileOnboardingRow,
    MemoryProfileSuggestionRow,
    MemorySyncStateRow,
)
from agentkit.agents.memory.canonical.models import (
    MemoryConflictRow,
    MemoryConsolidationJobRow,
    MemoryEpisodeSessionRow,
    MemoryIndexOutboxRow,
    MemoryIngestJobRow,
    MemoryMutationLedgerRow,
    MemoryProfileRow,
    MemoryProjectionStateRow,
    MemoryRecordRow,
    MemoryRecordVersionRow,
    MemoryScopeMetaRow,
    MemorySkillCandidateRow,
)
from agentkit.knowledge.store.model import KnowledgeBaseRow, KnowledgeDocumentRow
from agentkit.persistence.base import Base
from agentkit.persistence.events.model import RunEventRow
from agentkit.persistence.run.model import RunRow
from agentkit.persistence.thread_meta.model import ThreadMetaRow
from agentkit.skills.store.model import (
    SkillAuditEventRow,
    SkillCatalogEpochRow,
    SkillDraftFileRow,
    SkillIndexOutboxRow,
    SkillPrivateActiveRow,
    SkillPublicActiveRow,
    SkillReleaseBatchRow,
    SkillReleaseRow,
    SkillRow,
    SkillSettingsRow,
    SkillSubmissionRow,
    SkillVersionRow,
)

#: Retained in metadata only for migration downgrade/tests. Production
#: bootstrap excludes these tables; migration 0009 drops them from existing
#: databases.
LEGACY_CANONICAL_MEMORY_TABLES = frozenset(
    {
        "memory_profiles",
        "memory_records",
        "memory_record_versions",
        "memory_episode_sessions",
        "memory_ingest_jobs",
        "memory_consolidation_jobs",
        "memory_skill_candidates",
        "memory_conflicts",
        "memory_mutation_ledger",
        "memory_index_outbox",
        "memory_scope_meta",
        "memory_projection_state",
    }
)

__all__ = [
    "Base",
    "LEGACY_CANONICAL_MEMORY_TABLES",
    "KnowledgeBaseRow",
    "KnowledgeDocumentRow",
    "MemoryCaptureDedupRow",
    "MemoryConflictRow",
    "MemoryConsolidationJobRow",
    "MemoryProfileDocumentRow",
    "MemoryProfileOnboardingRow",
    "MemoryProfileSuggestionRow",
    "MemorySyncStateRow",
    "MemoryEpisodeSessionRow",
    "MemoryIndexOutboxRow",
    "MemoryIngestJobRow",
    "MemoryMutationLedgerRow",
    "MemoryProfileRow",
    "MemoryProjectionStateRow",
    "MemoryRecordRow",
    "MemoryRecordVersionRow",
    "MemoryScopeMetaRow",
    "MemorySkillCandidateRow",
    "RunEventRow",
    "RunRow",
    "SkillAuditEventRow",
    "SkillCatalogEpochRow",
    "SkillDraftFileRow",
    "SkillIndexOutboxRow",
    "SkillPrivateActiveRow",
    "SkillPublicActiveRow",
    "SkillReleaseBatchRow",
    "SkillReleaseRow",
    "SkillRow",
    "SkillSettingsRow",
    "SkillSubmissionRow",
    "SkillVersionRow",
    "ThreadMetaRow",
]

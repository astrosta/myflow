"""把每一张 ORM 表登记进 `Base.metadata` 的**唯一**入口。

`create_all`、alembic `autogenerate` 和 0001_baseline 都只看 `Base.metadata`：
漏 import 一个模型模块，那张表就**建不出来**，而且没有任何报错——
CI 的「空 diff」测试照样绿，上线才 500 `no such table`。

所以这里**显式列出每一个模型模块**，而不是指望"别处总会 import 到它"。
新加一张表的唯一动作：在上面加一行 import，在这里加进 `__all__`。
"""

from __future__ import annotations

from agentkit.agents.memory.openviking.models import (
    MemoryCaptureDedupRow,
    MemoryProfileDocumentRow,
    MemoryProfileOnboardingRow,
    MemorySyncStateRow,
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
    SkillRow,
    SkillSecurityScanRow,
    SkillVersionRow,
)

__all__ = [
    "Base",
    "KnowledgeBaseRow",
    "KnowledgeDocumentRow",
    "MemoryCaptureDedupRow",
    "MemoryProfileDocumentRow",
    "MemoryProfileOnboardingRow",
    "MemorySyncStateRow",
    "RunEventRow",
    "RunRow",
    "SkillAuditEventRow",
    "SkillCatalogEpochRow",
    "SkillDraftFileRow",
    "SkillIndexOutboxRow",
    "SkillPrivateActiveRow",
    "SkillRow",
    "SkillSecurityScanRow",
    "SkillVersionRow",
    "ThreadMetaRow",
]

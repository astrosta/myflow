"""Rescue messages that are about to be compacted (M10 section 2.6.1)."""

from __future__ import annotations

import logging
from typing import Any

from agentkit.agents.memory.manager import MemoryManager
from agentkit.agents.memory.types import MemoryScope
from agentkit.utils.messages import INJECTED_USER_MESSAGE_ID_SUFFIX

logger = logging.getLogger(__name__)


class MemorySummarizationHook:
    """Queue the compacted window before summarization drops it from the checkpoint."""

    def __init__(
        self,
        manager: MemoryManager,
        *,
        user_id: str | None,
        thread_id: str | None = None,
        agent_name: str | None = None,
        skip_memory_flush: bool = False,
    ) -> None:
        self._manager = manager
        self._user_id = user_id
        self._thread_id = thread_id
        self._agent_name = agent_name
        self._skip_memory_flush = skip_memory_flush

    def __call__(self, messages: list[Any]) -> None:
        if self._skip_memory_flush or not self._user_id or not self._manager.ingestors:
            return
        visible = [
            message
            for message in messages
            if not str(getattr(message, "id", "")).endswith(INJECTED_USER_MESSAGE_ID_SUFFIX)
        ]
        if not visible:
            return
        try:
            self._manager.ingest(
                MemoryScope(
                    user_id=self._user_id,
                    agent_name=self._agent_name,
                    thread_id=self._thread_id,
                ),
                visible,
            )
        except Exception:
            logger.warning("Failed to queue compacted messages for memory extraction", exc_info=True)

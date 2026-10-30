"""Async profile-to-system-prompt provider used during agent assembly.

The provider is process-local and configured by the gateway lifespan. It is
deliberately separate from the capture stream: profile rendering happens once
when the graph is assembled, not as a hidden HumanMessage each turn.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

ProfilePromptProvider = Callable[[str], Awaitable[str | None]]
_PROVIDER: ProfilePromptProvider | None = None
_TIMEOUT_SECONDS = 1.0


def configure_profile_prompt_provider(provider: ProfilePromptProvider | None) -> None:
    global _PROVIDER
    _PROVIDER = provider


async def load_profile_prompt(user_id: str, *, timeout: float = _TIMEOUT_SECONDS) -> str | None:
    provider = _PROVIDER
    if provider is None or not user_id:
        return None
    try:
        return await asyncio.wait_for(provider(user_id), timeout=timeout)
    except Exception:
        logger.warning("Profile prompt assembly failed; continuing without it", exc_info=True)
        return None


__all__ = [
    "ProfilePromptProvider",
    "configure_profile_prompt_provider",
    "load_profile_prompt",
]

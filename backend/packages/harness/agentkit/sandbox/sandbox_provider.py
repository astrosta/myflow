"""SandboxProvider ABC and the process-wide singleton.

``reset`` clears cached state so the next ``acquire`` takes effect. It does not
promise to release sandboxes that are still in use. ``shutdown`` destroys them.
A remote ``reset`` that deleted containers would kill work owned by other processes.
"""

from __future__ import annotations

import asyncio
import threading
from abc import ABC, abstractmethod

from agentkit.sandbox.sandbox import Sandbox

_default_provider: SandboxProvider | None = None
_provider_lock = threading.Lock()


class SandboxProvider(ABC):
    uses_thread_data_mounts: bool = False

    @abstractmethod
    def acquire(self, thread_id: str | None = None, *, user_id: str | None = None) -> str: ...

    async def acquire_async(self, thread_id: str | None = None, *, user_id: str | None = None) -> str:
        """Async callers must use this. The default offloads ``acquire`` to a worker thread.

        Docker ``acquire`` creates a container and polls readiness. Calling the sync
        method on the event loop stalls every concurrent session.
        """
        return await asyncio.to_thread(self.acquire, thread_id, user_id=user_id)

    @abstractmethod
    def get(self, sandbox_id: str) -> Sandbox | None:
        """Memory lookup only. Ownership and health are decided in ``acquire`` and cached."""

    @abstractmethod
    def release(self, sandbox_id: str) -> None: ...

    def startup_check(self) -> None:
        """Validate configuration and dependencies before the gateway serves traffic.

        The lifespan calls this once, after construction and before the first request.
        It must not create or touch a sandbox: a remote provider only pings its health
        endpoint and mount sources here, the first sandbox still comes from
        ``acquire``. Raising aborts startup on purpose—半启动的 provider 比不启动更难排查。
        """
        return None

    async def release_async(self, sandbox_id: str) -> None:
        """Async callers must use this. The default offloads ``release`` to a worker thread.

        A remote release pauses or kills a container. Running that on the event loop
        stalls every concurrent session, the same reason ``acquire_async`` exists.
        """
        await asyncio.to_thread(self.release, sandbox_id)

    def reset(self) -> None:
        """Clear cross-call cache state. Does not guarantee release of live sandboxes."""
        return None

    def shutdown(self) -> None:
        """Destroy the provider and release every sandbox it still holds."""
        return None


def get_sandbox_provider(**kwargs: object) -> SandboxProvider:
    """Return the process singleton. The lock covers only the global reference swap."""
    global _default_provider
    with _provider_lock:
        if _default_provider is not None:
            return _default_provider

    from agentkit.config import get_app_config
    from agentkit.reflection import resolve_class
    from agentkit.sandbox.security import sandbox_use

    # The provider class must come from the same config the caller hands to the
    # constructor. Reading the ambient config here would build the process-wide
    # "use: ..." provider while giving it someone else's settings, which is how a
    # test that passes its own app_config ends up with a remote provider.
    config = kwargs.get("app_config")
    if config is None:
        try:
            config = get_app_config()
        except Exception:
            config = None
    try:
        use = sandbox_use(config)
    except Exception:
        use = "agentkit.sandbox.local:LocalSandboxProvider"
    provider = resolve_class(use, SandboxProvider)(**kwargs)

    with _provider_lock:
        if _default_provider is None:
            _default_provider = provider
            return provider
        winner = _default_provider

    if hasattr(provider, "shutdown"):
        provider.shutdown()
    return winner


def reset_sandbox_provider() -> None:
    """Test helper: drop the singleton so the next call constructs a new provider."""
    global _default_provider
    with _provider_lock:
        current = _default_provider
        _default_provider = None
    if current is not None and hasattr(current, "shutdown"):
        current.shutdown()

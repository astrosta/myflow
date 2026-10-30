"""Best-effort process-local finalizers for a single run.

Run cancellation does not execute every graph middleware hook (notably
after_agent). Features that must close a run-scoped resource on both normal
completion and cancellation register a callback here; the run worker invokes
them from its finally before final persistence.

The registry is intentionally process-local: a run is owned by one worker task,
and callbacks only publish to a process-local Redis client/stream.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress

logger = logging.getLogger(__name__)

RunFinalizer = Callable[[str], Awaitable[None]]
_FINALIZERS: dict[str, list[RunFinalizer]] = {}
_DEFAULT_TIMEOUT_SECONDS = 2.0


def register_run_finalizer(run_id: str, finalizer: RunFinalizer) -> None:
    if not run_id:
        return
    callbacks = _FINALIZERS.setdefault(run_id, [])
    if finalizer not in callbacks:
        callbacks.append(finalizer)


def unregister_run_finalizer(run_id: str, finalizer: RunFinalizer) -> None:
    callbacks = _FINALIZERS.get(run_id)
    if not callbacks:
        return
    callbacks = [item for item in callbacks if item is not finalizer]
    if callbacks:
        _FINALIZERS[run_id] = callbacks
    else:
        _FINALIZERS.pop(run_id, None)


def clear_run_finalizers(run_id: str) -> None:
    """Drop callbacks for a run that never reached the worker finalizer."""

    _FINALIZERS.pop(run_id, None)


def pending_finalizers(run_id: str) -> int:
    return len(_FINALIZERS.get(run_id, ()))


async def finish_run_finalizers(
    run_id: str,
    *,
    status: str,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """Run callbacks without letting a failing callback mask the run outcome."""

    callbacks = _FINALIZERS.pop(run_id, [])
    for finalizer in callbacks:
        task = asyncio.create_task(finalizer(status))
        try:
            # shield keeps a finally-time Redis publish alive when the outer run
            # task is cancelled again by a disconnect.
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.CancelledError:
            continue
        except TimeoutError:
            task.cancel()
            with suppress(BaseException):
                await task
            logger.warning("Run finalizer timed out for %s", run_id)
        except Exception:
            logger.warning("Run finalizer failed for %s", run_id, exc_info=True)


__all__ = [
    "RunFinalizer",
    "clear_run_finalizers",
    "finish_run_finalizers",
    "pending_finalizers",
    "register_run_finalizer",
    "unregister_run_finalizer",
]

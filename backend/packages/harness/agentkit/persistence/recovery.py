"""Mark runs left pending or running by a dead process as error.

A restarted gateway does not resume ``astream``. It also does not load those
rows into ``RunManager``: a record without a task would look alive.
"""

from __future__ import annotations

import os

RESTART_ERROR = "interrupted: gateway restarted"


def gateway_worker_count() -> int:
    """How many gateway processes share this database. Unset means one."""
    raw = os.environ.get("GATEWAY_WORKERS", "1")
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"GATEWAY_WORKERS must be an integer, got {raw!r}") from exc


async def recover_inflight_runs(run_store, thread_meta, *, gateway_workers: int | None = None) -> int:
    """Write ``error`` for every inflight row. Returns how many rows changed.

    ``GATEWAY_WORKERS > 1`` must refuse: those rows may still be owned by a
    peer process. A raised error fails startup; a row that is already terminal
    is left alone by ``update_run_completion``.
    """
    workers = gateway_worker_count() if gateway_workers is None else gateway_workers
    if workers > 1:
        raise RuntimeError(
            "GATEWAY_WORKERS > 1 must not mark another worker's inflight runs as error"
        )
    recovered = 0
    for row in await run_store.list_inflight():
        changed = await run_store.update_run_completion(
            row["run_id"], status="error", error=RESTART_ERROR
        )
        if not changed:
            continue
        await thread_meta.update_status(row["thread_id"], "error", user_id=row.get("user_id"))
        recovered += 1
    return recovered

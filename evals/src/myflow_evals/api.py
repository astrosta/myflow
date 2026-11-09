"""Black-box HTTP client for the Gateway thread, run, and event APIs.

This module imports neither backend.app nor agentkit. Every request goes through
the per-account ``httpx.Client`` an ``AccountSession`` already owns, so the
auth cookie jar and the ``X-CSRF-Token`` header set at login travel with the
call. No Authorization header is built here.

The default run path is ``POST /threads/{thread_id}/runs`` plus polling
``GET /threads/{thread_id}/runs/{run_id}``; the SSE and blocking wait endpoints
are deliberately unused. Runs are always started with ``on_disconnect=continue``
so a dropped connection cannot cancel a run the evaluator still needs.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from myflow_evals.accounts import AccountSession

#: The Gateway serves one built-in assistant; a run request must name it.
_ASSISTANT_ID = "lead_agent"

#: Server-generated run trace id key (TRACE_METADATA_KEY in the backend).
_TRACE_METADATA_KEY = "myflow-trace_id"

#: Everything else is terminal: success, interrupted, error, timeout.
_PENDING_RUN_STATUSES = frozenset({"pending", "running"})

#: Tests shrink this; production trades latency for one request every second.
_POLL_INTERVAL_SECONDS = 1.0

#: A run whose worker died must not hang the evaluator forever.
_DEFAULT_WAIT_TIMEOUT_SECONDS = 900.0


@dataclass(frozen=True)
class RunSnapshot:
    """Terminal view of one run: its status, error, and run trace id."""

    run_id: str
    status: str
    error: str | None = None
    trace_id: str | None = None


class AgentApi:
    """Stateless Gateway calls; every method takes the session that owns the Client."""

    @staticmethod
    def create_thread(
        session: AccountSession,
        thread_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Create one thread. ``metadata`` is routing metadata, never credentials."""
        response = session.client.post(
            _data_path(session, "/threads"),
            json={"thread_id": thread_id, "metadata": metadata},
        )
        response.raise_for_status()

    @staticmethod
    def start_run(
        session: AccountSession,
        thread_id: str,
        query: str,
        metadata: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Start one run and return its opaque run id.

        ``metadata`` carries routing keys such as ``sample_id``; callers must not
        forward a whole Dataset item, a user id, or credentials in it.
        """
        response = session.client.post(
            _data_path(session, f"/threads/{thread_id}/runs"),
            json={
                "assistant_id": _ASSISTANT_ID,
                "input": {"messages": [{"type": "human", "content": query}]},
                "metadata": metadata,
                "context": context,
                "on_disconnect": "continue",
            },
        )
        response.raise_for_status()
        body = _json_object(response, f"run for thread {thread_id!r}")
        run_id = body.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise RuntimeError(f"run for thread {thread_id!r} did not return a run id")
        return run_id

    @staticmethod
    def wait_run(
        session: AccountSession,
        thread_id: str,
        run_id: str,
        timeout_seconds: float | None = None,
    ) -> RunSnapshot:
        """Poll until the run leaves pending/running or the deadline expires.

        ``timeout_seconds`` defaults to 900 seconds: ``None`` selects that default
        and ``float("inf")`` disables the bound. The deadline is measured on the
        monotonic clock. A terminal ``error``, ``timeout``, or ``interrupted``
        status is returned, not raised: the caller decides what a failed run means
        for the sample. When the deadline expires a ``RuntimeError`` names the run,
        its last observed status, and the elapsed budget. HTTP errors still propagate.
        """
        budget = (
            _DEFAULT_WAIT_TIMEOUT_SECONDS
            if timeout_seconds is None
            else float(timeout_seconds)
        )
        started_at = time.monotonic()
        path = _data_path(session, f"/threads/{thread_id}/runs/{run_id}")
        while True:
            response = session.client.get(path)
            response.raise_for_status()
            body = _json_object(response, f"run {run_id!r}")
            status = body.get("status")
            if not isinstance(status, str) or not status:
                raise RuntimeError(f"run {run_id!r} did not return a status")
            if status not in _PENDING_RUN_STATUSES:
                return _run_snapshot(body, status=status, requested_run_id=run_id)
            elapsed = time.monotonic() - started_at
            if elapsed >= budget:
                raise RuntimeError(
                    f"run {run_id!r} was still {status!r} after {elapsed:.1f}s "
                    f"(budget {budget:g}s)"
                )
            time.sleep(_POLL_INTERVAL_SECONDS)

    @staticmethod
    def list_events(
        session: AccountSession,
        thread_id: str,
        run_id: str,
        after_seq: int | None = None,
        event_types: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Read one run's event ledger, following pagination to completion."""
        path = _data_path(session, f"/threads/{thread_id}/runs/{run_id}/events")
        rows: list[dict[str, Any]] = []
        cursor = after_seq
        while True:
            params: dict[str, Any] = {}
            if cursor is not None:
                params["after_seq"] = cursor
            if event_types:
                params["event_types"] = ",".join(event_types)
            response = session.client.get(path, params=params)
            response.raise_for_status()
            body = _json_object(response, f"events for run {run_id!r}")
            page = body.get("data")
            if not isinstance(page, list):
                raise RuntimeError(f"events for run {run_id!r} did not return a data list")
            for row in page:
                if not isinstance(row, Mapping):
                    raise RuntimeError(
                        f"events for run {run_id!r} returned a non-object row"
                    )
                rows.append(dict(row))
            if not body.get("has_more"):
                return rows
            next_cursor = body.get("next_cursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                raise RuntimeError(
                    f"events for run {run_id!r} reported has_more without a next_cursor"
                )
            try:
                advanced = int(next_cursor)
            except ValueError:
                raise RuntimeError(
                    f"events for run {run_id!r} returned a non-integer next_cursor"
                ) from None
            if cursor is not None and advanced <= cursor:
                raise RuntimeError(
                    f"events for run {run_id!r} returned next_cursor {advanced} "
                    f"that did not advance past {cursor}"
                )
            cursor = advanced


def _data_path(session: AccountSession, suffix: str) -> str:
    """Data routes live under /api (not /api/v1); match the AGENT_API base spelling."""
    base = str(session.client.base_url).rstrip("/")
    prefix = "" if base.endswith("/api") else "/api"
    return f"{prefix}{suffix}"


def _run_snapshot(
    body: Mapping[str, Any], *, status: str, requested_run_id: str
) -> RunSnapshot:
    response_run_id = body.get("run_id")
    error = body.get("error")
    return RunSnapshot(
        run_id=(
            response_run_id
            if isinstance(response_run_id, str) and response_run_id
            else requested_run_id
        ),
        status=status,
        error=error if isinstance(error, str) else None,
        trace_id=_trace_id(body.get("metadata")),
    )


def _trace_id(metadata: object) -> str | None:
    if not isinstance(metadata, Mapping):
        return None
    value = metadata.get(_TRACE_METADATA_KEY)
    return value if isinstance(value, str) and value else None


def _json_object(response: httpx.Response, what: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        raise RuntimeError(f"{what} returned a non-JSON response") from None
    if not isinstance(body, dict):
        raise RuntimeError(f"{what} returned a non-object JSON response")
    return body

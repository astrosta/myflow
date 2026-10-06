"""Public run JSON used by cancel(wait=true)."""

from datetime import UTC, datetime

from agentkit.runtime.runs import DisconnectMode, RunRecord, RunStatus
from backend.app.gateway.run_models import run_to_wire


def test_run_to_wire_keeps_the_public_run_and_drops_process_fields() -> None:
    created = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    record = RunRecord(
        run_id="run-1",
        thread_id="thread-1",
        assistant_id="lead",
        status=RunStatus.interrupted,
        on_disconnect=DisconnectMode.cancel,
        metadata={"topic": "中文"},
        kwargs={"input": {"messages": ["hi"]}},
        user_id="alice",
        error="cancelled",
        created_at=created.isoformat(),
        updated_at=created,
        multitask_strategy="interrupt",
    )

    body = run_to_wire(record).model_dump()

    assert body == {
        "run_id": "run-1",
        "thread_id": "thread-1",
        "assistant_id": "lead",
        "status": "interrupted",
        "created_at": "2026-10-01T12:00:00+00:00",
        "updated_at": "2026-10-01T12:00:00Z",
        "metadata": {"topic": "中文"},
        "kwargs": {"input": {"messages": ["hi"]}},
        "multitask_strategy": "interrupt",
        "on_disconnect": "cancel",
        "model_name": None,
        "error": "cancelled",
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "llm_call_count": 0,
    }
    assert "user_id" not in body
    assert "task" not in body
    body["metadata"]["topic"] = "changed"
    assert record.metadata["topic"] == "中文"

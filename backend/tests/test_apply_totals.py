"""Progress and completion copy token totals onto the live run record."""

from agentkit.runtime.runs.manager import RunRecord, _apply_totals
from agentkit.runtime.runs.schemas import DisconnectMode, RunStatus
from agentkit.runtime.runs.totals import RunTotals


def test_apply_totals_copies_every_counter_and_the_model_buckets() -> None:
    record = RunRecord(
        run_id="run-1",
        thread_id="thread-1",
        assistant_id="lead",
        status=RunStatus.running,
        on_disconnect=DisconnectMode.cancel,
    )
    buckets = {"lead": {"input": 3, "output": 5}}
    totals = RunTotals(
        total_input_tokens=3,
        total_output_tokens=5,
        llm_call_count=1,
        lead_agent_tokens=8,
        token_usage_by_model=buckets,
    )

    _apply_totals(record, totals)
    buckets["lead"]["input"] = 99

    assert record.total_input_tokens == 3
    assert record.total_output_tokens == 5
    assert record.llm_call_count == 1
    assert record.lead_agent_tokens == 8
    assert record.subagent_tokens == 0
    assert record.middleware_tokens == 0
    assert record.token_usage_by_model == {"lead": {"input": 3, "output": 5}}

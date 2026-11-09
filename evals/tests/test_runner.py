"""Runner tests: completeness gate, compact evidence handoff, and metric gates.

Both dependencies are faked in memory. An httpx.MockTransport stands in for the
Gateway behind a real httpx.Client, so thread/run/event traffic is exercised
exactly as the client builds it. A tiny fake object stands in for Langfuse and
exposes only the surface runner.py uses (get_dataset -> object with .items,
run_experiment -> object with .item_results / .experiment_url), which is the same
surface the real SDK offers. No network, no sleeps, and no credential is ever
needed: the password below only proves that it never travels.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from langfuse import Evaluation

from myflow_evals.accounts import AccountCredential, AccountSession
from myflow_evals.api import AgentApi
from myflow_evals.evaluators import THRESHOLDS
from myflow_evals.evidence import RunEvidence
from myflow_evals.runner import (
    CI_SUBSET_SIZE,
    EvidenceStore,
    ExperimentConfigError,
    ExperimentRunner,
    GateResult,
    IncompleteExperiment,
    RuleMetricAdapter,
    aggregate_scores,
    ci_subset,
    evaluate_gates,
    select_items,
)

BASE_URL = "http://gateway.test/api"
CSRF_TOKEN = "csrf-token"
RAW_USER_ID = "11111111-1111-4111-8111-111111111111"
HASHED_USER_ID = "9f2c4b6d8e0a1c3e5f7a9b1d3c5e7f90"
OTHER_HASHED_USER_ID = "0123456789abcdef0123456789abcdef"
PASSWORD = "alice-password"
RUN_NAME = "ci-20261109"

CHUNK = {
    "citation_id": "cite-1",
    "chunk_id": "chunk-1",
    "document_id": "doc-1",
    "content": "a chunk body that must never reach Langfuse",
}
CITATION = {"citation_id": "cite-1", "chunk_id": "chunk-1", "document_id": "doc-1"}


def _credential() -> AccountCredential:
    return AccountCredential(label="analyst_a", username="alice", password=PASSWORD)


def _session(
    transport: httpx.BaseTransport, hashed_user_id: str = HASHED_USER_ID
) -> AccountSession:
    client = httpx.Client(
        base_url=BASE_URL,
        transport=transport,
        headers={"X-CSRF-Token": CSRF_TOKEN},
    )
    client.cookies.set("access_token", "access-token")
    return AccountSession(
        credential=_credential(),
        client=client,
        user_id=RAW_USER_ID,
        hashed_user_id=hashed_user_id,
    )


def _evidence(**overrides: Any) -> RunEvidence:
    fields: dict[str, Any] = {
        "answer": "",
        "tool_calls": [],
        "business_tool_calls": [],
        "subagent_calls": [],
        "retrieved_tools": [],
        "retrieved_skills": [],
        "loaded_skills": [],
        "kb_called": False,
        "retrieved_chunks": [],
        "citations": [],
        "trace_id": None,
    }
    fields.update(overrides)
    return RunEvidence(**fields)


def _messages(
    item_id: str,
    *,
    tools: Sequence[str] = (),
    subagents: Sequence[str] = (),
    citations: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """The checkpoint message list a finished run leaves behind for one item."""
    rows: list[dict[str, Any]] = [{"type": "human", "content": f"question for {item_id}"}]
    for index, tool in enumerate(tools):
        call_id = f"call-{item_id}-{index}"
        rows.append(
            {
                "type": "ai",
                "id": f"ai-{item_id}-{index}",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "name": "tool_execute",
                        "args": {"tool_name": tool, "arguments": {"project_id": "p-1"}},
                    }
                ],
            }
        )
        rows.append({"type": "tool", "tool_call_id": call_id, "content": "ok"})
    for index, subagent in enumerate(subagents):
        call_id = f"task-{item_id}-{index}"
        rows.append(
            {
                "type": "ai",
                "id": f"ai-{item_id}-task-{index}",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "name": "task",
                        "args": {"subagent_type": subagent, "description": "research"},
                    }
                ],
            }
        )
        rows.append({"type": "tool", "tool_call_id": call_id, "content": "done"})
    rows.append(
        {
            "type": "ai",
            "id": f"ai-{item_id}-final",
            "content": f"answer for {item_id}",
            "additional_kwargs": {"knowledge_citations": list(citations)},
        }
    )
    return rows


class FakeGateway:
    """A Gateway that serves one thread, run, ledger, and checkpoint per item."""

    def __init__(
        self,
        *,
        tools_by_item: Mapping[str, Sequence[str]] | None = None,
        subagents_by_item: Mapping[str, Sequence[str]] | None = None,
        citations_by_item: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
        chunks_by_item: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
        status_by_item: Mapping[str, str] | None = None,
        fail_runs_for: Sequence[str] = (),
    ) -> None:
        self.requests: list[httpx.Request] = []
        self._tools = dict(tools_by_item or {})
        self._subagents = dict(subagents_by_item or {})
        self._citations = dict(citations_by_item or {})
        self._chunks = dict(chunks_by_item or {})
        self._status = dict(status_by_item or {})
        self._fail_runs_for = set(fail_runs_for)
        self._threads: dict[str, str] = {}
        self._runs: dict[str, str] = {}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        parts = [part for part in request.url.path.split("/") if part]
        try:
            rest = parts[parts.index("threads") + 1 :]
        except ValueError:
            raise AssertionError(f"unexpected request path {request.url.path}") from None
        if request.method == "POST" and not rest:
            body = json.loads(request.content)
            thread_id = body["thread_id"]
            self._threads[thread_id] = body["metadata"]["eval_item_id"]
            return httpx.Response(201, json={"thread_id": thread_id, "status": "idle"})
        thread_id = rest[0]
        item_id = self._threads[thread_id]
        if request.method == "POST" and rest[1:] == ["runs"]:
            if item_id in self._fail_runs_for:
                return httpx.Response(500, json={"detail": "worker exploded"})
            run_id = f"run-{len(self._runs) + 1}"
            self._runs[run_id] = item_id
            return httpx.Response(
                202, json={"run_id": run_id, "thread_id": thread_id, "status": "pending"}
            )
        if rest[1:] == ["state"]:
            return httpx.Response(
                200,
                json={
                    "values": {
                        "messages": _messages(
                            item_id,
                            tools=self._tools.get(item_id, ()),
                            subagents=self._subagents.get(item_id, ()),
                            citations=self._citations.get(item_id, ()),
                        )
                    },
                    "checkpoint_id": "cp-1",
                },
            )
        run_id = rest[2]
        tail = rest[3:]
        if tail == ["events"]:
            chunks = self._chunks.get(item_id, [])
            rows = (
                [
                    {
                        "event_type": "retrieval.kb",
                        "run_id": run_id,
                        "content": {"items": [dict(chunk) for chunk in chunks]},
                    }
                ]
                if chunks
                else []
            )
            return httpx.Response(200, json={"data": rows, "has_more": False})
        if tail == []:
            status = self._status.get(item_id, "success")
            body: dict[str, Any] = {
                "run_id": run_id,
                "thread_id": thread_id,
                "status": status,
                "metadata": {"myflow-trace_id": f"trace-{item_id}"},
            }
            if status != "success":
                body["error"] = "worker exploded"
            return httpx.Response(200, json=body)
        raise AssertionError(f"unexpected request path {request.url.path}")


@dataclass
class FakeItem:
    id: str
    input: dict[str, Any]
    expected_output: dict[str, Any]
    metadata: Any


@dataclass
class FakeItemResult:
    item: FakeItem
    output: Any
    evaluations: list[Evaluation]
    trace_id: str | None = None


@dataclass
class FakeExperimentOutput:
    item_results: list[FakeItemResult]
    experiment_url: str | None = "https://langfuse.test/project/p/experiments/run"
    name: str = ""
    run_name: str = ""
    run_evaluations: list[Evaluation] = field(default_factory=list)


class FakeDataset:
    """The dataset surface the runner reads: .items plus the run bookkeeping."""

    def __init__(self, items: Sequence[FakeItem], *, drop: Sequence[str] = ()) -> None:
        self.items = list(items)
        self.calls: list[dict[str, Any]] = []
        self.outputs: dict[str, Any] = {}
        self.evaluations: dict[str, list[Evaluation]] = {}
        self.errors: list[tuple[str, Exception]] = []
        self._drop = set(drop)

    def run_items(
        self,
        data: Sequence[FakeItem],
        *,
        name: str,
        run_name: str | None = None,
        task: Any,
        evaluators: Sequence[Any] = (),
        max_concurrency: int = 50,
        metadata: Mapping[str, Any] | None = None,
    ) -> FakeExperimentOutput:
        self.calls.append(
            {
                "name": name,
                "run_name": run_name,
                "max_concurrency": max_concurrency,
                "metadata": dict(metadata or {}),
                "evaluator_names": [getattr(ev, "__name__", "?") for ev in evaluators],
            }
        )
        results: list[FakeItemResult] = []
        for item in data:
            try:
                output = task(item=item)
            except Exception as exc:  # the real SDK logs the failure and drops the item
                self.errors.append((item.id, exc))
                continue
            evaluations: list[Evaluation] = []
            for evaluator in evaluators:
                found = evaluator(
                    input=item.input,
                    output=output,
                    expected_output=item.expected_output,
                    metadata=item.metadata,
                )
                evaluations.extend(found or [])
            self.outputs[item.id] = output
            self.evaluations[item.id] = evaluations
            if item.id in self._drop:
                continue
            results.append(
                FakeItemResult(
                    item=item,
                    output=output,
                    evaluations=evaluations,
                    trace_id=output.get("trace_id"),
                )
            )
        return FakeExperimentOutput(item_results=results, name=name, run_name=run_name or "")


class FakeLangfuse:
    """The SDK surface the runner may use: get_dataset plus run_experiment(data=...).

    run_experiment requires the selected items as data= and refuses anything else,
    exactly like the real client, whose DatasetClient variant would silently
    execute every item of the dataset instead. A regression to that call shape
    therefore fails here instead of quietly running unselected samples.
    """

    def __init__(
        self,
        dataset: FakeDataset | None = None,
        *,
        expected_item_ids: Sequence[str] | None = None,
    ) -> None:
        self.dataset = dataset
        self.expected_item_ids = (
            None if expected_item_ids is None else list(expected_item_ids)
        )
        self.requested: list[str] = []
        self.data_ids: list[str] = []

    def get_dataset(self, name: str, **kwargs: Any) -> FakeDataset:
        self.requested.append(name)
        if self.dataset is None:
            raise AssertionError(f"no fake dataset configured for {name!r}")
        return self.dataset

    def run_experiment(
        self,
        *,
        name: str,
        run_name: str | None = None,
        data: Sequence[FakeItem],
        task: Any,
        evaluators: Sequence[Any] = (),
        max_concurrency: int = 50,
        metadata: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> FakeExperimentOutput:
        received = [item.id for item in data]
        expected = (
            self.expected_item_ids
            if self.expected_item_ids is not None
            else [item.id for item in (self.dataset.items if self.dataset else [])]
        )
        if received != expected:
            raise AssertionError(
                "run_experiment must execute exactly the selected items via data=; "
                f"got {received!r}, expected {expected!r}"
            )
        self.data_ids = received
        assert self.dataset is not None
        return self.dataset.run_items(
            data,
            name=name,
            run_name=run_name,
            task=task,
            evaluators=evaluators,
            max_concurrency=max_concurrency,
            metadata=metadata,
        )


def _item(
    item_id: str,
    *,
    user_id: Any = HASHED_USER_ID,
    category: str = "single_tool",
    expected_output: Mapping[str, Any] | None = None,
) -> FakeItem:
    return FakeItem(
        id=item_id,
        input={"query": f"question for {item_id}"},
        expected_output=dict(expected_output or {}),
        metadata={"user_id": user_id, "category": category},
    )


def _runner(
    gateway: FakeGateway,
    dataset: FakeDataset,
    *,
    store: EvidenceStore | None = None,
    sessions: Mapping[str, AccountSession] | None = None,
    expected_item_ids: Sequence[str] | None = None,
    langfuse: FakeLangfuse | None = None,
) -> ExperimentRunner:
    client = langfuse if langfuse is not None else FakeLangfuse(
        dataset, expected_item_ids=expected_item_ids
    )
    return ExperimentRunner(
        client,
        sessions=sessions or {HASHED_USER_ID: _session(gateway.transport())},
        api=AgentApi,
        store=store,
    )


def test_thread_state_returns_the_checkpoint_values() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "values": {"messages": [{"type": "ai", "content": "hi"}]},
                "checkpoint_id": "cp-1",
            },
        )

    state = AgentApi.thread_state(_session(httpx.MockTransport(handler)), "thread-1")

    assert state == {"messages": [{"type": "ai", "content": "hi"}]}
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/api/threads/thread-1/state"


def test_thread_state_returns_an_empty_mapping_when_values_are_absent() -> None:
    session = _session(httpx.MockTransport(lambda request: httpx.Response(200, json={})))

    assert AgentApi.thread_state(session, "thread-1") == {}


def test_thread_state_rejects_a_non_object_values_field() -> None:
    session = _session(
        httpx.MockTransport(lambda request: httpx.Response(200, json={"values": ["no"]}))
    )

    with pytest.raises(RuntimeError, match="values"):
        AgentApi.thread_state(session, "thread-1")


def test_evidence_store_keys_evidence_by_item_id_and_overwrites() -> None:
    store = EvidenceStore()
    first = _evidence(answer="first")
    second = _evidence(answer="second")

    evidence_id = store.put("item-1", first)

    assert evidence_id == "ev-" + hashlib.sha256(b"item-1").hexdigest()[:16]
    assert store.get(evidence_id) is first
    assert store.put("item-1", second) == evidence_id
    assert store.get(evidence_id) is second
    assert store.get("ev-unknown") is None


def _dataset_items(count: int, categories: Sequence[str]) -> list[FakeItem]:
    return [
        _item(f"item-{index:03d}", category=categories[index % len(categories)])
        for index in range(count)
    ]


def test_ci_subset_is_stratified_and_deterministic() -> None:
    items = _dataset_items(90, ["single_tool", "kb_single_hop", "skill_single"])

    chosen = ci_subset(items)
    reshuffled = ci_subset(list(reversed(items)))

    assert len(chosen) == CI_SUBSET_SIZE == 30
    assert [item.id for item in chosen] == [item.id for item in reshuffled]
    by_category: dict[str, list[str]] = {}
    for item in chosen:
        by_category.setdefault(item.metadata["category"], []).append(item.id)
    assert sorted(by_category) == ["kb_single_hop", "single_tool", "skill_single"]
    assert sorted(len(ids) for ids in by_category.values()) == [10, 10, 10]
    # Round-robin over the id-sorted groups: the first pick of each group leads.
    assert [item.id for item in chosen[:3]] == ["item-001", "item-000", "item-002"]


def test_ci_subset_keeps_every_item_when_the_dataset_is_smaller() -> None:
    items = _dataset_items(4, ["single_tool", "no_tool"])

    chosen = ci_subset(items)

    assert len(chosen) == 4
    assert [item.id for item in chosen] == [item.id for item in ci_subset(list(reversed(items)))]


def test_select_items_caps_full_mode_after_sorting_by_id() -> None:
    items = _dataset_items(5, ["single_tool"])

    chosen = select_items(list(reversed(items)), mode="full", max_items=3)

    assert [item.id for item in chosen] == ["item-000", "item-001", "item-002"]
    assert len(select_items(items, mode="full")) == 5
    assert len(select_items(items, mode="ci", max_items=2)) == 2


def test_run_raises_incomplete_experiment_naming_the_missing_item(
    capsys: pytest.CaptureFixture[str],
) -> None:
    gateway = FakeGateway()
    dataset = FakeDataset([_item("item-1"), _item("item-2"), _item("item-3")], drop=["item-3"])
    runner = _runner(gateway, dataset)

    with pytest.raises(IncompleteExperiment) as excinfo:
        runner.run("agent-eval/regression/v1", RUN_NAME, mode="ci")

    assert excinfo.value.missing_item_ids == ("item-3",)
    assert "item-3" in str(excinfo.value)
    assert capsys.readouterr().out == ""


def test_run_hands_langfuse_a_compact_output_and_keeps_evidence_in_the_store() -> None:
    items = [
        _item("item-1", expected_output={"expect_kb": True, "expected_doc_ids": ["doc-1"]}),
    ]
    gateway = FakeGateway(
        subagents_by_item={"item-1": ["knowledge-researcher"]},
        citations_by_item={"item-1": [CITATION]},
        chunks_by_item={"item-1": [CHUNK]},
    )
    dataset = FakeDataset(items)
    store = EvidenceStore()
    runner = _runner(gateway, dataset, store=store)

    result = runner.run("agent-eval/regression/v1", RUN_NAME, mode="ci")

    output = dataset.outputs["item-1"]
    assert set(output) == {"answer", "trace_id", "status", "evidence_id"}
    assert output["answer"] == "answer for item-1"
    assert output["trace_id"] == "trace-item-1"
    assert output["status"] == "success"
    assert CHUNK["content"] not in json.dumps(output)
    assert "retrieved_chunks" not in json.dumps(output)

    evidence = store.get(output["evidence_id"])
    assert evidence is not None
    assert evidence.retrieved_chunks == [CHUNK]
    assert evidence.citations == [CITATION]
    assert evidence.trace_id == "trace-item-1"

    scores = {evaluation.name: evaluation.value for evaluation in dataset.evaluations["item-1"]}
    assert scores["kb_recall@5"] == 1.0
    assert scores["citation_valid"] == 1.0
    assert result.result_count == 1


def test_run_gives_every_item_its_own_thread_and_run() -> None:
    gateway = FakeGateway()
    dataset = FakeDataset([_item("item-1"), _item("item-2")])
    runner = _runner(gateway, dataset)

    runner.run("agent-eval/regression/v1", RUN_NAME, mode="ci")

    bodies = [
        json.loads(request.content) for request in gateway.requests if request.method == "POST"
    ]
    thread_posts = [body for body in bodies if "thread_id" in body]
    run_posts = [body for body in bodies if "thread_id" not in body]
    assert [post["metadata"] for post in thread_posts] == [
        {"eval": True, "eval_run": RUN_NAME, "eval_item_id": "item-1"},
        {"eval": True, "eval_run": RUN_NAME, "eval_item_id": "item-2"},
    ]
    assert len({post["thread_id"] for post in thread_posts}) == 2
    assert [post["metadata"] for post in run_posts] == [
        {"eval_item_id": "item-1"},
        {"eval_item_id": "item-2"},
    ]
    assert all(post["on_disconnect"] == "continue" for post in run_posts)
    wire = json.dumps(bodies)
    assert PASSWORD not in wire
    assert RAW_USER_ID not in wire
    assert HASHED_USER_ID not in wire


@pytest.mark.parametrize("status", ["error", "timeout", "interrupted"])
def test_run_marks_a_failed_run_status_as_a_failed_item(status: str) -> None:
    gateway = FakeGateway(status_by_item={"item-2": status})
    dataset = FakeDataset(
        [
            _item("item-1", expected_output={"expect_kb": False}),
            _item("item-2", expected_output={"expect_kb": False}),
        ]
    )
    runner = _runner(gateway, dataset)

    result = runner.run("agent-eval/regression/v1", RUN_NAME, mode="ci")

    assert [failure.item_id for failure in result.failed_items] == ["item-2"]
    assert result.failed_items[0].status == status
    assert result.passed is False
    assert "FAIL" in result.render()


def test_run_records_a_task_exception_and_fails_the_completeness_gate() -> None:
    gateway = FakeGateway(fail_runs_for=["item-2"])
    dataset = FakeDataset([_item("item-1"), _item("item-2")])
    runner = _runner(gateway, dataset)

    with pytest.raises(IncompleteExperiment) as excinfo:
        runner.run("agent-eval/regression/v1", RUN_NAME, mode="ci")

    assert excinfo.value.missing_item_ids == ("item-2",)
    assert "HTTPStatusError" in str(excinfo.value)


@pytest.mark.parametrize(
    "metadata",
    [
        {"user_id": RAW_USER_ID},
        {"user_id": "alice"},
        {"category": "single_tool"},
        "not-a-mapping",
    ],
)
def test_run_rejects_a_missing_or_raw_user_id_without_falling_back(metadata: Any) -> None:
    item = FakeItem(
        id="item-1",
        input={"query": "question for item-1"},
        expected_output={},
        metadata=metadata,
    )
    gateway = FakeGateway()
    dataset = FakeDataset([item])
    runner = _runner(gateway, dataset)

    with pytest.raises(ExperimentConfigError) as excinfo:
        runner.run("agent-eval/regression/v1", RUN_NAME, mode="ci")

    message = str(excinfo.value)
    assert "item-1" in message
    assert RAW_USER_ID not in message
    assert PASSWORD not in message
    assert dataset.calls == []
    assert gateway.requests == []


def test_run_rejects_a_hashed_user_id_with_no_logged_in_account() -> None:
    gateway = FakeGateway()
    dataset = FakeDataset([_item("item-1", user_id=OTHER_HASHED_USER_ID)])
    runner = _runner(gateway, dataset)

    with pytest.raises(ExperimentConfigError) as excinfo:
        runner.run("agent-eval/regression/v1", RUN_NAME, mode="ci")

    assert "item-1" in str(excinfo.value)
    assert OTHER_HASHED_USER_ID not in str(excinfo.value)
    assert dataset.calls == []


def test_run_rejects_an_unknown_mode() -> None:
    dataset = FakeDataset([_item("item-1")])
    runner = _runner(FakeGateway(), dataset)

    with pytest.raises(ExperimentConfigError, match="mode"):
        runner.run("agent-eval/regression/v1", RUN_NAME, mode="nightly")


def test_run_rejects_an_empty_dataset() -> None:
    dataset = FakeDataset([])
    runner = _runner(FakeGateway(), dataset)

    with pytest.raises(ExperimentConfigError):
        runner.run("agent-eval/regression/v1", RUN_NAME, mode="ci")


def test_run_fails_when_a_rule_threshold_is_missed(capsys: pytest.CaptureFixture[str]) -> None:
    items = [
        _item("item-1", expected_output={"expected_tools": ["search_projects"]}),
        _item("item-2", expected_output={"expected_tools": ["search_projects"]}),
    ]
    gateway = FakeGateway(tools_by_item={"item-1": ["search_projects"]})
    dataset = FakeDataset(items)
    runner = _runner(gateway, dataset)

    result = runner.run("agent-eval/regression/v1", RUN_NAME, mode="ci")

    assert result.passed is False
    by_metric = {gate.metric: gate for gate in result.gates}
    assert by_metric["tool_hit"].status == "fail"
    assert by_metric["tool_hit"].value == pytest.approx(0.5)
    assert by_metric["tool_hit"].threshold == pytest.approx(0.90)
    assert by_metric["retrieval_recall@5"].status == "fail"
    assert capsys.readouterr().out == ""


def test_run_fails_on_a_citation_violation() -> None:
    forged = {"citation_id": "cite-9", "chunk_id": "chunk-9", "document_id": "doc-9"}
    gateway = FakeGateway(
        citations_by_item={"item-1": [forged]}, chunks_by_item={"item-1": [CHUNK]}
    )
    dataset = FakeDataset(
        [
            _item("item-1", expected_output={"expect_kb": False}),
            _item("item-2", expected_output={"expect_kb": False}),
        ]
    )
    runner = _runner(gateway, dataset)

    result = runner.run("agent-eval/regression/v1", RUN_NAME, mode="ci")

    assert result.citation_violations == ("item-1",)
    assert result.failed_items == ()
    assert result.passed is False
    assert "citation violation" in result.render()


def test_evaluate_gates_gates_rule_metrics_hard() -> None:
    gates = evaluate_gates({"tool_hit": [1.0, 0.0]}, mode="ci")

    by_metric = {gate.metric: gate for gate in gates}
    assert by_metric["tool_hit"] == GateResult(
        metric="tool_hit",
        value=0.5,
        threshold=0.90,
        comparison="min",
        kind="rule",
        samples=2,
        status="fail",
    )
    rendered = by_metric["tool_hit"].render()
    assert "tool_hit" in rendered
    assert "0.500" in rendered
    assert "FAIL" in rendered
    assert {gate.status for gate in gates if gate.metric != "tool_hit"} == {"insufficient"}


LLM_THRESHOLDS = {"answer_quality": {"value": 0.85, "comparison": "min", "kind": "llm"}}


def test_evaluate_gates_reports_llm_metrics_as_soft_in_ci() -> None:
    gates = evaluate_gates({"answer_quality": [0.5] * 25}, mode="ci", thresholds=LLM_THRESHOLDS)

    assert gates[0].status == "soft"
    assert gates[0].samples == 25
    assert "soft" in gates[0].render()


def test_evaluate_gates_gates_llm_metrics_hard_in_full() -> None:
    gates = evaluate_gates({"answer_quality": [0.5] * 25}, mode="full", thresholds=LLM_THRESHOLDS)

    assert gates[0].status == "fail"


def test_evaluate_gates_reports_insufficient_below_the_minimum_llm_samples() -> None:
    gates = evaluate_gates({"answer_quality": [0.1] * 19}, mode="full", thresholds=LLM_THRESHOLDS)

    assert gates[0].status == "insufficient"
    assert gates[0].samples == 19
    assert "insufficient" in gates[0].render()


def test_aggregate_scores_drops_none_and_gates_cover_every_thresholded_metric() -> None:
    results = [
        FakeItemResult(
            item=_item("item-1"),
            output={},
            evaluations=[
                Evaluation(name="tool_hit", value=None),
                Evaluation(name="tool_hit", value=1.0),
                Evaluation(name="skill_extra", value=3.0),
            ],
        )
    ]

    scores = aggregate_scores(results)
    gates = evaluate_gates(scores, mode="ci")

    assert scores == {"tool_hit": [1.0], "skill_extra": [3.0]}
    by_metric = {gate.metric: gate for gate in gates}
    assert sorted(by_metric) == sorted(THRESHOLDS)
    assert "skill_extra" not in by_metric
    assert by_metric["tool_hit"] == GateResult(
        metric="tool_hit",
        value=1.0,
        threshold=0.90,
        comparison="min",
        kind="rule",
        samples=1,
        status="pass",
    )
    unscored = by_metric["kb_recall@5"]
    assert (unscored.value, unscored.samples, unscored.status) == (None, 0, "insufficient")
    assert evaluate_gates({"unknown_metric": [0.0]}, mode="ci") == evaluate_gates({}, mode="ci")


def test_run_executes_only_the_items_it_selected() -> None:
    items = _dataset_items(40, ["single_tool", "kb_single_hop", "skill_single"])
    dataset = FakeDataset(items)
    expected = [item.id for item in ci_subset(items)]
    langfuse = FakeLangfuse(dataset, expected_item_ids=expected)
    runner = _runner(FakeGateway(), dataset, langfuse=langfuse)

    result = runner.run("agent-eval/regression/v1", RUN_NAME, mode="ci")

    assert langfuse.data_ids == expected
    assert len(langfuse.data_ids) == CI_SUBSET_SIZE
    assert set(langfuse.data_ids) != {item.id for item in items}
    assert (result.item_count, result.result_count) == (CI_SUBSET_SIZE, CI_SUBSET_SIZE)


def test_run_honours_max_items_in_full_mode() -> None:
    items = _dataset_items(5, ["single_tool"])
    dataset = FakeDataset(items)
    langfuse = FakeLangfuse(dataset, expected_item_ids=["item-000", "item-001"])
    runner = _runner(FakeGateway(), dataset, langfuse=langfuse)

    runner.run("agent-eval/regression/v1", RUN_NAME, mode="full", max_items=2)

    assert langfuse.data_ids == ["item-000", "item-001"]


def test_run_names_the_experiment_stably_and_keeps_the_run_name() -> None:
    dataset = FakeDataset([_item("item-1", expected_output={"expect_kb": False})])
    runner = _runner(FakeGateway(), dataset)

    runner.run("agent-eval/regression/v1", RUN_NAME, mode="ci")

    call = dataset.calls[0]
    assert call["name"] == "agent-eval:agent-eval/regression/v1"
    assert call["run_name"] == RUN_NAME
    assert call["max_concurrency"] == 2


def test_run_passes_concurrency_and_mode_metadata_to_the_sdk() -> None:
    dataset = FakeDataset([_item("item-1", expected_output={"expect_kb": False})])
    runner = _runner(FakeGateway(), dataset)

    runner.run("agent-eval/regression/v1", RUN_NAME, mode="full", max_concurrency=3)

    call = dataset.calls[0]
    assert call["max_concurrency"] == 3
    assert call["metadata"] == {"mode": "full"}
    assert all(isinstance(value, str) for value in call["metadata"].values())


def test_run_fails_when_every_item_is_unscored() -> None:
    dataset = FakeDataset([_item("item-1"), _item("item-2")])
    runner = _runner(FakeGateway(), dataset)

    result = runner.run("agent-eval/regression/v1", RUN_NAME, mode="ci")

    assert [(failure.item_id, failure.status) for failure in result.failed_items] == [
        ("item-1", "unscored"),
        ("item-2", "unscored"),
    ]
    assert result.passed is False
    assert "item-1=unscored" in result.render()
    assert "result: FAIL" in result.render()
    assert {gate.status for gate in result.gates} == {"insufficient"}


def test_gate_rows_report_insufficient_for_metrics_without_samples() -> None:
    dataset = FakeDataset(
        [
            _item("item-1", expected_output={"expect_kb": False}),
            _item("item-2", expected_output={"expect_kb": False}),
        ]
    )
    runner = _runner(FakeGateway(), dataset)

    result = runner.run("agent-eval/regression/v1", RUN_NAME, mode="ci")

    by_metric = {gate.metric: gate for gate in result.gates}
    assert set(by_metric) == set(THRESHOLDS)
    assert (by_metric["kb_routing"].status, by_metric["kb_routing"].samples) == ("pass", 2)
    assert (by_metric["tool_hit"].status, by_metric["tool_hit"].samples) == (
        "insufficient",
        0,
    )
    assert result.failed_items == ()
    assert result.passed is True


def test_rule_metric_adapter_passes_item_metadata_to_no_tool_ok() -> None:
    store = EvidenceStore()
    evidence_id = store.put("item-1", _evidence())
    adapter = RuleMetricAdapter(store)

    as_no_tool = adapter(
        input={},
        output={"evidence_id": evidence_id},
        expected_output={},
        metadata={"category": "no_tool"},
    )
    as_tool_sample = adapter(
        input={},
        output={"evidence_id": evidence_id},
        expected_output={},
        metadata={"category": "single_tool"},
    )

    assert {evaluation.name: evaluation.value for evaluation in as_no_tool} == {
        "no_tool_ok": 1.0,
    }
    assert as_tool_sample == []

def test_rule_metric_adapter_scores_every_metric_from_stored_evidence() -> None:
    store = EvidenceStore()
    evidence_id = store.put("item-1", _evidence(retrieved_tools=["search_projects"]))
    adapter = RuleMetricAdapter(store)

    scores = adapter(
        input={"query": "q"},
        output={"evidence_id": evidence_id},
        expected_output={"expected_tools": ["search_projects"]},
        metadata={"category": "single_tool"},
    )

    by_name = {evaluation.name: evaluation.value for evaluation in scores}
    assert by_name["retrieval_recall@5"] == 1.0
    assert by_name["tool_hit"] == 0.0
    assert adapter(input={}, output={}, expected_output={}, metadata={}) == []

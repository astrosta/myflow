"""End-to-end smoke test: the three sample kinds a build emits, run through the CLI.

This is a wiring test, not a live test. A real ``httpx.Client`` talks to an
``httpx.MockTransport`` Gateway, the real ``accounts``/``api``/``evidence``/
``evaluators``/``runner``/``cli`` modules do all the work, and a tiny fake
Langfuse object exposes only the surface ``runner.py`` uses (``get_dataset`` ->
object with ``.items``, ``run_experiment`` -> object with ``.item_results`` and
``.experiment_url``). No network, no real credentials, no sleeps.

The dataset items are produced by the real deterministic builder, so the smoke
test also pins that what ``build`` writes is what ``run`` can score: one tool
sample, one private Skill sample, and one knowledge-base sample. The assertions
are on SCORES, because a wiring test that only checks "no exception" proves
nothing about routing.

Two failure paths are part of the smoke path:

* an item missing from ``item_results`` must raise ``IncompleteExperiment`` and
  leave the CLI with exit code 1 -- a shrunken run must never pass;
* an expected retrieval event that never arrived must score ``0.0`` on
  ``retrieval_recall@5`` and fail its gate -- never be silently skipped.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from langfuse import Evaluation

from myflow_evals.accounts import AccountCredential, AccountSession
from myflow_evals.build_dataset import (
    BuildSources,
    DatasetItemDraft,
    KnowledgeSource,
    SkillSource,
    build_account_items,
)
from myflow_evals.cli import CliServices, main
from myflow_evals.mcp import McpTool
from myflow_evals.runner import ExperimentRunner, IncompleteExperiment

BASE_URL = "http://gateway.test/api"
CSRF_TOKEN = "csrf-token"
HASHED_USER_ID = "9f2c4b6d8e0a1c3e5f7a9b1d3c5e7f90"
DATASET = "agent-eval/regression/v1"
RUN_NAME = "smoke-20261109"

TOOL_NAME = "mcp__crm__find_customer"
SKILL_ID = "skill-1"
SKILL_NAME = "pdf-report"
KNOWLEDGE_BASE_ID = "kb-1"
DOC_ID = "doc-upgrade"
CHUNK_ID = "chunk-1"
CITATION_ID = "k3f9a2c1"
CHUNK = {
    "document_id": DOC_ID,
    "chunk_id": CHUNK_ID,
    "citation_id": CITATION_ID,
    "filename": "upgrade.md",
    "section": "prepare",
    "page": 3,
    "score": 0.9,
    "rerank_score": 0.44,
    "content": "Back up the database before upgrading.",
    "truncated": False,
}
CITATION = {"citation_id": CITATION_ID, "chunk_id": CHUNK_ID, "document_id": DOC_ID}

ENVIRON = {
    "AGENT_API": BASE_URL,
    "USER_ID_SALT": "salt-fixture",
    "LANGFUSE_HOST": "https://langfuse.test",
    "LANGFUSE_PUBLIC_KEY": "unused",
    "LANGFUSE_SECRET_KEY": "unused",
}


# --- fakes -----------------------------------------------------------------


@dataclass
class FakeItem:
    id: str
    input: dict[str, Any]
    expected_output: dict[str, Any]
    metadata: dict[str, Any]


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


class FakeDataset:
    """Runs the real task and evaluators; ``drop`` makes an item vanish."""

    def __init__(self, items: Sequence[FakeItem], *, drop: Sequence[str] = ()) -> None:
        self.items = list(items)
        self._drop = set(drop)
        self.data_ids: list[str] = []
        self.calls: list[dict[str, Any]] = []
        self.outputs: dict[str, Any] = {}
        self.evaluations: dict[str, list[Evaluation]] = {}

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
        self.data_ids = [item.id for item in data]
        self.calls.append(
            {
                "name": name,
                "run_name": run_name,
                "max_concurrency": max_concurrency,
                "metadata": dict(metadata or {}),
            }
        )
        results: list[FakeItemResult] = []
        for item in data:
            output = task(item=item)
            if inspect.isawaitable(output):
                output = asyncio.run(output)
            evaluations: list[Evaluation] = []
            for evaluator in evaluators:
                evaluations.extend(
                    evaluator(
                        input=item.input,
                        output=output,
                        expected_output=item.expected_output,
                        metadata=item.metadata,
                    )
                    or []
                )
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
        return FakeExperimentOutput(item_results=results)


class FakeLangfuse:
    """Only the SDK surface ``runner.py`` reads, plus the item_results list."""

    def __init__(self, dataset: FakeDataset) -> None:
        self.dataset = dataset
        self.requested: list[str] = []

    def get_dataset(self, name: str, **kwargs: Any) -> FakeDataset:
        self.requested.append(name)
        return self.dataset

    def run_experiment(self, **kwargs: Any) -> FakeExperimentOutput:
        return self.dataset.run_experiment(**kwargs)


class FakeGateway:
    """An in-memory Gateway: one thread, run, ledger, and checkpoint per item."""

    def __init__(
        self, scenarios: Mapping[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]]
    ) -> None:
        self._scenarios = {
            item_id: (list(messages), list(events))
            for item_id, (messages, events) in scenarios.items()
        }
        self.requests: list[httpx.Request] = []
        self._threads: dict[str, str] = {}
        self._runs: dict[str, str] = {}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
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
            run_id = f"run-{len(self._runs) + 1}"
            self._runs[run_id] = item_id
            return httpx.Response(
                202, json={"run_id": run_id, "thread_id": thread_id, "status": "pending"}
            )
        if rest[1:] == ["state"]:
            messages, _ = self._scenarios[item_id]
            return httpx.Response(
                200,
                json={"values": {"messages": messages}, "checkpoint_id": "cp-1"},
            )

        run_id = rest[2]
        tail = rest[3:]
        if tail == ["events"]:
            _, events = self._scenarios[item_id]
            rows = [dict(event, run_id=run_id) for event in events]
            return httpx.Response(200, json={"data": rows, "has_more": False})
        if tail == []:
            return httpx.Response(
                200,
                json={
                    "run_id": run_id,
                    "thread_id": thread_id,
                    "status": "success",
                    "metadata": {"myflow-trace_id": f"trace-{item_id}"},
                },
            )
        raise AssertionError(f"unexpected request path {request.url.path}")


# --- dataset items, built by the real builder ------------------------------


def _credential() -> AccountCredential:
    # The smoke test never performs a real login; the password is an unused
    # fixture placeholder so no credential value exists in this file.
    return AccountCredential(label="analyst_a", username="alice", password="unused")


def _session(transport: httpx.BaseTransport | None = None) -> AccountSession:
    client = httpx.Client(
        base_url=BASE_URL,
        transport=transport,
        headers={"X-CSRF-Token": CSRF_TOKEN},
    )
    return AccountSession(
        credential=_credential(),
        client=client,
        user_id="unused",
        hashed_user_id=HASHED_USER_ID,
    )


def _drafts() -> list[DatasetItemDraft]:
    """The three built sample kinds, from one fixed account's real source shapes."""
    session = _session()
    sources = BuildSources(
        tools=[
            McpTool(
                call_name=TOOL_NAME,
                name="find_customer",
                description="Find a customer by email address.",
                input_schema={"type": "object", "required": [], "properties": {}},
            )
        ],
        skills=[
            SkillSource(
                skill_id=SKILL_ID,
                name=SKILL_NAME,
                description="Render a PDF report from the current thread.",
                git_commit="abc123",
            )
        ],
        knowledge=[
            KnowledgeSource(
                knowledge_base_id=KNOWLEDGE_BASE_ID,
                document_id=DOC_ID,
                filename="upgrade.md",
                chunk_id=CHUNK_ID,
                chunk_content=CHUNK["content"],
                chunk_count=3,
            )
        ],
        source_totals={"tool": 1, "skill": 1, "knowledge": 1, "trace": 0},
    )
    try:
        return build_account_items(session, sources)
    finally:
        session.client.close()


def _items(drafts: Sequence[DatasetItemDraft]) -> list[FakeItem]:
    return [
        FakeItem(
            id=draft.id,
            input=draft.input,
            expected_output=draft.expected_output,
            metadata=draft.metadata,
        )
        for draft in drafts
    ]


def _by_kind(items: Sequence[FakeItem]) -> dict[str, FakeItem]:
    return {item.metadata["source_kind"]: item for item in items}


def _scores(dataset: FakeDataset, item_id: str) -> dict[str, Any]:
    return {evaluation.name: evaluation.value for evaluation in dataset.evaluations[item_id]}


# --- per-kind run evidence --------------------------------------------------


def _run_evidence(item: FakeItem, *, retrieval: bool = True) -> tuple[list[dict], list[dict]]:
    """The checkpoint messages and event-ledger rows one finished run leaves behind."""
    kind = item.metadata["source_kind"]
    call_id = f"call-{item.id}"

    if kind == "tool":
        messages = [
            {"type": "human", "content": item.input["query"]},
            {
                "type": "ai",
                "id": f"ai-{item.id}-1",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "name": "tool_execute",
                        "args": {"tool_name": TOOL_NAME, "arguments": {}},
                    }
                ],
            },
            {"type": "tool", "tool_call_id": call_id, "content": "customer found"},
            {"type": "ai", "id": f"ai-{item.id}-final", "content": "Here is the customer."},
        ]
        events = []
        if retrieval:
            events.append(
                {
                    "event_type": "retrieval.tool",
                    "category": "retrieval",
                    "content": {
                        "tools": [{"tool_id": "tool-1", "call_name": TOOL_NAME, "score": 0.91}]
                    },
                }
            )
        return messages, events

    if kind == "skill":
        task_id = f"task-{item.id}"
        load_id = f"load-{item.id}"
        messages = [
            {"type": "human", "content": item.input["query"]},
            {
                "type": "ai",
                "id": f"ai-{item.id}-task",
                "content": "",
                "tool_calls": [
                    {
                        "id": task_id,
                        "name": "task",
                        "args": {"subagent_type": "skill-finder", "description": "find the skill"},
                    }
                ],
            },
            {"type": "tool", "tool_call_id": task_id, "content": "pdf-report found"},
            {
                "type": "ai",
                "id": f"ai-{item.id}-load",
                "content": "",
                "tool_calls": [{"id": load_id, "name": "load_skill", "args": {"name": SKILL_NAME}}],
            },
            {"type": "tool", "tool_call_id": load_id, "content": "skill loaded"},
            {"type": "ai", "id": f"ai-{item.id}-final", "content": "The report is ready."},
        ]
        events = [
            {
                "event_type": "subagent.start",
                "category": "subagent",
                "metadata": {"task_id": task_id, "subagent_type": "skill-finder"},
                "content": {"description": "find the skill"},
            },
            {
                "event_type": "retrieval.skill",
                "category": "retrieval",
                "content": {"skills": [{"skill_id": SKILL_ID, "name": SKILL_NAME}]},
            },
        ]
        return messages, events

    if kind == "knowledge":
        task_id = f"task-{item.id}"
        messages = [
            {"type": "human", "content": item.input["query"]},
            {
                "type": "ai",
                "id": f"ai-{item.id}-task",
                "content": "",
                "tool_calls": [
                    {
                        "id": task_id,
                        "name": "task",
                        "args": {
                            "subagent_type": "knowledge-researcher",
                            "description": "research",
                        },
                    }
                ],
            },
            {"type": "tool", "tool_call_id": task_id, "content": "document read"},
            {
                "type": "ai",
                "id": f"ai-{item.id}-final",
                "content": "Back up the database first.",
                "additional_kwargs": {"knowledge_citations": [dict(CITATION)]},
            },
        ]
        events = [
            {
                "event_type": "subagent.start",
                "category": "subagent",
                "metadata": {"task_id": task_id, "subagent_type": "knowledge-researcher"},
                "content": {"description": "research"},
            },
            {
                "event_type": "retrieval.kb",
                "category": "retrieval",
                "content": {
                    "query": item.input["query"],
                    "retrieval_mode": "hybrid",
                    "reranked": True,
                    "items": [dict(CHUNK)],
                },
            },
        ]
        return messages, events

    raise AssertionError(f"unexpected source kind {kind!r}")


def _gateway(items: Sequence[FakeItem], *, retrieval_for: Sequence[str] = ()) -> FakeGateway:
    """A Gateway whose per-kind evidence comes from each item's built metadata."""
    without_retrieval = set(retrieval_for)
    return FakeGateway(
        {
            item.id: _run_evidence(
                item, retrieval=item.metadata["source_kind"] not in without_retrieval
            )
            for item in items
        }
    )


def _services(gateway: FakeGateway, langfuse: FakeLangfuse) -> tuple[CliServices, AccountSession]:
    session = _session(gateway.transport())

    def fake_load_accounts(
        path: Any = None, *, environ: Any = None
    ) -> list[AccountCredential]:
        return [_credential()]

    def fake_login_all(
        accounts: Sequence[AccountCredential], *, base_url: str, salt: str
    ) -> dict[str, AccountSession]:
        assert base_url == BASE_URL
        assert salt == "salt-fixture"
        return {session.hashed_user_id: session}

    def fake_langfuse_client(environ: Mapping[str, str]) -> FakeLangfuse:
        assert environ["LANGFUSE_HOST"] == "https://langfuse.test"
        return langfuse

    services = CliServices(
        load_accounts=fake_load_accounts,
        login_all=fake_login_all,
        langfuse_client=fake_langfuse_client,
    )
    return services, session


def _run_argv(*extra: str) -> list[str]:
    return [
        "run",
        "--dataset",
        DATASET,
        "--run-name",
        RUN_NAME,
        "--mode",
        "full",
        *extra,
    ]


def _capture(capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


# --- the smoke scenarios ----------------------------------------------------


def test_build_output_scores_all_three_sample_kinds_end_to_end(
    capsys: pytest.CaptureFixture[str],
) -> None:
    items = _items(_drafts())
    kinds = _by_kind(items)
    dataset = FakeDataset(items)
    langfuse = FakeLangfuse(dataset)
    services, session = _services(_gateway(items), langfuse)

    exit_code = main(_run_argv(), services=services, environ=dict(ENVIRON))
    output = _capture(capsys)

    assert exit_code == 0
    assert "result: PASS" in output
    assert HASHED_USER_ID not in output

    # The run executed exactly the built items under the stable experiment label.
    assert sorted(dataset.data_ids) == sorted(item.id for item in items)
    assert dataset.calls[0]["name"] == f"agent-eval:{DATASET}"
    assert dataset.calls[0]["run_name"] == RUN_NAME
    assert dataset.calls[0]["max_concurrency"] == 2
    assert langfuse.requested == [DATASET]
    assert session.client.is_closed

    # Tool sample: retrieval.tool is the source, tool_execute is a business call.
    tool_scores = _scores(dataset, kinds["tool"].id)
    assert tool_scores["retrieval_recall@5"] == 1.0
    assert tool_scores["tool_hit"] == 1.0

    # Private Skill sample: delegated to skill-finder, loaded what was retrieved.
    skill_scores = _scores(dataset, kinds["skill"].id)
    assert skill_scores["skill_routing"] == 1.0
    assert skill_scores["skill_hit"] == 1.0
    assert skill_scores["skill_recall@5"] == 1.0

    # Knowledge sample: delegated to knowledge-researcher, cited a retrieved chunk.
    kb_scores = _scores(dataset, kinds["knowledge"].id)
    assert kb_scores["kb_routing"] == 1.0
    assert kb_scores["kb_recall@5"] == 1.0
    assert kb_scores["citation_valid"] == 1.0


def test_a_missing_dataset_item_fails_the_experiment_and_the_cli(
    capsys: pytest.CaptureFixture[str],
) -> None:
    items = _items(_drafts())
    missing_id = _by_kind(items)["knowledge"].id

    # The runner refuses to score a shrunken result set.
    runner_dataset = FakeDataset(items, drop=[missing_id])
    session = _session(_gateway(items).transport())
    runner = ExperimentRunner(
        FakeLangfuse(runner_dataset), sessions={HASHED_USER_ID: session}
    )
    try:
        with pytest.raises(IncompleteExperiment) as excinfo:
            runner.run(DATASET, RUN_NAME, mode="full")
    finally:
        session.client.close()

    assert excinfo.value.missing_item_ids == (missing_id,)
    assert missing_id in str(excinfo.value)

    # And the CLI turns that exception into a gate failure, never a pass.
    services, session = _services(
        _gateway(items), FakeLangfuse(FakeDataset(items, drop=[missing_id]))
    )
    exit_code = main(_run_argv(), services=services, environ=dict(ENVIRON))
    output = _capture(capsys)

    assert exit_code == 1
    assert missing_id in output
    assert "missing" in output
    assert "result: PASS" not in output
    assert session.client.is_closed


def test_an_expected_retrieval_event_that_never_arrived_scores_zero() -> None:
    tool_item = _by_kind(_items(_drafts()))["tool"]
    dataset = FakeDataset([tool_item])
    gateway = _gateway([tool_item], retrieval_for=["tool"])
    session = _session(gateway.transport())
    runner = ExperimentRunner(FakeLangfuse(dataset), sessions={HASHED_USER_ID: session})
    try:
        result = runner.run(DATASET, RUN_NAME, mode="full")
    finally:
        session.client.close()

    scores = _scores(dataset, tool_item.id)
    # Measured and failed, never skipped: the evaluation exists with value 0.0.
    assert scores["retrieval_recall@5"] == 0.0
    # The tool call itself still happened, so only the retrieval metric moved.
    assert scores["tool_hit"] == 1.0

    gate = next(g for g in result.gates if g.metric == "retrieval_recall@5")
    assert gate.samples == 1
    assert gate.status == "fail"
    assert result.passed is False

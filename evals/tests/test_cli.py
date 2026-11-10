"""CLI tests: subcommand wiring, exit codes, dry runs, and secret hygiene.

Every external seam is faked through CliServices: no Langfuse client, no Gateway
login, and no environment mutation. The fakes record what they were asked to do,
so a dry run is proven to write nothing and a gate failure is proven to leave
through exit code 1 instead of an exception.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from langfuse import Evaluation

from myflow_evals.accounts import AccountCredential, AccountSession, load_accounts
from myflow_evals.build_dataset import (
    BuildSources,
    DatasetItemDraft,
    TraceSource,
    stable_item_id,
)
from myflow_evals.cli import CliServices, LangfuseDatasetWriter, dataset_item_prefix, main
from myflow_evals.mcp import McpTool

BASE_URL = "http://gateway.test/api"
HASHED_USER_ID = "9f2c4b6d8e0a1c3e5f7a9b1d3c5e7f90"
OTHER_HASHED_USER_ID = "0123456789abcdef0123456789abcdef"
RAW_USER_ID = "11111111-1111-4111-8111-111111111111"
PASSWORD = "alice-password"
MCP_HEADER_VALUE = "mcp-header-secret-value"
DATASET = "agent-eval/regression/v1"
RUN_NAME = "ci-20261109"

ENVIRON = {
    "AGENT_API": BASE_URL,
    "USER_ID_SALT": "salt-fixture",
    "LANGFUSE_HOST": "https://langfuse.test",
    "LANGFUSE_PUBLIC_KEY": "pk-fixture",
    "LANGFUSE_SECRET_KEY": "sk-fixture",
}


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
class FakeExperimentResult:
    item_results: list[FakeItemResult]
    experiment_url: str | None = "https://langfuse.test/project/p/experiments/run"


class FakeDataset:
    """Canned experiment results: the CLI tests never drive a real Gateway."""

    def __init__(
        self,
        items: list[FakeItem],
        *,
        missing: list[str] | None = None,
        evaluations: dict[str, list[Evaluation]] | None = None,
    ) -> None:
        self.items = list(items)
        self.missing = set(missing or ())
        self.evaluations = evaluations or {}
        self.calls: list[dict[str, Any]] = []

    def run_experiment(
        self,
        *,
        name: str,
        run_name: str | None = None,
        data: list[FakeItem],
        task: Any,
        evaluators: list[Any] | None = None,
        max_concurrency: int = 50,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> FakeExperimentResult:
        received = [item.id for item in data]
        if received != [item.id for item in self.items]:
            raise AssertionError(
                "run_experiment must execute exactly the selected items via data=; "
                f"got {received!r}"
            )
        self.calls.append(
            {
                "name": name,
                "run_name": run_name,
                "max_concurrency": max_concurrency,
                "metadata": dict(metadata or {}),
            }
        )
        results = [
            FakeItemResult(
                item=item,
                output={"answer": "a", "trace_id": "t", "status": "success", "evidence_id": "ev-x"},
                evaluations=list(self.evaluations.get(item.id, ())),
            )
            for item in data
            if item.id not in self.missing
        ]
        return FakeExperimentResult(item_results=results)


class FakeLangfuse:
    def __init__(self, dataset: FakeDataset | None = None) -> None:
        self.dataset = dataset if dataset is not None else FakeDataset([])
        self.dataset_reads: list[str] = []
        self.created_datasets: list[str] = []
        self.created_items: list[dict[str, Any]] = []

    def get_dataset(self, name: str, **kwargs: Any) -> FakeDataset:
        self.dataset_reads.append(name)
        return self.dataset

    def run_experiment(
        self,
        *,
        name: str,
        run_name: str | None = None,
        data: list[FakeItem],
        task: Any,
        evaluators: list[Any] | None = None,
        max_concurrency: int = 50,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> FakeExperimentResult:
        return self.dataset.run_experiment(
            name=name,
            run_name=run_name,
            data=data,
            task=task,
            evaluators=evaluators,
            max_concurrency=max_concurrency,
            metadata=metadata,
        )

    def create_dataset(self, *, name: str, **kwargs: Any) -> None:
        self.created_datasets.append(name)

    def create_dataset_item(
        self,
        *,
        dataset_name: str,
        id: str,
        input: Any = None,
        expected_output: Any = None,
        metadata: Any = None,
        **kwargs: Any,
    ) -> None:
        self.created_items.append(
            {
                "dataset_name": dataset_name,
                "id": id,
                "input": input,
                "expected_output": expected_output,
                "metadata": metadata,
            }
        )


def _item(
    item_id: str,
    *,
    user_id: str = HASHED_USER_ID,
    expected_output: dict[str, Any] | None = None,
) -> FakeItem:
    return FakeItem(
        id=item_id,
        input={"query": f"question for {item_id}"},
        expected_output=dict(expected_output or {}),
        metadata={"user_id": user_id, "category": "single_tool"},
    )


def _session(hashed_user_id: str = HASHED_USER_ID) -> AccountSession:
    return AccountSession(
        credential=AccountCredential(label="analyst_a", username="alice", password=PASSWORD),
        client=httpx.Client(base_url=BASE_URL),
        user_id=RAW_USER_ID,
        hashed_user_id=hashed_user_id,
    )


@dataclass
class Recorder:
    """Every fake seam the CLI touches, with what it was asked to do."""

    accounts_paths: list[Any] = field(default_factory=list)
    account_environs: list[dict[str, Any]] = field(default_factory=list)
    logins: list[dict[str, Any]] = field(default_factory=list)
    sessions: list[AccountSession] = field(default_factory=list)
    langfuse_calls: list[str] = field(default_factory=list)
    source_calls: list[dict[str, Any]] = field(default_factory=list)
    trace_calls: list[dict[str, Any]] = field(default_factory=list)


def _services(
    recorder: Recorder,
    *,
    dataset: FakeDataset | None = None,
    sources: BuildSources | None = None,
    sources_by_session: dict[str, BuildSources] | None = None,
    traces: list[TraceSource] | None = None,
    sessions: dict[str, AccountSession] | None = None,
) -> tuple[CliServices, FakeLangfuse]:
    langfuse = FakeLangfuse(dataset)

    def fake_load_accounts(
        path: Any = None, *, environ: Any = None
    ) -> list[AccountCredential]:
        recorder.accounts_paths.append(path)
        recorder.account_environs.append(dict(environ or {}))
        return [AccountCredential(label="analyst_a", username="alice", password=PASSWORD)]

    def fake_login_all(
        accounts: list[AccountCredential], *, base_url: str, salt: str
    ) -> dict[str, AccountSession]:
        recorder.logins.append(
            {
                "base_url": base_url,
                "salt": salt,
                "count": len(accounts),
                "labels": [account.label for account in accounts],
            }
        )
        logged_in = dict(sessions or {HASHED_USER_ID: _session()})
        recorder.sessions.extend(logged_in.values())
        return logged_in

    def fake_langfuse_client(environ: dict[str, str]) -> FakeLangfuse:
        recorder.langfuse_calls.append("client")
        return langfuse

    def fake_load_sources(
        session: AccountSession,
        *,
        limit: Any = None,
        environ: Any = None,
        include_traces: bool = True,
        rng: Any = None,
    ) -> BuildSources:
        recorder.source_calls.append(
            {
                "limit": limit,
                "environ": dict(environ or {}),
                "include_traces": include_traces,
                "rng": rng,
            }
        )
        if sources_by_session is not None:
            return sources_by_session[session.hashed_user_id]
        return sources if sources is not None else BuildSources()

    def fake_fetch_traces(user_hash: str, *, limit: Any = None, environ: Any = None) -> list[TraceSource]:
        recorder.trace_calls.append({"user_hash": user_hash, "limit": limit})
        return list(traces or ())

    services = CliServices(
        load_accounts=fake_load_accounts,
        login_all=fake_login_all,
        langfuse_client=fake_langfuse_client,
        load_sources=fake_load_sources,
        fetch_traces=fake_fetch_traces,
    )
    return services, langfuse


def _run_args(*extra: str) -> list[str]:
    return ["run", "--dataset", DATASET, "--run-name", RUN_NAME, *extra]


def _passing_dataset(count: int = 2) -> FakeDataset:
    items = [_item(f"item-{index}") for index in range(count)]
    evaluations = {
        item.id: [Evaluation(name="tool_hit", value=1.0, comment="called")] for item in items
    }
    return FakeDataset(items, evaluations=evaluations)


def _capture(capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


def test_run_prints_the_summary_and_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    recorder = Recorder()
    services, _ = _services(recorder, dataset=_passing_dataset())

    exit_code = main(_run_args(), services=services, environ=dict(ENVIRON))

    output = _capture(capsys)
    assert exit_code == 0
    assert f"dataset: {DATASET}" in output
    assert "tool_hit" in output
    assert "result: PASS" in output
    assert PASSWORD not in output
    assert HASHED_USER_ID not in output
    assert recorder.logins[0]["base_url"] == BASE_URL
    assert recorder.logins[0]["salt"] == "salt-fixture"


def test_run_returns_one_on_a_threshold_miss(capsys: pytest.CaptureFixture[str]) -> None:
    items = [_item("item-1"), _item("item-2")]
    dataset = FakeDataset(
        items,
        evaluations={item.id: [Evaluation(name="tool_hit", value=0.0)] for item in items},
    )
    services, _ = _services(Recorder(), dataset=dataset)

    exit_code = main(_run_args(), services=services, environ=dict(ENVIRON))

    output = _capture(capsys)
    assert exit_code == 1
    assert "result: FAIL" in output


def test_run_returns_one_on_a_missing_item(capsys: pytest.CaptureFixture[str]) -> None:
    dataset = FakeDataset([_item("item-1"), _item("item-2")], missing=["item-2"])
    services, _ = _services(Recorder(), dataset=dataset)

    exit_code = main(_run_args(), services=services, environ=dict(ENVIRON))

    output = _capture(capsys)
    assert exit_code == 1
    assert "item-2" in output
    assert "missing" in output
    assert "result: PASS" not in output


def test_run_returns_one_on_a_citation_violation(capsys: pytest.CaptureFixture[str]) -> None:
    items = [_item("item-1"), _item("item-2")]
    dataset = FakeDataset(
        items,
        evaluations={"item-1": [Evaluation(name="citation_valid", value=0.0)]},
    )
    services, _ = _services(Recorder(), dataset=dataset)

    exit_code = main(_run_args(), services=services, environ=dict(ENVIRON))

    output = _capture(capsys)
    assert exit_code == 1
    assert "citation violation: item-1" in output


def test_run_returns_two_when_a_secret_is_missing(capsys: pytest.CaptureFixture[str]) -> None:
    environ = {key: value for key, value in ENVIRON.items() if key != "USER_ID_SALT"}
    services, langfuse = _services(Recorder(), dataset=_passing_dataset())

    exit_code = main(_run_args(), services=services, environ=environ)

    output = _capture(capsys)
    assert exit_code == 2
    assert "USER_ID_SALT" in output
    assert langfuse.dataset_reads == []


def test_run_returns_two_on_an_unknown_account(capsys: pytest.CaptureFixture[str]) -> None:
    dataset = FakeDataset([_item("item-1", user_id=OTHER_HASHED_USER_ID)])
    services, _ = _services(Recorder(), dataset=dataset)

    exit_code = main(_run_args(), services=services, environ=dict(ENVIRON))

    output = _capture(capsys)
    assert exit_code == 2
    assert "item-1" in output
    assert OTHER_HASHED_USER_ID not in output
    assert dataset.calls == []


def test_run_returns_two_on_an_empty_dataset(capsys: pytest.CaptureFixture[str]) -> None:
    services, _ = _services(Recorder(), dataset=FakeDataset([]))

    exit_code = main(_run_args(), services=services, environ=dict(ENVIRON))

    assert exit_code == 2
    assert "no items" in _capture(capsys)


def test_run_returns_two_on_a_bad_mode(capsys: pytest.CaptureFixture[str]) -> None:
    services, _ = _services(Recorder(), dataset=_passing_dataset())

    exit_code = main(
        ["run", "--dataset", DATASET, "--mode", "nightly"], services=services, environ=dict(ENVIRON)
    )

    assert exit_code == 2
    assert "mode" in _capture(capsys)


def test_run_dry_run_prints_the_plan_and_writes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    recorder = Recorder()
    dataset = _passing_dataset(40)
    services, langfuse = _services(recorder, dataset=dataset)

    exit_code = main(_run_args("--dry-run"), services=services, environ=dict(ENVIRON))

    output = _capture(capsys)
    assert exit_code == 0
    assert "dry-run" in output
    assert "30" in output
    assert "40" in output
    assert recorder.logins == []
    assert dataset.calls == []
    assert langfuse.created_datasets == []
    assert langfuse.created_items == []


def _tool(name: str, call_name: str) -> McpTool:
    return McpTool(
        call_name=call_name,
        name=name,
        description=f"the {name} tool",
        input_schema={"type": "object", "required": [], "properties": {}},
    )


def test_build_dry_run_reports_counts_and_writes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    recorder = Recorder()
    sources = BuildSources(
        tools=[_tool("search", "mcp__db__search"), _tool("load", "mcp__db__load")],
        source_totals={"tool": 2},
    )
    services, langfuse = _services(recorder, sources=sources)

    exit_code = main(
        ["build", "--dataset", DATASET, "--dry-run"],
        services=services,
        environ=dict(ENVIRON),
    )

    output = _capture(capsys)
    assert exit_code == 0
    assert "mode: dry-run (writes nothing)" in output
    assert "tool: 2 drafts from 2 sources" in output
    assert recorder.langfuse_calls == []
    assert langfuse.created_datasets == []
    assert langfuse.created_items == []


def test_build_help_names_deterministic_ids_and_the_reproducible_draw(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["build", "--help"]) == 0

    output = " ".join(_capture(capsys).split())
    assert "deterministic item ids" in output
    assert "--seed makes the chunk draw reproducible" in output


def test_build_passes_a_seeded_random_to_the_source_loader(
    capsys: pytest.CaptureFixture[str],
) -> None:
    recorder = Recorder()
    services, _ = _services(recorder)

    exit_code = main(
        ["build", "--dataset", DATASET, "--dry-run", "--seed", "12345"],
        services=services,
        environ=dict(ENVIRON),
    )

    assert exit_code == 0
    _capture(capsys)
    rng = recorder.source_calls[0]["rng"]
    assert isinstance(rng, random.Random)
    assert rng.random() == random.Random(12345).random()


def test_build_writes_every_draft_through_the_langfuse_writer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    recorder = Recorder()
    sources = BuildSources(
        tools=[_tool("search", "mcp__db__search")],
        source_totals={"tool": 1},
    )
    services, langfuse = _services(recorder, sources=sources)

    exit_code = main(["build", "--dataset", DATASET], services=services, environ=dict(ENVIRON))

    output = _capture(capsys)
    assert exit_code == 0
    assert langfuse.created_datasets == [DATASET]
    assert [item["dataset_name"] for item in langfuse.created_items] == [DATASET]
    created = langfuse.created_items[0]
    assert created["id"] == (
        f"{dataset_item_prefix(DATASET)}-"
        + stable_item_id("tool", "mcp__db__search", 0, HASHED_USER_ID)
    )
    assert created["input"]["query"].startswith("Use the search tool")
    assert created["metadata"]["user_id"] == HASHED_USER_ID
    assert f"written: 1 item(s) to {DATASET}" in output
    assert PASSWORD not in output


def test_ingest_prints_drafts_and_writes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    recorder = Recorder()
    traces = [TraceSource(trace_id="trace-abc", query="what is the refund policy?")]
    services, langfuse = _services(recorder, traces=traces)

    exit_code = main(["ingest", "--dataset", DATASET], services=services, environ=dict(ENVIRON))

    output = _capture(capsys)
    assert exit_code == 0
    assert "trace-abc" in output
    assert "what is the refund policy?" in output
    assert "nothing was written" in output
    assert recorder.trace_calls[0]["user_hash"] == HASHED_USER_ID
    assert langfuse.created_datasets == []
    assert langfuse.created_items == []


def test_ingest_returns_two_when_langfuse_is_not_configured(
    capsys: pytest.CaptureFixture[str],
) -> None:
    environ = {key: value for key, value in ENVIRON.items() if not key.startswith("LANGFUSE_")}
    services, _ = _services(Recorder())

    exit_code = main(["ingest", "--dataset", DATASET], services=services, environ=environ)

    assert exit_code == 2
    assert "LANGFUSE_HOST" in _capture(capsys)


def test_cli_reads_shared_settings_from_an_env_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / "eval.env"
    env_file.write_text(
        "\n".join(f"{key}={value}" for key, value in ENVIRON.items()) + "\n",
        encoding="utf-8",
    )
    recorder = Recorder()
    services, _ = _services(recorder, dataset=_passing_dataset())

    exit_code = main(
        _run_args("--env-file", str(env_file)), services=services, environ={}
    )

    assert exit_code == 0
    assert recorder.logins[0]["base_url"] == BASE_URL
    assert recorder.logins[0]["salt"] == "salt-fixture"


def test_mcp_headers_file_fills_the_environment_without_echoing_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    headers_file = tmp_path / "mcp.json"
    headers_file.write_text(
        json.dumps({"project_db": {"X-Api-Key": MCP_HEADER_VALUE}}),
        encoding="utf-8",
    )
    recorder = Recorder()
    sources = BuildSources(tools=[], source_totals={"tool": 0})
    services, _ = _services(recorder, sources=sources)

    exit_code = main(
        ["build", "--dataset", DATASET, "--dry-run", "--mcp-headers-file", str(headers_file)],
        services=services,
        environ=dict(ENVIRON),
    )

    output = _capture(capsys)
    assert exit_code == 0
    assert json.loads(recorder.source_calls[0]["environ"]["MCP_SERVER_HEADERS"]) == {
        "project_db": {"X-Api-Key": MCP_HEADER_VALUE}
    }
    assert MCP_HEADER_VALUE not in output


def test_accounts_file_is_passed_through_without_being_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    accounts_file = tmp_path / "accounts.json"
    accounts_file.write_text(
        json.dumps({"analyst_a": {"username": "alice", "password": PASSWORD}}), encoding="utf-8"
    )
    recorder = Recorder()
    services, _ = _services(recorder, dataset=_passing_dataset())

    exit_code = main(
        _run_args("--accounts-file", str(accounts_file)),
        services=services,
        environ=dict(ENVIRON),
    )

    output = _capture(capsys)
    assert exit_code == 0
    assert recorder.accounts_paths == [Path(accounts_file)]
    assert PASSWORD not in output


def test_build_merges_every_logged_in_account(capsys: pytest.CaptureFixture[str]) -> None:
    recorder = Recorder()
    sessions = {
        HASHED_USER_ID: _session(),
        OTHER_HASHED_USER_ID: _session(OTHER_HASHED_USER_ID),
    }
    sources_by_session = {
        HASHED_USER_ID: BuildSources(
            tools=[_tool("search", "mcp__db__search")], source_totals={"tool": 1}
        ),
        OTHER_HASHED_USER_ID: BuildSources(
            tools=[_tool("load", "mcp__db__load")], source_totals={"tool": 1}
        ),
    }
    services, langfuse = _services(
        recorder, sessions=sessions, sources_by_session=sources_by_session
    )

    exit_code = main(["build", "--dataset", DATASET], services=services, environ=dict(ENVIRON))

    output = _capture(capsys)
    assert exit_code == 0
    assert "tool: 2 drafts from 2 sources" in output
    prefix = dataset_item_prefix(DATASET)
    assert [item["id"] for item in langfuse.created_items] == [
        f"{prefix}-" + stable_item_id("tool", "mcp__db__load", 0, OTHER_HASHED_USER_ID),
        f"{prefix}-" + stable_item_id("tool", "mcp__db__search", 0, HASHED_USER_ID),
    ]
    assert [item["metadata"]["user_id"] for item in langfuse.created_items] == [
        OTHER_HASHED_USER_ID,
        HASHED_USER_ID,
    ]


def test_run_closes_every_logged_in_session(capsys: pytest.CaptureFixture[str]) -> None:
    recorder = Recorder()
    services, _ = _services(recorder, dataset=_passing_dataset())

    assert main(_run_args(), services=services, environ=dict(ENVIRON)) == 0
    assert main(_run_args(), services=services, environ=dict(ENVIRON)) == 0

    assert len(recorder.sessions) == 2
    assert all(session.client.is_closed for session in recorder.sessions)


def test_build_and_ingest_close_their_sessions(capsys: pytest.CaptureFixture[str]) -> None:
    recorder = Recorder()
    sources = BuildSources(tools=[_tool("search", "mcp__db__search")], source_totals={"tool": 1})
    services, _ = _services(recorder, sources=sources, traces=[TraceSource("trace-1", "why?")])

    assert main(["build", "--dataset", DATASET], services=services, environ=dict(ENVIRON)) == 0
    assert main(["ingest", "--dataset", DATASET], services=services, environ=dict(ENVIRON)) == 0

    assert len(recorder.sessions) == 2
    assert all(session.client.is_closed for session in recorder.sessions)


def test_run_passes_max_concurrency_to_the_runner(capsys: pytest.CaptureFixture[str]) -> None:
    dataset = FakeDataset(
        [_item("item-1")],
        evaluations={"item-1": [Evaluation(name="tool_hit", value=1.0)]},
    )
    services, _ = _services(Recorder(), dataset=dataset)

    exit_code = main(
        _run_args("--max-concurrency", "3"), services=services, environ=dict(ENVIRON)
    )

    assert exit_code == 0
    assert dataset.calls[0]["max_concurrency"] == 3


def _draft(item_id: str, *, kind: str = "tool") -> DatasetItemDraft:
    expected = {} if kind == "trace" else {"expect_kb": False}
    category = "trace_replay" if kind == "trace" else "single_tool"
    return DatasetItemDraft(
        id=item_id,
        input={"query": f"question for {item_id}"},
        expected_output=expected,
        metadata={
            "user_id": HASHED_USER_ID,
            "source_kind": kind,
            "category": category,
        },
    )


def test_build_skips_trace_drafts_unless_include_traces(
    capsys: pytest.CaptureFixture[str],
) -> None:
    recorder = Recorder()
    sources = BuildSources(
        tools=[_tool("search", "mcp__db__search")],
        traces=[TraceSource("trace-1", "why did it fail?")],
        source_totals={"tool": 1, "trace": 1},
    )
    services, langfuse = _services(recorder, sources=sources)

    exit_code = main(["build", "--dataset", DATASET], services=services, environ=dict(ENVIRON))

    assert exit_code == 0
    assert recorder.source_calls[0]["include_traces"] is False
    kinds = [item["metadata"]["source_kind"] for item in langfuse.created_items]
    assert kinds == ["tool"]
    assert "trace: 0 drafts" in _capture(capsys)


def test_build_include_traces_keeps_trace_drafts(capsys: pytest.CaptureFixture[str]) -> None:
    recorder = Recorder()
    sources = BuildSources(
        tools=[_tool("search", "mcp__db__search")],
        traces=[TraceSource("trace-1", "why did it fail?")],
        source_totals={"tool": 1, "trace": 1},
    )
    services, langfuse = _services(recorder, sources=sources)

    exit_code = main(
        ["build", "--dataset", DATASET, "--include-traces"],
        services=services,
        environ=dict(ENVIRON),
    )

    assert exit_code == 0
    assert recorder.source_calls[0]["include_traces"] is True
    kinds = [item["metadata"]["source_kind"] for item in langfuse.created_items]
    assert sorted(kinds) == ["tool", "trace"]


def _env_file(tmp_path: Path, extra: dict[str, str] | None = None) -> Path:
    settings = dict(ENVIRON)
    settings.update(extra or {})
    path = tmp_path / "eval.env"
    path.write_text(
        "\n".join(f"{key}={value}" for key, value in settings.items()) + "\n",
        encoding="utf-8",
    )
    return path


def test_env_file_can_supply_the_fixed_accounts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = _env_file(
        tmp_path,
        {
            "AGENT_USERS": json.dumps(
                {"from_env": {"username": "alice", "password": PASSWORD}}
            )
        },
    )
    recorder = Recorder()
    services, _ = _services(recorder, dataset=_passing_dataset())
    services = replace(services, load_accounts=load_accounts)

    exit_code = main(_run_args("--env-file", str(env_file)), services=services, environ={})

    output = _capture(capsys)
    assert exit_code == 0
    assert recorder.logins[0]["labels"] == ["from_env"]
    assert PASSWORD not in output


def test_accounts_file_wins_over_the_env_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = _env_file(
        tmp_path,
        {"AGENT_USERS": json.dumps({"from_env": {"username": "alice", "password": PASSWORD}})},
    )
    accounts_file = tmp_path / "accounts.json"
    accounts_file.write_text(
        json.dumps({"from_file": {"username": "bob", "password": "bob-password"}}),
        encoding="utf-8",
    )
    recorder = Recorder()
    services, _ = _services(recorder, dataset=_passing_dataset())
    services = replace(services, load_accounts=load_accounts)

    exit_code = main(
        _run_args("--env-file", str(env_file), "--accounts-file", str(accounts_file)),
        services=services,
        environ={},
    )

    output = _capture(capsys)
    assert exit_code == 0
    assert recorder.logins[0]["labels"] == ["from_file"]
    assert "bob-password" not in output


def test_run_forwards_the_salt_fingerprint_to_the_experiment(
    capsys: pytest.CaptureFixture[str],
) -> None:
    dataset = FakeDataset(
        [_item("item-1")],
        evaluations={"item-1": [Evaluation(name="tool_hit", value=1.0)]},
    )
    services, _ = _services(Recorder(), dataset=dataset)

    assert main(_run_args(), services=services, environ=dict(ENVIRON)) == 0

    metadata = dataset.calls[0]["metadata"]
    assert metadata["salt_fingerprint"] == hashlib.sha256(b"salt-fixture").hexdigest()[:8]
    assert metadata["dataset"] == DATASET
    assert "salt-fixture" not in json.dumps(metadata)


def test_writer_namespaces_item_ids_per_dataset() -> None:
    client = FakeLangfuse()
    drafts = [_draft("tool-1")]

    LangfuseDatasetWriter(client).upsert_items("agent-eval/regression/v1", drafts)
    LangfuseDatasetWriter(client).upsert_items("agent-eval/explore", drafts)
    LangfuseDatasetWriter(client).upsert_items("agent-eval/regression/v1", drafts)

    ids = [item["id"] for item in client.created_items]
    assert len({ids[0], ids[1]}) == 2
    assert all(re.fullmatch(r"[0-9a-f]{8}-tool-1", value) for value in ids)
    assert ids[2] == ids[0]
    assert [item["dataset_name"] for item in client.created_items] == [
        "agent-eval/regression/v1",
        "agent-eval/explore",
        "agent-eval/regression/v1",
    ]


def test_main_returns_zero_for_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    assert "build" in _capture(capsys)

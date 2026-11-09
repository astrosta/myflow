"""Langfuse experiment execution with a completeness gate and compact evidence.

This module drives one frozen dataset through the Gateway over HTTP and hands
Langfuse only a compact per-item output -- answer, status, trace id, and an
evidence id -- while the full :class:`RunEvidence` (retrieved chunks, citations,
tool calls) stays in this process in an :class:`EvidenceStore`. The evaluator
adapter reads that evidence back by id, so no chunk body or citation payload is
ever uploaded to Langfuse.

Two guarantees shape the design:

* **Completeness.** ``run`` remembers the ids of the dataset items it planned
  and, once ``run_experiment`` returns, fails with :class:`IncompleteExperiment`
  naming every id that is missing or unexpected. A task exception (which the
  SDK logs and swallows into a dropped item) therefore fails the run instead of
  quietly shrinking the average. A run whose status is ``error``, ``timeout``, or
  ``interrupted`` is recorded as a failed item and fails the same way.
* **Loud configuration.** Account selection never falls back: an item whose
  ``metadata.user_id`` is missing, not a Langfuse hashed id, or not logged in
  raises :class:`ExperimentConfigError` before anything is started.

Nothing here prints and nothing here decides an exit code; the CLI renders the
returned :class:`ExperimentResult` and owns the process status.
"""

from __future__ import annotations

import hashlib
import re
import threading
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from myflow_evals.accounts import AccountSession
from myflow_evals.api import AgentApi
from myflow_evals.evidence import RunEvidence, collect_evidence
from myflow_evals.evaluators import RULE_EVALUATORS, THRESHOLDS

#: The two experiment modes the design distinguishes.
MODE_CI = "ci"
MODE_FULL = "full"
MODES = (MODE_CI, MODE_FULL)
DEFAULT_MODE = MODE_CI

#: The design fixes the CI regression subset at 30 stratified items.
CI_SUBSET_SIZE = 30

#: Design: start at 2 -- the SDK default of 50 is far too heavy for the agent.
DEFAULT_MAX_CONCURRENCY = 2

#: Design: an LLM metric needs this many scored samples before it can gate.
LLM_MIN_SAMPLES = 20

#: The one rule metric that fails a run on a single bad sample.
_CITATION_METRIC = "citation_valid"

_EVIDENCE_PREFIX = "ev-"
_RUN_STATUS_SUCCESS = "success"
_UNCATEGORIZED = "unknown"
_CATEGORY_KEY = "category"
_EVAL_ITEM_METADATA_KEY = "eval_item_id"

#: Langfuse hashed user id: HMAC-SHA256(salt, user_id)[:32], lowercase hex.
_HASHED_USER_ID_RE = re.compile(r"[0-9a-f]{32}")

__all__ = [
    "CI_SUBSET_SIZE",
    "DEFAULT_MAX_CONCURRENCY",
    "DEFAULT_MODE",
    "EvidenceStore",
    "ExperimentConfigError",
    "ExperimentResult",
    "ExperimentRunner",
    "FailedItem",
    "GateResult",
    "IncompleteExperiment",
    "LLM_MIN_SAMPLES",
    "MODES",
    "RuleMetricAdapter",
    "aggregate_scores",
    "ci_subset",
    "citation_violations",
    "evaluate_gates",
    "select_items",
]


class ExperimentConfigError(RuntimeError):
    """The run cannot start as configured: a bad mode, dataset, or account map.

    Messages name the offending dataset item or argument and never a password,
    a header value, or the raw user id behind a hashed one.
    """


class IncompleteExperiment(RuntimeError):
    """At least one planned dataset item has no result; the run must not pass."""

    def __init__(
        self,
        missing_item_ids: Sequence[str],
        *,
        unexpected_item_ids: Sequence[str] = (),
        failures: Sequence["FailedItem"] = (),
    ) -> None:
        self.missing_item_ids = tuple(sorted(missing_item_ids))
        self.unexpected_item_ids = tuple(sorted(unexpected_item_ids))
        self.failures = tuple(failures)
        parts = [
            "experiment did not return every dataset item it planned",
            f"missing: {_join(self.missing_item_ids)}",
        ]
        if self.unexpected_item_ids:
            parts.append(f"unexpected: {_join(self.unexpected_item_ids)}")
        if self.failures:
            parts.append(
                "failed: "
                + ", ".join(
                    f"{failure.item_id}={failure.status}"
                    + (f" ({failure.reason})" if failure.reason else "")
                    for failure in self.failures
                )
            )
        super().__init__("; ".join(parts))


class EvidenceStore:
    """The only place a run's full evidence survives, keyed by an opaque id.

    The runner writes and the evaluators read from the SDK's worker threads, so
    every access takes one lock. ``put`` returns a stable id derived from the item
    id, and re-putting overwrites: a retried item can never resurrect the evidence
    of its earlier attempt.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._evidence: dict[str, RunEvidence] = {}

    def put(self, item_id: str, evidence: RunEvidence) -> str:
        """Store one item's evidence and return the id evaluators read it by."""
        evidence_id = self.evidence_id(item_id)
        with self._lock:
            self._evidence[evidence_id] = evidence
        return evidence_id

    def get(self, evidence_id: str) -> RunEvidence | None:
        """Return the evidence stored under ``evidence_id``, or ``None``."""
        if not isinstance(evidence_id, str):
            return None
        with self._lock:
            return self._evidence.get(evidence_id)

    @staticmethod
    def evidence_id(item_id: str) -> str:
        """Return the deterministic evidence id for one dataset item."""
        if not isinstance(item_id, str) or not item_id:
            raise ValueError("item_id must be a non-empty string")
        digest = hashlib.sha256(item_id.encode("utf-8")).hexdigest()[:16]
        return f"{_EVIDENCE_PREFIX}{digest}"


@dataclass(frozen=True)
class FailedItem:
    """One item that did not finish cleanly, and why (never the raw message)."""

    item_id: str
    status: str
    reason: str | None = None


@dataclass(frozen=True)
class GateResult:
    """One thresholded metric: its average, its threshold, and its verdict."""

    metric: str
    value: float | None
    threshold: float
    comparison: str
    kind: str
    samples: int
    status: str

    def render(self) -> str:
        """Render one summary line for the CLI; short, deterministic, secret-free."""
        value = "n/a" if self.value is None else f"{self.value:.3f}"
        verdict = _STATUS_TEXT.get(self.status, self.status)
        return (
            f"{self.metric}: {value} {self.comparison} {self.threshold:g} "
            f"samples={self.samples} {verdict}"
        )


@dataclass(frozen=True)
class ExperimentResult:
    """The summary the CLI renders and gates its exit code on."""

    dataset_name: str
    run_name: str
    mode: str
    item_count: int
    result_count: int
    failed_items: tuple[FailedItem, ...] = ()
    gates: tuple[GateResult, ...] = ()
    citation_violations: tuple[str, ...] = ()
    experiment_url: str | None = None

    @property
    def passed(self) -> bool:
        """True only when every item finished, no citation was forged, and no gate failed."""
        if self.failed_items or self.citation_violations:
            return False
        return all(gate.status != "fail" for gate in self.gates)

    def render(self) -> str:
        """Render the human summary as lines; the CLI prints them."""
        lines = [
            f"dataset: {self.dataset_name}",
            f"run: {self.run_name} (mode {self.mode})",
            f"items: {self.result_count} of {self.item_count} returned",
        ]
        lines.extend(gate.render() for gate in self.gates)
        if self.failed_items:
            lines.append(
                "failed items: "
                + ", ".join(
                    f"{failure.item_id}={failure.status}"
                    + (f" ({failure.reason})" if failure.reason else "")
                    for failure in self.failed_items
                )
            )
        if self.citation_violations:
            lines.append("citation violation: " + ", ".join(self.citation_violations))
        lines.append("result: " + ("PASS" if self.passed else "FAIL"))
        if self.experiment_url:
            lines.append(f"experiment: {self.experiment_url}")
        return "\n".join(lines)


_STATUS_TEXT = {
    "pass": "PASS",
    "fail": "FAIL",
    "soft": "soft-fail (LLM metrics are not hard gates in ci mode)",
    "insufficient": "insufficient scored samples (LLM metrics need 20)",
}


class RuleMetricAdapter:
    """Langfuse item evaluator that scores the compact output with every rule metric.

    The output carries only an ``evidence_id``; the full evidence is fetched from
    the store, so the payload Langfuse sees never grows with the retrieved text.
    An output without a resolvable evidence id scores nothing -- the item never
    produced evidence, and the completeness gate is what fails such a run.
    """

    __name__ = "rule_metrics"

    def __init__(self, store: EvidenceStore) -> None:
        self._store = store

    def __call__(
        self,
        *,
        input: Any = None,
        output: Any = None,
        expected_output: Any = None,
        metadata: Any = None,
        **kwargs: Any,
    ) -> list[Any]:
        evidence_id = output.get("evidence_id") if isinstance(output, Mapping) else None
        evidence = self._store.get(evidence_id) if evidence_id is not None else None
        if evidence is None:
            return []
        declared = expected_output if isinstance(expected_output, Mapping) else None
        item_metadata = metadata if isinstance(metadata, Mapping) else None
        evaluations: list[Any] = []
        for name in sorted(RULE_EVALUATORS):
            evaluation = RULE_EVALUATORS[name](
                expected_output=declared,
                evidence=evidence,
                metadata=item_metadata,
            )
            if evaluation is not None:
                evaluations.append(evaluation)
        return evaluations


@dataclass(frozen=True)
class _ItemPlan:
    """Everything one item needs that must be settled before the run starts."""

    item_id: str
    query: str
    session: AccountSession


class _ItemTask:
    """The Langfuse task: one thread, one run, one evidence record per item."""

    def __init__(
        self,
        *,
        api: Any,
        plans: Mapping[str, _ItemPlan],
        store: EvidenceStore,
        run_name: str,
    ) -> None:
        self._api = api
        self._plans = plans
        self._store = store
        self._run_name = run_name
        self._lock = threading.Lock()
        self.failures: list[FailedItem] = []

    def __call__(self, *, item: Any, **kwargs: Any) -> dict[str, Any]:
        """Run one dataset item and return its compact output."""
        item_id = _item_id(item)
        plan = self._plans.get(item_id)
        if plan is None:
            raise ExperimentConfigError(
                f"dataset item {item_id} was not part of the planned run"
            )
        try:
            return self._run_item(plan)
        except Exception as exc:
            self._record(FailedItem(item_id, "exception", _failure_reason(exc)))
            raise

    def _run_item(self, plan: _ItemPlan) -> dict[str, Any]:
        thread_id = uuid.uuid4().hex
        self._api.create_thread(
            plan.session,
            thread_id,
            {"eval": True, "eval_run": self._run_name, _EVAL_ITEM_METADATA_KEY: plan.item_id},
        )
        run_id = self._api.start_run(
            plan.session, thread_id, plan.query, {_EVAL_ITEM_METADATA_KEY: plan.item_id}
        )
        snapshot = self._api.wait_run(plan.session, thread_id, run_id)
        events = self._api.list_events(plan.session, thread_id, snapshot.run_id)
        state = self._api.thread_state(plan.session, thread_id)
        messages = _messages_from(state)
        evidence = collect_evidence(
            messages, events, run_id=snapshot.run_id, trace_id=snapshot.trace_id
        )
        evidence.trace_id = snapshot.trace_id
        evidence_id = self._store.put(plan.item_id, evidence)
        if snapshot.status != _RUN_STATUS_SUCCESS:
            # error / timeout / interrupted (and any other non-success terminal
            # status) is a failed sample, never a silently unscored one.
            self._record(FailedItem(plan.item_id, snapshot.status))
        return {
            "answer": evidence.answer,
            "trace_id": snapshot.trace_id,
            "status": snapshot.status,
            "evidence_id": evidence_id,
        }

    def _record(self, failure: FailedItem) -> None:
        with self._lock:
            self.failures.append(failure)

    @property
    def snapshot(self) -> tuple[FailedItem, ...]:
        """A stable, sorted copy of the recorded failures."""
        with self._lock:
            return tuple(sorted(self.failures, key=lambda failure: failure.item_id))


class ExperimentRunner:
    """Runs one frozen dataset through the Gateway and gates the result."""

    def __init__(
        self,
        client: Any,
        *,
        sessions: Mapping[str, AccountSession],
        api: Any = AgentApi,
        store: EvidenceStore | None = None,
    ) -> None:
        self._client = client
        self._sessions = dict(sessions)
        self._api = api
        self._store = store if store is not None else EvidenceStore()

    def run(
        self,
        dataset_name: str,
        run_name: str,
        *,
        mode: str = DEFAULT_MODE,
        max_items: int | None = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ) -> ExperimentResult:
        """Run every selected item once and score, gate, and summarize the result.

        Raises :class:`ExperimentConfigError` before anything starts when the mode,
        the dataset, or an item's account mapping is unusable, and
        :class:`IncompleteExperiment` afterwards when an item has no result.
        """
        _validate_mode(mode)
        _validate_positive_int(max_concurrency, "max_concurrency")
        _validate_max_items(max_items)
        if not isinstance(dataset_name, str) or not dataset_name.strip():
            raise ExperimentConfigError("dataset name must be a non-empty string")
        if not isinstance(run_name, str) or not run_name.strip():
            raise ExperimentConfigError("run name must be a non-empty string")

        dataset = self._client.get_dataset(dataset_name)
        items = list(getattr(dataset, "items", ()) or ())
        if not items:
            raise ExperimentConfigError(
                f"dataset {dataset_name!r} has no items; build it before running it"
            )
        selected = select_items(items, mode=mode, max_items=max_items)
        plans = {plan.item_id: plan for plan in self._plan(selected)}

        task = _ItemTask(
            api=self._api, plans=plans, store=self._store, run_name=run_name
        )
        outcome = dataset.run_experiment(
            name=run_name,
            run_name=run_name,
            task=task,
            evaluators=[RuleMetricAdapter(self._store)],
            max_concurrency=max_concurrency,
            metadata={"mode": mode},
        )

        item_results = list(getattr(outcome, "item_results", ()) or ())
        actual_ids = {_item_id(result.item) for result in item_results}
        missing = sorted(set(plans) - actual_ids)
        unexpected = sorted(actual_ids - set(plans))
        if missing or unexpected:
            raise IncompleteExperiment(
                missing,
                unexpected_item_ids=unexpected,
                failures=task.snapshot,
            )

        scores = aggregate_scores(item_results)
        return ExperimentResult(
            dataset_name=dataset_name,
            run_name=run_name,
            mode=mode,
            item_count=len(plans),
            result_count=len(item_results),
            failed_items=task.snapshot,
            gates=evaluate_gates(scores, mode=mode),
            citation_violations=citation_violations(item_results),
            experiment_url=getattr(outcome, "experiment_url", None),
        )

    def _plan(self, items: Sequence[Any]) -> list[_ItemPlan]:
        """Resolve account and query for every item before the first thread exists."""
        plans: list[_ItemPlan] = []
        for item in items:
            item_id = _item_id(item)
            plans.append(
                _ItemPlan(
                    item_id=item_id,
                    query=_query_for(item_id, item),
                    session=self._session_for(item_id, _metadata_of(item)),
                )
            )
        return plans

    def _session_for(self, item_id: str, metadata: Any) -> AccountSession:
        """Pick the session by hashed user id; never fall back to another account."""
        if not isinstance(metadata, Mapping):
            raise ExperimentConfigError(
                f"dataset item {item_id} has no metadata object to select an account"
            )
        user_id = metadata.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            raise ExperimentConfigError(
                f"dataset item {item_id} has no metadata.user_id to select an account"
            )
        if not _HASHED_USER_ID_RE.fullmatch(user_id):
            raise ExperimentConfigError(
                f"dataset item {item_id} carries a user id that is not a Langfuse "
                "hashed id; rebuild the dataset instead of sending a raw id"
            )
        session = self._sessions.get(user_id)
        if session is None:
            raise ExperimentConfigError(
                f"dataset item {item_id} names a user id with no logged-in test account"
            )
        return session


def ci_subset(items: Sequence[Any], size: int = CI_SUBSET_SIZE) -> list[Any]:
    """Return the fixed stratified CI subset: round-robin over id-sorted categories.

    Deterministic by construction: group by ``metadata.category`` (``unknown`` when
    absent), sort the groups and the items inside them by id, then take one item
    from each group per round until ``size`` is reached. The same dataset always
    yields the same subset, in the same order.
    """
    groups: dict[str, list[Any]] = {}
    for item in sorted(items, key=_item_id):
        groups.setdefault(_category_of(item), []).append(item)
    chosen: list[Any] = []
    cursors = {category: 0 for category in groups}
    while len(chosen) < size:
        advanced = False
        for category in sorted(groups):
            if len(chosen) >= size:
                break
            index = cursors[category]
            if index >= len(groups[category]):
                continue
            chosen.append(groups[category][index])
            cursors[category] = index + 1
            advanced = True
        if not advanced:
            break
    return chosen


def select_items(
    items: Sequence[Any], *, mode: str = DEFAULT_MODE, max_items: int | None = None
) -> list[Any]:
    """Return the items one run executes for ``mode``, capped by ``max_items``."""
    _validate_mode(mode)
    _validate_max_items(max_items)
    ordered = sorted(items, key=_item_id)
    selected = ci_subset(ordered) if mode == MODE_CI else ordered
    return selected if max_items is None else selected[:max_items]


def aggregate_scores(item_results: Sequence[Any]) -> dict[str, list[float]]:
    """Collect every numeric score by metric name; ``None`` means "inapplicable"."""
    scores: dict[str, list[float]] = {}
    for result in item_results:
        for evaluation in getattr(result, "evaluations", ()) or ():
            name = getattr(evaluation, "name", None)
            if not isinstance(name, str) or not name:
                continue
            value = _numeric(getattr(evaluation, "value", None))
            if value is None:
                continue
            scores.setdefault(name, []).append(value)
    return scores


def evaluate_gates(
    scores: Mapping[str, Sequence[float]],
    *,
    mode: str = DEFAULT_MODE,
    thresholds: Mapping[str, Mapping[str, Any]] = THRESHOLDS,
) -> tuple[GateResult, ...]:
    """Compare every thresholded metric average with its design threshold.

    Rule metrics are hard gates. LLM metrics need at least
    :data:`LLM_MIN_SAMPLES` scored samples before they are treated as a hard
    gate; below that, and in ``ci`` mode whatever the sample count, they are
    reported without failing the run. A metric with no threshold entry is
    recorded in Langfuse but not gated here.
    """
    gates: list[GateResult] = []
    for metric in sorted(scores):
        spec = thresholds.get(metric)
        if spec is None:
            continue
        values = [
            value for value in (_numeric(raw) for raw in scores[metric]) if value is not None
        ]
        average = sum(values) / len(values) if values else None
        threshold = float(spec["value"])
        comparison = str(spec["comparison"])
        kind = str(spec["kind"])
        gates.append(
            GateResult(
                metric=metric,
                value=average,
                threshold=threshold,
                comparison=comparison,
                kind=kind,
                samples=len(values),
                status=_gate_status(
                    average, samples=len(values), threshold=threshold,
                    comparison=comparison, kind=kind, mode=mode,
                ),
            )
        )
    return tuple(gates)


def citation_violations(item_results: Sequence[Any]) -> tuple[str, ...]:
    """Return the ids of items whose citation scored zero: never an average.

    One citation that names a chunk the run never retrieved is a correctness
    invariant, not a quality trend, so a single zero fails the experiment.
    """
    violations: list[str] = []
    for result in item_results:
        for evaluation in getattr(result, "evaluations", ()) or ():
            if getattr(evaluation, "name", None) != _CITATION_METRIC:
                continue
            if _numeric(getattr(evaluation, "value", None)) == 0.0:
                violations.append(_item_id(result.item))
    return tuple(sorted(violations))


def _gate_status(
    average: float | None,
    *,
    samples: int,
    threshold: float,
    comparison: str,
    kind: str,
    mode: str,
) -> str:
    if average is None:
        return "insufficient"
    if kind == "llm" and samples < LLM_MIN_SAMPLES:
        return "insufficient"
    if comparison == "max":
        passed = average <= threshold
    else:
        passed = average >= threshold
    if passed:
        return "pass"
    if kind == "llm" and mode != MODE_FULL:
        return "soft"
    return "fail"


def _numeric(value: Any) -> float | None:
    """A score that can be averaged: numbers only, never bools or ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _item_id(item: Any) -> str:
    """Read one dataset item id, accepting the SDK model and a plain mapping."""
    raw = item.get("id") if isinstance(item, Mapping) else getattr(item, "id", None)
    if not isinstance(raw, str) or not raw:
        raise ExperimentConfigError("a dataset item has no usable id")
    return raw


def _metadata_of(item: Any) -> Any:
    if isinstance(item, Mapping):
        return item.get("metadata")
    return getattr(item, "metadata", None)


def _input_of(item: Any) -> Any:
    if isinstance(item, Mapping):
        return item.get("input")
    return getattr(item, "input", None)


def _category_of(item: Any) -> str:
    metadata = _metadata_of(item)
    if isinstance(metadata, Mapping):
        category = metadata.get(_CATEGORY_KEY)
        if isinstance(category, str) and category:
            return category
    return _UNCATEGORIZED


def _query_for(item_id: str, item: Any) -> str:
    input_data = _input_of(item)
    query = input_data.get("query") if isinstance(input_data, Mapping) else None
    if not isinstance(query, str) or not query.strip():
        raise ExperimentConfigError(
            f"dataset item {item_id} has no input.query to send to the agent"
        )
    return query


def _messages_from(state: Any) -> list[Mapping[str, Any]]:
    """Read the checkpoint's message list; a missing list is legal (empty)."""
    if not isinstance(state, Mapping):
        return []
    messages = state.get("messages")
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        return []
    return [message for message in messages if isinstance(message, Mapping)]


def _failure_reason(exc: BaseException) -> str:
    """Name a failure without echoing text this package did not author."""
    module = type(exc).__module__ or ""
    if module.startswith("myflow_evals"):
        return f"{type(exc).__name__}: {exc}"
    return type(exc).__name__


def _join(item_ids: Sequence[str]) -> str:
    return ", ".join(item_ids) if item_ids else "none"


def _validate_mode(mode: str) -> None:
    if mode not in MODES:
        raise ExperimentConfigError(
            f"unknown experiment mode {mode!r}; expected one of {_join(MODES)}"
        )


def _validate_max_items(max_items: int | None) -> None:
    if max_items is None:
        return
    _validate_positive_int(max_items, "max_items")


def _validate_positive_int(value: Any, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ExperimentConfigError(f"{name} must be a positive integer")

"""The myflow-evals command line: build, run, and ingest.

Three subcommands share one secret-loading path and one exit-code contract:
0 when the command did what it was asked, 1 when an experiment gate failed
(a missing item, a task exception, a forged citation, a missed threshold), and 2
when the command could not run as configured (a missing secret, a bad mode, an
unknown account, an unreachable Langfuse).

main returns that code instead of exiting, so a caller -- the console script, the
Task 10 smoke test, or a future workflow -- can inspect it. Nothing here prints a
password, a header value, a cookie, or a raw user id.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langfuse import Langfuse

from myflow_evals.accounts import AccountCredential, AccountSession, load_accounts, login_all
from myflow_evals.api import AgentApi
from myflow_evals.build_dataset import (
    BuildSources,
    DatasetItemDraft,
    TraceSource,
    build_account_items,
    build_report,
    fetch_account_traces,
    load_build_sources,
    write_drafts,
)
from myflow_evals.runner import (
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_MODE,
    MODES,
    ExperimentConfigError,
    ExperimentRunner,
    IncompleteExperiment,
    select_items,
)

EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_CONFIG_ERROR = 2

#: The Gateway origin the fixed accounts log in to (documented AGENT_API form).
_GATEWAY_ENV = "AGENT_API"
#: The salt the Agent uses for its Langfuse user id; both sides must agree.
_SALT_ENV = "USER_ID_SALT"
_LANGFUSE_HOST_ENV = "LANGFUSE_HOST"
_LANGFUSE_PUBLIC_KEY_ENV = "LANGFUSE_PUBLIC_KEY"
_LANGFUSE_SECRET_KEY_ENV = "LANGFUSE_SECRET_KEY"
_MCP_HEADERS_ENV = "MCP_SERVER_HEADERS"

_INGEST_QUERY_PREVIEW = 100


class CliError(RuntimeError):
    """The command cannot start: a required setting or file is missing or unusable."""


def _require(environ: Mapping[str, str], name: str) -> str:
    value = (environ.get(name) or "").strip()
    if not value:
        raise CliError(f"{name} is not set; provide it in the environment or a secret file")
    return value


def _langfuse_client(environ: Mapping[str, str]) -> Langfuse:
    """Build a Langfuse client from the environment; keys are never echoed."""
    return Langfuse(
        host=_require(environ, _LANGFUSE_HOST_ENV),
        public_key=_require(environ, _LANGFUSE_PUBLIC_KEY_ENV),
        secret_key=_require(environ, _LANGFUSE_SECRET_KEY_ENV),
    )


@dataclass(frozen=True)
class CliServices:
    """The external seams the CLI reaches through.

    Defaults are the real package functions, so production code never passes this
    argument; tests substitute every seam and assert what the CLI asked for.
    """

    load_accounts: Callable[[Path | None], Sequence[AccountCredential]] = load_accounts
    login_all: Callable[..., dict[str, AccountSession]] = login_all
    langfuse_client: Callable[[Mapping[str, str]], Any] = _langfuse_client
    load_sources: Callable[..., BuildSources] = load_build_sources
    fetch_traces: Callable[..., list[TraceSource]] = fetch_account_traces
    api: Any = AgentApi


class LangfuseDatasetWriter:
    """DatasetWriter that upserts drafts into one Langfuse dataset.

    Datasets are keyed by name in Langfuse, so creation is idempotent and items
    upsert by their deterministic id. Only the builder writes: the runner treats
    the dataset as frozen input.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self._dataset: str | None = None

    def upsert_items(self, dataset_name: str, items: Sequence[DatasetItemDraft]) -> None:
        """Create the dataset once, then upsert every draft as one item."""
        if self._dataset != dataset_name:
            self._client.create_dataset(name=dataset_name)
            self._dataset = dataset_name
        for draft in items:
            self._client.create_dataset_item(
                dataset_name=dataset_name,
                id=draft.id,
                input=draft.input,
                expected_output=draft.expected_output,
                metadata=draft.metadata,
            )


def main(
    argv: Sequence[str] | None = None,
    *,
    services: CliServices | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Run one subcommand and return its exit code (0, 1, or 2)."""
    parser = _build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        # argparse already printed usage/help; keep its code (2 on a bad flag).
        code = exc.code
        if isinstance(code, int):
            return code
        return EXIT_OK if code is None else EXIT_CONFIG_ERROR

    base = dict(os.environ if environ is None else environ)
    assumed = services if services is not None else CliServices()
    try:
        resolved = _resolve_environ(base, args)
        handlers: dict[str, Callable[..., int]] = {
            "build": _cmd_build,
            "run": _cmd_run,
            "ingest": _cmd_ingest,
        }
        return handlers[args.command](args, resolved, assumed)
    except IncompleteExperiment as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_GATE_FAILED
    except (CliError, ExperimentConfigError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except Exception as exc:  # an unexpected failure is never a pass
        print(f"error: {_error_text(exc)}", file=sys.stderr)
        return EXIT_CONFIG_ERROR


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="myflow-evals",
        description="Black-box evaluations for the MyFlow agent.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--dataset", required=True, help="Langfuse dataset name")
    shared.add_argument("--limit", type=int, default=None, help="cap drafts or items")
    shared.add_argument(
        "--dry-run", action="store_true", help="do everything except writing"
    )
    shared.add_argument(
        "--accounts-file", type=Path, default=None, help="fixed-account secret file"
    )
    shared.add_argument(
        "--mcp-headers-file", type=Path, default=None, help="MCP server header secret file"
    )
    shared.add_argument("--env-file", type=Path, default=None, help="KEY=VALUE settings file")

    commands.add_parser(
        "build",
        parents=[shared],
        help="build dataset drafts from the fixed accounts",
        description="Read each fixed account data and upsert deterministic drafts.",
    )
    run = commands.add_parser(
        "run",
        parents=[shared],
        help="run the frozen dataset through the agent",
        description="Run, score, and gate one frozen dataset.",
    )
    run.add_argument("--run-name", default=None, help="experiment run name")
    run.add_argument(
        "--mode",
        choices=MODES,
        default=DEFAULT_MODE,
        help="ci (30-item subset, soft LLM gates) or full",
    )
    run.add_argument(
        "--max-concurrency",
        type=int,
        default=DEFAULT_MAX_CONCURRENCY,
        help="parallel agent runs (keep small)",
    )
    commands.add_parser(
        "ingest",
        parents=[shared],
        help="print the failed online traces that would join the explore set",
        description="Read failed or low-scored traces; Task 9 writes nothing.",
    )
    return parser


def _resolve_environ(base: Mapping[str, str], args: argparse.Namespace) -> dict[str, str]:
    """Merge --env-file and --mcp-headers-file under the process environment.

    Real environment values win, so a CI secret is never overridden by a file that
    happens to sit in the checkout.
    """
    environ = dict(base)
    if args.env_file is not None:
        for key, value in _read_env_file(args.env_file).items():
            environ.setdefault(key, value)
    if args.mcp_headers_file is not None and not (environ.get(_MCP_HEADERS_ENV) or "").strip():
        environ[_MCP_HEADERS_ENV] = _read_text(args.mcp_headers_file, _MCP_HEADERS_ENV)
    return environ


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in _read_text(path, "environment file").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _read_text(path: Path, what: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise CliError(f"cannot read the {what} at {path}: {exc.strerror or exc}") from None


def _login(
    environ: Mapping[str, str], services: CliServices, accounts_file: Path | None
) -> dict[str, AccountSession]:
    """Log in every fixed account, keyed by Langfuse hashed user id."""
    base_url = _require(environ, _GATEWAY_ENV)
    salt = _require(environ, _SALT_ENV)
    accounts = services.load_accounts(accounts_file)
    if not accounts:
        raise CliError("no fixed accounts were configured to log in")
    sessions = services.login_all(accounts, base_url=base_url, salt=salt)
    if not sessions:
        raise CliError("no account finished logging in")
    return sessions


def _cmd_run(
    args: argparse.Namespace, environ: Mapping[str, str], services: CliServices
) -> int:
    _require(environ, _LANGFUSE_HOST_ENV)
    _require(environ, _LANGFUSE_PUBLIC_KEY_ENV)
    _require(environ, _LANGFUSE_SECRET_KEY_ENV)
    run_name = args.run_name or _default_run_name(args.mode)
    if args.dry_run:
        return _plan_run(args, environ, services, run_name)
    sessions = _login(environ, services, args.accounts_file)
    runner = ExperimentRunner(
        services.langfuse_client(environ), sessions=sessions, api=services.api
    )
    result = runner.run(
        args.dataset,
        run_name,
        mode=args.mode,
        max_items=args.limit,
        max_concurrency=args.max_concurrency,
    )
    print(result.render())
    return EXIT_OK if result.passed else EXIT_GATE_FAILED


def _plan_run(
    args: argparse.Namespace,
    environ: Mapping[str, str],
    services: CliServices,
    run_name: str,
) -> int:
    """Print what a run would execute; start nothing and log in nowhere."""
    dataset = services.langfuse_client(environ).get_dataset(args.dataset)
    items = list(getattr(dataset, "items", ()) or ())
    if not items:
        raise CliError(f"dataset {args.dataset!r} has no items; build it before running it")
    selected = select_items(items, mode=args.mode, max_items=args.limit)
    print(f"dataset: {args.dataset}")
    print(f"mode: {args.mode}")
    print(f"run: {run_name}")
    print(f"items: {len(selected)} of {len(items)} selected")
    print("dry-run: nothing was written and no run was started")
    return EXIT_OK


def _default_run_name(mode: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{mode}-{stamp}"


def _cmd_build(
    args: argparse.Namespace, environ: Mapping[str, str], services: CliServices
) -> int:
    sessions = _login(environ, services, args.accounts_file)
    drafts: list[DatasetItemDraft] = []
    per_account: list[BuildSources] = []
    for hashed_user_id in sorted(sessions):
        session = sessions[hashed_user_id]
        sources = services.load_sources(session, limit=args.limit, environ=environ)
        per_account.append(sources)
        drafts.extend(build_account_items(session, sources, limit=args.limit))
    merged = _merge_sources(per_account)
    report = build_report(
        args.dataset, merged, drafts, limit=args.limit, dry_run=args.dry_run
    )
    writer = (
        None
        if args.dry_run
        else LangfuseDatasetWriter(services.langfuse_client(environ))
    )
    written = write_drafts(args.dataset, drafts, writer, dry_run=args.dry_run)
    print(report.render())
    if not args.dry_run:
        print(f"written: {written} item(s) to {args.dataset}")
    return EXIT_OK


def _merge_sources(all_sources: Sequence[BuildSources]) -> BuildSources:
    """Fold every account sources into one report input; counts stay real."""
    if len(all_sources) == 1:
        return all_sources[0]
    totals: dict[str, int] = {}
    for sources in all_sources:
        for kind, count in sources.source_totals.items():
            totals[kind] = totals.get(kind, 0) + count
    return BuildSources(
        tools=[tool for sources in all_sources for tool in sources.tools],
        skills=[skill for sources in all_sources for skill in sources.skills],
        knowledge=[document for sources in all_sources for document in sources.knowledge],
        traces=[trace for sources in all_sources for trace in sources.traces],
        warnings=tuple(warning for sources in all_sources for warning in sources.warnings),
        source_totals=totals,
    )


def _cmd_ingest(
    args: argparse.Namespace, environ: Mapping[str, str], services: CliServices
) -> int:
    _require(environ, _LANGFUSE_HOST_ENV)
    _require(environ, _LANGFUSE_PUBLIC_KEY_ENV)
    _require(environ, _LANGFUSE_SECRET_KEY_ENV)
    sessions = _login(environ, services, args.accounts_file)
    drafts = 0
    print(f"dataset: {args.dataset}")
    for hashed_user_id in sorted(sessions):
        traces = services.fetch_traces(
            sessions[hashed_user_id].hashed_user_id, limit=args.limit, environ=environ
        )
        for trace in traces:
            drafts += 1
            print(f"{_trace_category(trace)}: {trace.trace_id} -> {_preview(trace.query)}")
    print(f"ingest: {drafts} draft(s) for {args.dataset}; nothing was written")
    return EXIT_OK


def _trace_category(trace: TraceSource) -> str:
    return trace.category or "trace_replay"


def _preview(text: str) -> str:
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= _INGEST_QUERY_PREVIEW:
        return collapsed
    return collapsed[:_INGEST_QUERY_PREVIEW] + "..."


def _error_text(exc: BaseException) -> str:
    """Name a failure without echoing text this package did not author."""
    module = type(exc).__module__ or ""
    if module.startswith("myflow_evals"):
        return f"{type(exc).__name__}: {exc}"
    return type(exc).__name__

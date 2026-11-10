"""Dataset drafts built from the fixed account's real tools, Skills, knowledge, and traces.

This module is a data-source reader plus a pure synthesizer: it imports neither
backend.app nor agentkit. Every draft is derived from what the account can
actually see -- an MCP tool's real inputSchema, a private Skill's metadata, the
longest of three sampled chunks of a knowledge document, or one of the account's
own failed/low-scored Langfuse traces -- and carries an id derived from its
source. There is no LLM in this path, so a build costs nothing; a seeded build is
reproducible, and the categories that need synthesis or human review are left to
a later phase (see evals/README.md).

Rules that shape the drafts:

* Tools come from the Gateway's stored projection only: one
  GET /api/mcp-servers/{id}/tools?include_schema=true per effective server. This
  package opens no MCP session of its own and holds no server credentials.
* Tool arguments come from the required properties of the real inputSchema only
  (first enum value, else a type placeholder); nothing is guessed.
* Only effective sources are sampled: an MCP server must be enabled, active,
  and reachable, a tool must be both callable and enabled in the Gateway tool
  index and carry its stored inputSchema, and the account's own private Skills --
  builtin and public Skills are excluded -- must be enabled and active, with a
  skill_id and a name.
* Knowledge drafts carry document/chunk provenance in metadata but emit only
  expected_doc_ids: chunk ids are an implementation detail of the retriever.
* Traces are optional input: without LANGFUSE_HOST and keys, there are none.
* metadata.user_id is always the Langfuse hashed user id, never the raw
  /me.id, a username, or a password.

Writes go through the DatasetWriter seam so dry runs touch nothing and the
Langfuse-backed writer can arrive in a later task.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from myflow_evals.accounts import AccountSession

_KIND_TOOL = "tool"
_KIND_SKILL = "skill"
_KIND_KNOWLEDGE = "knowledge"
_KIND_TRACE = "trace"

#: Fixed kind order: reports and drafts never depend on dict iteration quirks.
_KIND_ORDER = (_KIND_TOOL, _KIND_SKILL, _KIND_KNOWLEDGE, _KIND_TRACE)

#: Sample this many chunks and keep the longest readable one, not chunk index 0.
_CHUNK_SAMPLE_SIZE = 3
#: Other readable chunks carried beside the primary one for multi-hop questions.
_CHUNK_RELATED_LIMIT = 2
#: A chunk without a usable index still competes, but loses every tie.
_UNINDEXED_CHUNK_INDEX = float("inf")
_PRIVATE_SCOPE = "private"
_BUILTIN_SCOPE = "builtin"
#: The only MCP status the Agent can route to; pending/error/disabled are not.
_ACTIVE_STATUS = "active"

_TRACE_SOURCE = "online_trace"
_TRACE_REPLAY_CATEGORY = "trace_replay"
_TRACE_ERROR_LEVEL = "ERROR"
_TRACE_LOW_SCORE = 0.5
_TRACE_PAGE_SIZE = 100
_HUMAN_TURN_TYPES = frozenset({"human", "user"})
#: Guard against a Langfuse deployment that reports an absurd page count.
_TRACE_MAX_PAGES = 20
_TRACES_PATH = "/api/public/traces"
_HOST_ENV = "LANGFUSE_HOST"
_PUBLIC_KEY_ENV = "LANGFUSE_PUBLIC_KEY"
_SECRET_KEY_ENV = "LANGFUSE_SECRET_KEY"

_DEFAULT_TIMEOUT_SECONDS = 60.0

#: Mirrors agentkit.mcp.store.tool_repository.is_safe_call_name (copied, never
#: imported: this package must not depend on agentkit). The backend refuses to
#: publish a call name the model cannot call back, so a sample built from it
#: could never pass.
_CALL_NAME_PREFIX = "mcp__"
_CALL_NAME_SEPARATOR = "__"
_MAX_CALL_NAME_CHARS = 128
_MAX_SERVER_NAME_CHARS = 128
_MAX_TOOL_NAME_CHARS = 128
_SAFE_CALL_NAME = re.compile(
    rf"^{_CALL_NAME_PREFIX}[A-Za-z0-9_-]{{1,{_MAX_SERVER_NAME_CHARS}}}"
    rf"{_CALL_NAME_SEPARATOR}[A-Za-z0-9][A-Za-z0-9_-]{{0,{_MAX_TOOL_NAME_CHARS - 1}}}$"
)


@dataclass(frozen=True)
class McpTool:
    """One tool as the Gateway stores it, including its real input schema.

    Every field comes from one mcp_tools row: the call name the Agent uses, the
    remote tool name, the description, and the verbatim inputSchema the sample
    arguments are derived from. Nothing here is re-fetched from the server.
    """

    call_name: str
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class _ServerRef:
    """One effective MCP server of the fixed account, as the Gateway names it."""

    id: str
    name: str


@dataclass(frozen=True)
class SkillSource:
    """One private Skill of the fixed account, as GET /api/skills reports it."""

    skill_id: str
    name: str
    description: str
    git_commit: str | None = None


@dataclass(frozen=True)
class KnowledgeSource:
    """One knowledge document plus the sampled chunk that answers questions about it.

    related_chunks carries up to two other readable chunks from the same sampled
    page, in chunk-index order, so a multi-hop question can be built without a
    second fetch. section and page locate the primary chunk for provenance and
    for a query that names where the answer lives. They default to empty values
    so every phase 1 caller keeps constructing the same source.
    """

    knowledge_base_id: str
    document_id: str
    filename: str
    chunk_id: str
    chunk_content: str
    chunk_count: int
    related_chunks: tuple[tuple[str, str], ...] = ()
    section: str = ""
    page: int = 0


@dataclass(frozen=True)
class TraceSource:
    """One failed or low-scored trace of this account, reduced to its question."""

    trace_id: str
    query: str
    category: str | None = None


@dataclass(frozen=True)
class BuildSources:
    """Everything one build may sample from, already fetched and filtered.

    source_totals records how many sources each kind really has, so a report can
    distinguish "2 drafts from 37 sources" when a limit caps the draft set.
    """

    tools: Sequence[McpTool] = ()
    skills: Sequence[SkillSource] = ()
    knowledge: Sequence[KnowledgeSource] = ()
    traces: Sequence[TraceSource] = ()
    warnings: Sequence[str] = ()
    source_totals: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class DatasetItemDraft:
    """One Dataset item before it is uploaded; input/expected_output are Langfuse's."""

    id: str
    input: dict[str, Any]
    expected_output: dict[str, Any]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class BuildReport:
    """Deterministic counts of one build, so a limit cannot pass for a full build."""

    dataset_name: str
    limit: int | None
    dry_run: bool
    source_counts: dict[str, int]
    draft_counts: dict[str, int]
    warnings: Sequence[str] = ()

    def render(self) -> str:
        """Render the report as report lines; the CLI prints them, this module does not."""
        limit = "none" if self.limit is None else f"{self.limit} per kind"
        mode = "dry-run (writes nothing)" if self.dry_run else "write"
        lines = [f"dataset: {self.dataset_name}", f"limit: {limit}", f"mode: {mode}"]
        for kind in _KIND_ORDER:
            lines.append(
                f"{kind}: {self.draft_counts.get(kind, 0)} drafts from "
                f"{self.source_counts.get(kind, 0)} sources"
            )
        lines.append(f"total: {sum(self.draft_counts.values())} drafts")
        for warning in self.warnings:
            lines.append(f"warning: {warning}")
        return "\n".join(lines)


class DatasetWriter(Protocol):
    """Where finished drafts go. Langfuse implements this in a later task."""

    def upsert_items(self, dataset_name: str, items: Sequence[DatasetItemDraft]) -> None: ...


def stable_item_id(kind: str, source_id: str, ordinal: int, user_hash: str) -> str:
    """Return the deterministic item id for one draft.

    The same (kind, source_id, ordinal, user_hash) always yields the same id, so
    rebuilding a dataset is idempotent, while another account's hash never
    collides with this account's items.
    """
    material = f"{kind}\x1f{source_id}\x1f{ordinal}\x1f{user_hash}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"{kind}-{digest}"


def load_build_sources(
    session: AccountSession,
    *,
    limit: int | None = None,
    environ: Mapping[str, str] | None = None,
    trace_transport: httpx.BaseTransport | None = None,
    include_traces: bool = True,
    rng: random.Random | None = None,
) -> BuildSources:
    """Fetch this account's real tools, private Skills, knowledge documents, and traces.

    Tools are read from the Gateway's own projection of the account's mcp_tools
    rows: one GET /api/mcp-servers/{id}/tools?include_schema=true per effective
    server. The Gateway already stores each tool's verbatim inputSchema beside its
    callable/enabled flags, so this costs one request per server and needs no
    per-server credential -- this package never opens an MCP session.

    A tool whose call name the Agent could not call back (a dot, or a name beyond
    the provider bound) is skipped with a warning: a sample built from it could
    never pass.

    limit bounds the work a capped build does: only the first limit documents get
    their chunk fetched, and the returned source lists are capped per kind. Trace
    reading still pages the whole (page-guarded) trace set, and source_totals
    always records how many sources each kind really has.

    include_traces=False skips Langfuse entirely: a trace replay has no
    expectations, so a dataset built for scoring must not contain one. The trace
    count is then a real 0, not a silently skipped kind.

    rng supplies the seeded randomness for knowledge chunk sampling. A build
    with the same seed sees the same reference answers; item ids never depend on
    the draw.

    A Gateway failure that makes the account data unusable raises. Everything
    this account cannot use -- a server that is disabled, not active, or
    unreachable, a tool the Gateway tool index does not mark callable and enabled,
    a tool whose projection carries no usable schema, a Skill that is switched off
    or not active, a document without a readable chunk, an unavailable Langfuse --
    is skipped with a warning on the result, so one broken source never costs more
    than its own items.
    """
    _validate_limit(limit)
    rng = random.Random() if rng is None else rng
    env = os.environ if environ is None else environ
    warnings: list[str] = []
    tools = _account_tools(session, warnings=warnings)
    skills = _account_skills(session, warnings=warnings)
    knowledge, knowledge_total = _account_knowledge(
        session, limit=limit, warnings=warnings, rng=rng
    )
    traces = (
        fetch_account_traces(
            session.hashed_user_id,
            environ=env,
            transport=trace_transport,
            warnings=warnings,
        )
        if include_traces
        else []
    )
    return BuildSources(
        tools=_capped(tools, limit),
        skills=_capped(skills, limit),
        knowledge=knowledge,
        traces=_capped(traces, limit),
        warnings=tuple(warnings),
        source_totals={
            _KIND_TOOL: len(tools),
            _KIND_SKILL: len(skills),
            _KIND_KNOWLEDGE: knowledge_total,
            _KIND_TRACE: len(traces),
        },
    )


def fetch_account_traces(
    user_hash: str,
    *,
    limit: int | None = None,
    environ: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
    warnings: list[str] | None = None,
) -> list[TraceSource]:
    """Read this account's failed or low-scored traces from Langfuse over HTTP.

    Traces are optional: without LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY, and
    LANGFUSE_SECRET_KEY this returns [] and makes no request. Only a trace that
    failed (level == ERROR) or carries a score below 0.5 is kept, and it must
    still have question text to replay.

    Langfuse being unavailable must not fail a build: the traces kept so far are
    returned and one secret-free warning is appended to the warnings list.
    """
    _validate_limit(limit)
    env = os.environ if environ is None else environ
    host = (env.get(_HOST_ENV) or "").strip()
    public_key = (env.get(_PUBLIC_KEY_ENV) or "").strip()
    secret_key = (env.get(_SECRET_KEY_ENV) or "").strip()
    if not host or not public_key or not secret_key:
        return []

    kept: list[TraceSource] = []
    with httpx.Client(
        base_url=host.rstrip("/"),
        auth=(public_key, secret_key),
        timeout=_DEFAULT_TIMEOUT_SECONDS,
        transport=transport,
    ) as client:
        page = 1
        while page <= _TRACE_MAX_PAGES:
            try:
                rows, total_pages = _trace_page(client, user_hash, page)
            except RuntimeError as exc:
                if warnings is not None:
                    warnings.append(f"stopped reading Langfuse traces after page {page}: {exc}")
                return kept
            for row in rows:
                trace = _trace_source(row)
                if trace is None:
                    continue
                kept.append(trace)
                if limit is not None and len(kept) >= limit:
                    return kept
            if total_pages is None or page >= total_pages:
                break
            page += 1
    return kept


def is_safe_call_name(name: str) -> bool:
    """False for a call name the Agent would refuse to publish (mirror rule).

    The whole name must be mcp__<server slug>__<tool>, bounded to a length every
    provider accepts, with a tool part limited to [A-Za-z0-9_-]: MCP also allows
    a dot, but no provider accepts one in a function name.
    """
    text = str(name or "")
    return (
        bool(text)
        and len(text) <= _MAX_CALL_NAME_CHARS
        and bool(_SAFE_CALL_NAME.fullmatch(text))
    )


def build_account_items(
    session: AccountSession,
    sources: BuildSources,
    *,
    limit: int | None = None,
) -> list[DatasetItemDraft]:
    """Synthesize one deterministic draft per source, capped per kind by limit.

    Pure: no I/O, no clock, no randomness. The session supplies only the hashed
    user id that goes into metadata.
    """
    _validate_limit(limit)
    user_hash = session.hashed_user_id
    if not isinstance(user_hash, str) or not user_hash:
        raise ValueError("the account session has no hashed user id; log in first")

    drafts: list[DatasetItemDraft] = []
    for tool in _capped(sources.tools, limit):
        if not is_safe_call_name(tool.call_name):
            continue  # the Agent cannot call it, so a sample would be unpassable
        drafts.append(_tool_draft(tool, user_hash))
    for skill in _capped(sources.skills, limit):
        drafts.append(_skill_draft(skill, user_hash))
    for document in _capped(sources.knowledge, limit):
        drafts.append(_knowledge_draft(document, user_hash))
    for trace in _capped(sources.traces, limit):
        drafts.append(_trace_draft(trace, user_hash))
    return drafts


def build_report(
    dataset_name: str,
    sources: BuildSources,
    drafts: Sequence[DatasetItemDraft],
    *,
    limit: int | None = None,
    dry_run: bool = False,
) -> BuildReport:
    """Count what a build saw and produced, per source kind.

    source_counts is the real number of sources each kind has (source_totals),
    never the capped sample, so a limit can never pass for a full build.
    """
    source_counts = {
        _KIND_TOOL: sources.source_totals.get(_KIND_TOOL, len(sources.tools)),
        _KIND_SKILL: sources.source_totals.get(_KIND_SKILL, len(sources.skills)),
        _KIND_KNOWLEDGE: sources.source_totals.get(_KIND_KNOWLEDGE, len(sources.knowledge)),
        _KIND_TRACE: sources.source_totals.get(_KIND_TRACE, len(sources.traces)),
    }
    draft_counts = {kind: 0 for kind in _KIND_ORDER}
    for draft in drafts:
        kind = draft.metadata.get("source_kind")
        if kind in draft_counts:
            draft_counts[kind] += 1
    return BuildReport(
        dataset_name=dataset_name,
        limit=limit,
        dry_run=dry_run,
        source_counts=source_counts,
        draft_counts=draft_counts,
        warnings=tuple(sources.warnings),
    )


def write_drafts(
    dataset_name: str,
    drafts: Sequence[DatasetItemDraft],
    writer: DatasetWriter | None,
    dry_run: bool = False,
) -> int:
    """Upsert every draft into one dataset and return how many were written.

    A dry run returns 0 and does not touch the writer at all, so an operator can
    inspect a report without creating or changing a Langfuse dataset.
    """
    if not isinstance(dataset_name, str) or not dataset_name:
        raise ValueError("dataset_name must be a non-empty string")
    if dry_run:
        return 0
    if writer is None:
        raise ValueError("writing drafts needs a DatasetWriter; pass dry_run=True to skip writes")
    writer.upsert_items(dataset_name, list(drafts))
    return len(drafts)


def _account_tools(session: AccountSession, *, warnings: list[str]) -> list[McpTool]:
    """Read every effective server's stored tool projection: the whole tool source.

    One GET /api/mcp-servers/{id}/tools?include_schema=true per effective server
    yields the call name, both effectiveness flags and the verbatim inputSchema
    from the same row, so there is no second lookup and no MCP traffic.
    """
    body = _get_object(session, "/mcp-servers")
    summaries = body.get("servers")
    if not isinstance(summaries, list):
        raise RuntimeError("GET /mcp-servers returned no servers list")
    tools: list[McpTool] = []
    for summary in summaries:
        server = _mcp_server(summary, warnings)
        if server is None:
            continue
        for row in _server_tool_rows(session, server, warnings):
            tool = _projected_tool(row, server, warnings)
            if tool is not None:
                tools.append(tool)
    return tools


def _mcp_server(summary: object, warnings: list[str]) -> _ServerRef | None:
    """Keep a server the Agent can route to; everything else is a warning.

    Transport and url no longer matter: this build never calls the server, it
    reads what the Gateway already stored about it.
    """
    if not isinstance(summary, Mapping):
        warnings.append("skipped an /api/mcp-servers entry that is not an object")
        return None
    server_id = _text(summary.get("id"))
    if server_id is None:
        warnings.append("skipped an MCP server without an id")
        return None
    # The rows carry their own call names, so the server name is only the label a
    # warning uses; falling back to the id keeps every warning attributable.
    name = _text(summary.get("name")) or server_id
    if not _effective_server(summary, name, warnings):
        return None
    return _ServerRef(id=server_id, name=name)


def _effective_server(
    summary: Mapping[str, Any], name: str, warnings: list[str]
) -> bool:
    """Mirror agentkit's rule: enabled AND status=active AND reachable.

    An absent flag is not True. A summary that does not say the server is
    enabled and reachable cannot prove the Agent may use it, so it is skipped
    rather than guessed about.
    """
    enabled = summary.get("enabled")
    if enabled is not True:
        if enabled is False:
            warnings.append(f"skipped MCP server {name!r}: it is disabled for this account")
        else:
            warnings.append(
                f"skipped MCP server {name!r}: it has no enabled flag, so it is not effective"
            )
        return False
    status = _text(summary.get("status"))
    if status is None:
        warnings.append(
            f"skipped MCP server {name!r}: it has no status, so it is not effective"
        )
        return False
    if status != _ACTIVE_STATUS:
        warnings.append(
            f"skipped MCP server {name!r}: status={status} is not {_ACTIVE_STATUS}"
        )
        return False
    reachable = summary.get("reachable")
    if reachable is not True:
        if reachable is False:
            warnings.append(f"skipped MCP server {name!r}: it is unreachable")
        else:
            warnings.append(
                f"skipped MCP server {name!r}: it has no reachable flag, so it is not effective"
            )
        return False
    return True


def _tool_flag_problem(row: Mapping[str, Any], flag: str) -> str | None:
    """Say why one projection flag is not usable, or None when it is True.

    An absent key is not True either, and the warning says which it is: a row
    that never carried the flag is a different problem from one that switched it
    off, and an operator acts on them differently.
    """
    if flag not in row:
        return f"the Gateway tool index row has no {flag} flag"
    value = row[flag]
    if value is True:
        return None
    if value is False:
        return f"the Gateway tool index row has {flag}=false"
    return f"the Gateway tool index row has {flag}={value!r}"


def _server_tool_rows(
    session: AccountSession, server: _ServerRef, warnings: list[str]
) -> list[Mapping[str, Any]]:
    """Read one server's stored tool projection, schema included, exactly once.

    The projection is the whole tool source: it carries the two flags that decide
    whether the Agent can call a tool at all and the verbatim inputSchema the
    sample arguments come from. A server whose projection cannot be read
    contributes nothing -- effectiveness cannot be established there, and guessing
    would build samples that could never pass -- but it never aborts the build.
    """
    try:
        body = _get_object(
            session,
            f"/mcp-servers/{server.id}/tools",
            params={"include_schema": "true"},
        )
    except httpx.HTTPStatusError as exc:
        warnings.append(
            f"skipped MCP server {server.name!r}: its tool index request failed "
            f"with HTTP {exc.response.status_code}"
        )
        return []
    except (RuntimeError, httpx.HTTPError) as exc:
        warnings.append(
            f"skipped MCP server {server.name!r}: its tool index is unreadable ({exc})"
        )
        return []
    rows = body.get("tools")
    if not isinstance(rows, list):
        warnings.append(
            f"skipped MCP server {server.name!r}: its tool index returned no tools list"
        )
        return []
    usable: list[Mapping[str, Any]] = []
    for row in rows:
        if isinstance(row, Mapping):
            usable.append(row)
        else:
            # Every other skip warns; a row this package cannot read must too.
            warnings.append(
                f"skipped a tool of server {server.name!r}: its Gateway tool index "
                "entry is not an object"
            )
    return usable


def _projected_tool(
    row: Mapping[str, Any], server: _ServerRef, warnings: list[str]
) -> McpTool | None:
    """Turn one projection row into a sample source, or skip it with a warning.

    A tool is sampled only when the Agent could really call it with arguments
    derived from a real schema: callable AND enabled in the Gateway index, a call
    name the provider accepts, and a stored inputSchema that is a JSON object. No
    schema means no sample -- the arguments would have to be invented.
    """
    name = _text(row.get("name"))
    call_name = _text(row.get("call_name"))
    if name is None or call_name is None:
        # Say what is really absent: a row can carry a call_name and no name.
        label = name or call_name or "an unnamed tool"
        absent = (
            "no name to label it by" if name is None else "no call_name to call it by"
        )
        warnings.append(
            f"skipped tool {label!r} of server {server.name!r}: its Gateway tool "
            f"index row has {absent}"
        )
        return None
    if not is_safe_call_name(call_name):
        warnings.append(
            f"skipped tool {name!r} of server {server.name!r}: "
            f"the Agent cannot call {call_name!r}"
        )
        return None
    problem = _tool_flag_problem(row, "callable")
    if problem is None:
        problem = _tool_flag_problem(row, "enabled")
    if problem is not None:
        warnings.append(f"skipped tool {name!r} of server {server.name!r}: {problem}")
        return None
    if "input_schema" not in row:
        warnings.append(
            f"skipped tool {name!r} of server {server.name!r}: its Gateway tool "
            "index row has no input_schema, so its arguments would be a guess"
        )
        return None
    schema = row.get("input_schema")
    if not isinstance(schema, Mapping):
        warnings.append(
            f"skipped tool {name!r} of server {server.name!r}: its Gateway tool "
            "index row has a non-object input_schema"
        )
        return None
    description = row.get("description")
    return McpTool(
        call_name=call_name,
        name=name,
        description=description if isinstance(description, str) else "",
        input_schema=dict(schema),
    )


def _account_skills(session: AccountSession, *, warnings: list[str]) -> list[SkillSource]:
    body = _get_object(session, "/skills")
    rows = body.get("skills")
    if not isinstance(rows, list):
        raise RuntimeError("GET /skills returned no skills list")
    skills: list[SkillSource] = []
    for row in rows:
        skill = _private_skill(row, warnings)
        if skill is not None:
            skills.append(skill)
    return skills


def _private_skill(row: object, warnings: list[str]) -> SkillSource | None:
    """Keep the account's own effective private Skills.

    Public, builtin, and incomplete rows are dropped silently: they are not
    samples this account can switch on. A private row that is switched off or
    not active is dropped with a warning, because a sample built from it could
    never pass.
    """
    if not isinstance(row, Mapping):
        return None
    if row.get("scope") != _PRIVATE_SCOPE:
        return None
    if row.get("category") == _BUILTIN_SCOPE:
        return None
    skill_id = _text(row.get("skill_id"))
    name = _text(row.get("name"))
    if skill_id is None or name is None:
        return None
    if row.get("enabled") is not True:
        warnings.append(
            f"skipped private Skill {name!r} ({skill_id}): it is not enabled for this account"
        )
        return None
    status = row.get("status")
    if status is not None and status != _ACTIVE_STATUS:
        warnings.append(
            f"skipped private Skill {name!r} ({skill_id}): status={status} is not {_ACTIVE_STATUS}"
        )
        return None
    description = row.get("description")
    return SkillSource(
        skill_id=skill_id,
        name=name,
        description=description if isinstance(description, str) else "",
        git_commit=_text(row.get("git_commit")),
    )


def _account_knowledge(
    session: AccountSession,
    *,
    limit: int | None,
    warnings: list[str],
    rng: random.Random,
) -> tuple[list[KnowledgeSource], int]:
    """Return the sampled documents and how many documents the account really has."""
    documents = _knowledge_documents(session)
    sources: list[KnowledgeSource] = []
    for base_id, document in documents:
        if limit is not None and len(sources) >= limit:
            break
        source = _knowledge_source(session, base_id, document, warnings, rng=rng)
        if source is not None:
            sources.append(source)
    return sources, len(documents)


def _knowledge_documents(session: AccountSession) -> list[tuple[str, object]]:
    """List every document of every base without fetching a single chunk."""
    body = _get_object(session, "/knowledge/bases")
    bases = body.get("bases")
    if not isinstance(bases, list):
        raise RuntimeError("GET /knowledge/bases returned no bases list")
    documents: list[tuple[str, object]] = []
    for base in bases:
        if not isinstance(base, Mapping):
            continue
        base_id = _text(base.get("id"))
        if base_id is None:
            continue
        documents_body = _get_object(session, f"/knowledge/bases/{base_id}/documents")
        rows = documents_body.get("documents")
        if not isinstance(rows, list):
            raise RuntimeError(f"GET /knowledge/bases/{base_id}/documents returned no documents list")
        documents.extend((base_id, row) for row in rows if isinstance(row, Mapping))
    return documents


def _knowledge_source(
    session: AccountSession,
    base_id: str,
    document: object,
    warnings: list[str],
    *,
    rng: random.Random,
) -> KnowledgeSource | None:
    if not isinstance(document, Mapping):
        return None
    document_id = _text(document.get("id"))
    if document_id is None:
        return None
    try:
        chunks = _sampled_chunks(session, base_id, document_id, rng=rng)
    except _MalformedChunkCount:
        warnings.append(
            f"skipped knowledge document {document_id!r} of base {base_id!r}: "
            "its chunk count is missing or malformed"
        )
        return None
    except httpx.HTTPStatusError as exc:
        warnings.append(
            f"skipped knowledge document {document_id!r} of base {base_id!r}: "
            f"its chunk page could not be read (HTTP {exc.response.status_code})"
        )
        return None
    except (RuntimeError, httpx.HTTPError) as exc:
        warnings.append(
            f"skipped knowledge document {document_id!r} of base {base_id!r}: "
            f"its chunk page could not be read ({exc})"
        )
        return None
    if not chunks:
        warnings.append(
            f"skipped knowledge document {document_id!r} of base {base_id!r}: "
            "it has no readable chunk"
        )
        return None
    _, chunk_id, chunk_content, section, page = _primary_chunk(chunks)
    related = tuple(
        (candidate_id, candidate_content)
        for _, candidate_id, candidate_content, _, _ in chunks
        if candidate_id != chunk_id and candidate_content.strip()
    )[:_CHUNK_RELATED_LIMIT]
    chunk_count = document.get("chunk_count")
    return KnowledgeSource(
        knowledge_base_id=base_id,
        document_id=document_id,
        filename=_text(document.get("filename")) or "",
        chunk_id=chunk_id,
        chunk_content=chunk_content,
        chunk_count=chunk_count if isinstance(chunk_count, int) else 0,
        related_chunks=related,
        section=section,
        page=page,
    )


class _MalformedChunkCount(Exception):
    """The chunk endpoint did not report a usable total chunk count."""


def _sampled_chunk(
    session: AccountSession,
    base_id: str,
    document_id: str,
    *,
    rng: random.Random,
) -> tuple[str, str] | None:
    """Return the longest readable chunk of a seeded three-chunk sample.

    Kept for the phase 1 callers and tests; the expanded synthesizer reads
    _sampled_chunks directly so it can carry the other readable chunks without a
    second request. Blank chunks are never candidates, and only a stripped
    content length ranks them.
    """
    chunks = _sampled_chunks(session, base_id, document_id, rng=rng)
    if not chunks:
        return None
    _, chunk_id, content, _, _ = _primary_chunk(chunks)
    return chunk_id, content


def _sampled_chunks(
    session: AccountSession,
    base_id: str,
    document_id: str,
    *,
    rng: random.Random,
) -> list[tuple[int | float, str, str, str, int]]:
    """Return every readable chunk of a seeded three-chunk sample.

    A single-chunk document reuses the count probe. Two or three chunks need one
    full page; a larger document gets three distinct random offsets, sorted so a
    seeded run issues the same requests. Counting costs one request and sampling
    at most three, so no document ever needs more than four chunk requests.
    Candidates are deduplicated by chunk id and returned in chunk-index order; a
    missing or malformed count raises _MalformedChunkCount so the caller can
    report the contract drift instead of calling it unreadable content.
    """
    path = f"/knowledge/bases/{base_id}/documents/{document_id}/chunks"
    probe = _get_object(session, path, params={"offset": 0, "limit": 1})
    count = probe.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise _MalformedChunkCount
    if count == 1:
        chunks = probe.get("chunks")
    elif count <= _CHUNK_SAMPLE_SIZE:
        body = _get_object(session, path, params={"offset": 0, "limit": count})
        chunks = body.get("chunks")
    else:
        chunks = []
        for offset in sorted(rng.sample(range(count), _CHUNK_SAMPLE_SIZE)):
            body = _get_object(session, path, params={"offset": offset, "limit": 1})
            page = body.get("chunks")
            if isinstance(page, list):
                chunks.extend(page)
    if not isinstance(chunks, list):
        return []
    readable: dict[str, tuple[int | float, str, str, str, int]] = {}
    for chunk in chunks:
        candidate = _readable_chunk(chunk)
        if candidate is not None:
            readable.setdefault(candidate[1], candidate)
    return sorted(readable.values(), key=lambda candidate: (candidate[0], candidate[1]))


def _primary_chunk(
    chunks: Sequence[tuple[int | float, str, str, str, int]],
) -> tuple[int | float, str, str, str, int]:
    """Pick the longest readable chunk; ties keep the smallest chunk index."""
    return min(chunks, key=lambda candidate: (-len(candidate[2].strip()), candidate[0]))


def _readable_chunk(chunk: object) -> tuple[int | float, str, str, str, int] | None:
    """Return (index, id, content, section, page) for a non-blank Gateway chunk."""
    if not isinstance(chunk, Mapping):
        return None
    chunk_id = _text(chunk.get("chunk_id"))
    chunk_index = chunk.get("chunk_index")
    content = chunk.get("content")
    if chunk_id is None:
        return None
    if isinstance(chunk_index, bool) or not isinstance(chunk_index, int):
        chunk_index = _UNINDEXED_CHUNK_INDEX
    if not isinstance(content, str) or not content.strip():
        return None
    section = _text(chunk.get("section")) or ""
    page = chunk.get("page")
    page_number = page if isinstance(page, int) and not isinstance(page, bool) else 0
    return chunk_index, chunk_id, content, section, page_number


def _trace_page(
    client: httpx.Client, user_hash: str, page: int
) -> tuple[list[Any], int | None]:
    try:
        response = client.get(
            _TRACES_PATH,
            params={"userId": user_hash, "page": page, "limit": _TRACE_PAGE_SIZE},
        )
    except Exception as exc:
        raise RuntimeError(f"Langfuse traces are unreachable ({type(exc).__name__})") from None
    if response.status_code >= 400:
        raise RuntimeError(f"Langfuse traces request failed with HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        raise RuntimeError("Langfuse traces returned a non-JSON response") from None
    if not isinstance(body, Mapping):
        raise RuntimeError("Langfuse traces returned a non-object JSON response")
    rows = body.get("data")
    if not isinstance(rows, list):
        raise RuntimeError("Langfuse traces returned no data list")
    meta = body.get("meta")
    total_pages = meta.get("totalPages") if isinstance(meta, Mapping) else None
    return rows, total_pages if isinstance(total_pages, int) else None


def _trace_source(row: object) -> TraceSource | None:
    if not isinstance(row, Mapping):
        return None
    trace_id = _text(row.get("id"))
    if trace_id is None:
        return None
    if not _is_failing_trace(row):
        return None
    query = _trace_query(row.get("input"))
    if query is None:
        return None
    return TraceSource(trace_id=trace_id, query=query, category=_trace_category(row))


def _is_failing_trace(row: Mapping[str, Any]) -> bool:
    if row.get("level") == _TRACE_ERROR_LEVEL:
        return True
    scores = row.get("scores")
    if not isinstance(scores, list):
        return False
    return any(
        value is not None and value < _TRACE_LOW_SCORE
        for value in (_score_value(score) for score in scores)
    )


def _score_value(score: object) -> float | None:
    raw = score.get("value") if isinstance(score, Mapping) else score
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def _trace_query(value: object) -> str | None:
    """Reduce a trace input to the question text a replay item can send."""
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, Mapping):
        for key in ("query", "input"):
            inner = value.get(key)
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
        messages = value.get("messages")
        if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes)):
            return _question_from(messages)
        return None
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return _question_from(value)
    return None


def _question_from(messages: Sequence[Any]) -> str | None:
    """Prefer the last human turn; fall back to the last message that has text.

    A conversation trace ends with the agent's answer, so taking the last message
    would replay the answer as the question.
    """
    for message in reversed(messages):
        if _is_human_turn(message):
            text = _message_text(message)
            if text:
                return text
    for message in reversed(messages):
        text = _message_text(message)
        if text:
            return text
    return None


def _is_human_turn(message: object) -> bool:
    if not isinstance(message, Mapping):
        return False
    for key in ("type", "role"):
        turn = message.get(key)
        if isinstance(turn, str) and turn.lower() in _HUMAN_TURN_TYPES:
            return True
    return False


def _message_text(message: object) -> str | None:
    if isinstance(message, str):
        return message.strip() or None
    if not isinstance(message, Mapping):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content.strip() or None
    return None


def _trace_category(row: Mapping[str, Any]) -> str | None:
    metadata = row.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    return _text(metadata.get("category"))


def _tool_draft(tool: McpTool, user_hash: str) -> DatasetItemDraft:
    args, check_keys = _schema_arguments(tool.input_schema)
    return DatasetItemDraft(
        id=stable_item_id(_KIND_TOOL, tool.call_name, 0, user_hash),
        input={"query": _template_query("tool", tool.name, tool.description)},
        expected_output={
            "expected_tools": [tool.call_name],
            "expected_calls": [{"tool": tool.call_name, "args": args, "check_keys": check_keys}],
            "forbidden_tools": [],
            "expect_kb": False,
            "expect_skill": False,
            "judge_rule": (
                f"The agent calls {tool.call_name} and its arguments satisfy the tool schema."
            ),
        },
        metadata={
            "user_id": user_hash,
            "source_id": tool.call_name,
            "source_kind": _KIND_TOOL,
            "category": "single_tool",
            "source": "synthetic_from_tools",
            "tool_schema_hash": _schema_hash(tool.input_schema),
        },
    )


def _skill_draft(skill: SkillSource, user_hash: str) -> DatasetItemDraft:
    return DatasetItemDraft(
        id=stable_item_id(_KIND_SKILL, skill.skill_id, 0, user_hash),
        input={"query": _template_query("skill", skill.name, skill.description)},
        expected_output={
            "expect_skill": True,
            "expected_skills": [skill.name],
            "skill_rules": "",
            "expect_kb": False,
            "expected_tools": [],
        },
        metadata={
            "user_id": user_hash,
            "source_id": skill.skill_id,
            "source_kind": _KIND_SKILL,
            "category": "skill_single",
            "source": "synthetic_from_skill",
            "skill_commit": skill.git_commit,
        },
    )


def _knowledge_draft(document: KnowledgeSource, user_hash: str) -> DatasetItemDraft:
    return DatasetItemDraft(
        id=stable_item_id(_KIND_KNOWLEDGE, document.document_id, 0, user_hash),
        input={"query": _knowledge_query(document)},
        expected_output={
            "expect_kb": True,
            "expected_doc_ids": [document.document_id],
            "reference_answer": document.chunk_content,
            "key_points": [],
            "expect_refusal": False,
        },
        metadata={
            "user_id": user_hash,
            "source_id": document.document_id,
            "source_kind": _KIND_KNOWLEDGE,
            "category": "kb_single_hop",
            "source": "synthetic_from_docs",
            "document_id": document.document_id,
            "chunk_id": document.chunk_id,
            "knowledge_base_id": document.knowledge_base_id,
            "kb_version": f"{document.knowledge_base_id}:{document.chunk_count}",
        },
    )


def _trace_draft(trace: TraceSource, user_hash: str) -> DatasetItemDraft:
    return DatasetItemDraft(
        id=stable_item_id(_KIND_TRACE, trace.trace_id, 0, user_hash),
        input={"query": trace.query},
        # A replay has no expectations: evaluators treat it as inapplicable.
        expected_output={},
        metadata={
            "user_id": user_hash,
            "source_id": trace.trace_id,
            "source_kind": _KIND_TRACE,
            "source": _TRACE_SOURCE,
            "trace_id": trace.trace_id,
            "category": trace.category or _TRACE_REPLAY_CATEGORY,
        },
    )


def _template_query(kind: str, name: str, description: str) -> str:
    """The deterministic query template: the source name plus its own description."""
    detail = " ".join(description.split()).rstrip(".")
    if detail:
        return f"Use the {name} {kind}: {detail}."
    return f"Use the {name} {kind}."


def _knowledge_query(document: KnowledgeSource) -> str:
    title = document.filename.strip() or "the document"
    return f"According to {title}, what are the key points?"


def _schema_hash(schema: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _schema_arguments(
    schema: Mapping[str, Any], *, depth: int = 0
) -> tuple[dict[str, Any], list[str]]:
    """Fill the required properties of one schema level with deterministic values."""
    required = schema.get("required")
    if not isinstance(required, list):
        return {}, []
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        properties = {}
    args: dict[str, Any] = {}
    for name in required:
        if not isinstance(name, str) or not name:
            continue
        property_schema = properties.get(name)
        if not isinstance(property_schema, Mapping):
            continue
        args[name] = _placeholder(property_schema, depth=depth)
    return args, sorted(args)


def _placeholder(property_schema: Mapping[str, Any], *, depth: int) -> Any:
    """First enum value, else a type placeholder; recurse into a required object once."""
    enum = property_schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    type_name = _type_name(property_schema.get("type"))
    if type_name == "object" and depth == 0:
        nested, _ = _schema_arguments(property_schema, depth=depth + 1)
        return nested
    if type_name in ("integer", "number"):
        return 0
    if type_name == "boolean":
        return False
    if type_name == "array":
        return []
    if type_name == "object":
        return {}
    return ""


def _type_name(raw: object) -> str:
    if isinstance(raw, str):
        return raw.lower()
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, str) and entry and entry.lower() != "null":
                return entry.lower()
    return ""


def _text(value: object) -> str | None:
    """Return a non-empty string, or None for anything else."""
    return value if isinstance(value, str) and value else None


def _capped(items: Sequence[Any], limit: int | None) -> Sequence[Any]:
    return items if limit is None else items[:limit]


def _validate_limit(limit: int | None) -> None:
    if limit is None:
        return
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer or None")


def _data_path(session: AccountSession, suffix: str) -> str:
    """Data routes live under /api (not /api/v1); match the AGENT_API base spelling.

    Same rule as api._data_path: a base ending in /api already carries the prefix.
    """
    base = str(session.client.base_url).rstrip("/")
    prefix = "" if base.endswith("/api") else "/api"
    return f"{prefix}{suffix}"


def _get_object(
    session: AccountSession, suffix: str, *, params: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    path = _data_path(session, suffix)
    try:
        response = session.client.get(path, params=dict(params) if params else None)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"GET {path} failed ({type(exc).__name__})") from None
    response.raise_for_status()
    try:
        body = response.json()
    except ValueError:
        raise RuntimeError(f"GET {path} returned a non-JSON response") from None
    if not isinstance(body, dict):
        raise RuntimeError(f"GET {path} returned a non-object JSON response")
    return body

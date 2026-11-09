# myflow-evals

Standalone black-box evaluation workspace for the MyFlow agent. Everything here
talks to MyFlow over HTTP only: the package never imports `backend.app` or
`agentkit`. It authenticates as fixed test accounts, drives the Gateway's
thread/run API, reads the persisted run-event ledger, and builds Langfuse
datasets from the account's own tools, Skills, knowledge, and traces.

```bash
uv run --project evals pytest evals/tests -v
```

## Modules

| Module | Responsibility |
| --- | --- |
| `accounts.py` | Load fixed accounts (`AGENT_USERS` or a secret file), log in once per account, compute the Langfuse hashed user id. |
| `api.py` | Gateway thread/run/event client (`AgentApi`, `RunSnapshot`). |
| `evidence.py` | Pure normalization of a finished run into `RunEvidence` (answer, tool calls, subagents, retrieval, citations). |
| `mcp.py` | Direct MCP `tools/list` client (`McpClient`) plus `MCP_SERVER_HEADERS` secret loading. |
| `build_dataset.py` | Dataset sources, deterministic draft synthesis (`build_account_items`), optional trace fetch, and the `DatasetWriter` seam. |

Rule evaluators, the experiment runner, and the `myflow-evals` CLI are added by
later tasks; nothing here prints, and no module writes to a dataset by itself.

## Building drafts from the fixed account

`load_build_sources(session, limit=...)` reads, as that account:

- `GET /api/mcp-servers`, then a direct MCP `tools/list` per server for the real
  `inputSchema` (`GET /api/mcp-servers/{id}/tools` returns summaries without
  schemas, so it is not a schema source);
- `GET /api/skills`, keeping `scope == "private"` and dropping builtin/public
  Skills and any Skill without `skill_id`/`name`;
- `GET /api/knowledge/bases` -> `/documents` -> first chunk per document;
- the account's own failed or low-scored Langfuse traces (see below).

Only `streamable_http` MCP servers are supported. An `sse` server, a server
without a usable url, and a document without a readable chunk are skipped and
reported as warnings on `BuildSources.warnings` -- never silently dropped.

`build_account_items(session, sources, limit=...)` is pure: no I/O, no clock, no
randomness. It emits one draft per source with a deterministic id
(`stable_item_id(kind, source_id, ordinal, hashed_user_id)`), so rebuilding is
idempotent and re-running against the same account never duplicates items.

### Draft shapes

```json
// tool
{"id": "tool-<16 hex>", "input": {"query": "Use the <tool> tool: <description>."},
 "expected_output": {"expected_tools": ["mcp__<server>__<tool>"],
   "expected_calls": [{"tool": "mcp__<server>__<tool>", "args": {}, "check_keys": []}],
   "forbidden_tools": [], "expect_kb": false, "expect_skill": false, "judge_rule": "..."},
 "metadata": {"user_id": "<hashed>", "source_id": "mcp__<server>__<tool>", "source_kind": "tool",
   "category": "single_tool", "source": "synthetic_from_tools", "tool_schema_hash": "<16 hex>"}}

// private Skill
{"input": {"query": "Use the <skill> skill: <description>."},
 "expected_output": {"expect_skill": true, "expected_skills": ["<skill>"], "skill_rules": "",
   "expect_kb": false, "expected_tools": []},
 "metadata": {"user_id": "<hashed>", "source_id": "<skill_id>", "source_kind": "skill",
   "category": "skill_single", "source": "synthetic_from_skill", "skill_commit": "<sha or null>"}}

// knowledge document
{"input": {"query": "According to <filename>, what are the key points?"},
 "expected_output": {"expect_kb": true, "expected_doc_ids": ["<document_id>"],
   "reference_answer": "<first chunk>", "key_points": [], "expect_refusal": false},
 "metadata": {"user_id": "<hashed>", "source_id": "<document_id>", "source_kind": "knowledge",
   "category": "kb_single_hop", "source": "synthetic_from_docs", "document_id": "...",
   "chunk_id": "<document_id>:<chunk_index>", "knowledge_base_id": "...", "kb_version": "..."}}

// replayed online trace
{"input": {"query": "<the trace's question>"}, "expected_output": {},
 "metadata": {"user_id": "<hashed>", "source_id": "<trace id>", "source_kind": "trace",
   "source": "online_trace", "trace_id": "<trace id>", "category": "trace_replay"}}
```

Notes on the shapes:

- Tool `args` come from the real `inputSchema`: required properties only, first
  `enum` value when present, else a type placeholder, recursing into a required
  object one level. `check_keys` is the sorted list of the required properties
  that were filled. `forbidden_tools` stays empty: safety expectations are not
  guessed.
- Knowledge items carry `document_id`/`chunk_id` provenance in metadata but emit
  `expected_doc_ids` only -- chunk ids are a retriever implementation detail.
  `kb_version` is `<base_id>:<chunk_count>` because the knowledge APIs expose no
  version string; it still changes when a document is re-chunked.
- A trace draft has no expectations at all, so evaluators report it as
  inapplicable (`None`) instead of scoring it.
- `metadata.user_id` is always the Langfuse hashed user id
  (`HMAC-SHA256(user_id_salt, agent_user_id)[:32]`). The raw `/me.id`, usernames,
  and passwords never enter a draft, a log, or a report.

### Deterministic synthesis only

The build uses templates over real data; there is no LLM call or optional extra
in this path, so a build costs nothing and is reproducible byte for byte for the
same account data. Categories produced today are `single_tool`, `skill_single`,
`kb_single_hop`, and `trace_replay`.

The remaining categories from the design table -- `multi_tool`, `similar_tools`
(hard negatives), `kb_multi_hop`, `skill_multi`, and rewritten/human-reviewed
questions -- need LLM synthesis or human review and are future work. Until then
the built dataset has a narrower category mix than the design's target ratios,
and the template queries are placeholders, not realistic user phrasing.

### Limits, reports, and dry runs

`--limit` (the `limit` argument) caps drafts per source kind: tools, private
Skills, knowledge documents, and traces are capped separately. The returned
`BuildReport` records both how many sources were seen and how many drafts were
produced per kind and renders lines such as:

```
dataset: myflow-eval
limit: 2 per kind
mode: dry-run (writes nothing)
tool: 2 drafts from 37 sources
skill: 2 drafts from 11 sources
knowledge: 2 drafts from 120 sources
trace: 2 drafts from 4 sources
total: 8 drafts
warning: skipped MCP server 'legacy_db': sse transport is not supported
```

Writes go through `DatasetWriter.upsert_items(dataset_name, items)`.
`write_drafts(..., dry_run=True)` returns 0 and never calls the writer, so a dry
run cannot create or change a Langfuse dataset. The Langfuse-backed writer and
the `myflow-evals build` subcommand arrive in a later task.

## Secrets

| Variable | Purpose |
| --- | --- |
| `AGENT_USERS` | JSON map of label -> `{"username": ..., "password": ...}` for the fixed accounts. A secret file with the same shape may be passed instead. |
| `MCP_SERVER_HEADERS` | JSON map of server name (or id) -> that server's auth headers, for MCP servers that need auth. |
| `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | Langfuse HTTP API for trace replay. Without all three, no traces are read and no request is made. |
| `USER_ID_SALT` (or the CLI's salt argument) | Salt for the Langfuse hashed user id; it must match the Agent tracing configuration. |

`MCP_SERVER_HEADERS` example (values are placeholders, never commit real ones):

```json
{
  "project_db": {
    "X-Api-Key": "<token>",
    "url": "https://mcp.internal/rpc?tenant=<tenant>",
    "transport": "streamable_http"
  }
}
```

`url` and `transport` are reserved keys, not headers: they override a server
whose `GET /api/mcp-servers` URL lost its query string (`url_has_query`) or whose
listed transport is wrong.

The Gateway returns masked MCP header values, so the API can never be used to
call a server; the secret map is the only usable source, and header values, the
server URL query string, and Langfuse keys are excluded from dataclass reprs and
from every error message.

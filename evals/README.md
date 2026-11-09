# myflow-evals

Standalone black-box evaluation workspace for the MyFlow agent. It is one member
of the repository's uv workspace (`evals` in the root `pyproject.toml`), talks to
the MyFlow Gateway, MCP servers, and Langfuse over HTTP only, and never imports
`backend.app` or `agentkit`. It logs in as fixed test accounts, drives the
Gateway's thread/run API, reads the persisted run-event ledger, scores each
sample with rule evaluators, and writes scores through a Langfuse experiment. The
dataset builder reads each account's own MCP tools, private Skills, knowledge
documents, and failed/low-scored traces, and synthesizes deterministic drafts
from them.

## Install and run

```bash
uv sync --project evals
uv run --project evals myflow-evals --help
uv run --project evals pytest evals/tests -v
```

Run one CI-shaped experiment (all secrets must already be in the environment or a
secret file):

```bash
uv run --project evals myflow-evals run \
  --dataset agent-eval/regression/v1 \
  --mode ci \
  --run-name "ci-$(date +%Y%m%d-%H%M)"
```

The console script returns a process exit code instead of raising:

| Exit code | Meaning |
| --- | --- |
| `0` | The command did what it was asked: a run passed every gate, a build/ingest finished, or `--dry-run` printed its plan. |
| `1` | The experiment failed: a dataset item is missing or unexpected, a task raised, a run did not finish with `success`, an item produced no score, a citation scored 0.0, or a threshold was missed. |
| `2` | The command could not run as configured: a required secret is missing, the mode is unknown, an item names an account that is not logged in, the dataset is empty, or Langfuse/the Gateway is unusable. |

## Fixed accounts and identity

Every eval request runs as a fixed test account; there is no PAT or internal-token
shortcut. Configure at least one account, either in `AGENT_USERS` or in a JSON
file passed with `--accounts-file`:

```json
{
  "analyst_a": {"username": "alice", "password": "<password from the secret store>"}
}
```

For each account the package:

1. creates a fresh `httpx.Client` (its own cookie jar, so two accounts can never
   share a session) and calls the Gateway login route;
2. requires `needs_setup=false` -- a test account that still needs a password
   change is an error, not a silent default;
3. reads the `csrf_token` cookie, sends it as `X-CSRF-Token` on every later
   request, and calls `GET /auth/me` for the account's raw `users.id`;
4. computes the Langfuse hashed user id
   `HMAC-SHA256(USER_ID_SALT, agent_user_id)[:32]` (lowercase hex) and keys the
   logged-in session by that value.

`agent_user_id` is the raw `/me.id` after the Agent's storage-safe normalization
(unsafe characters become `-` and a 16-hex sha256 suffix preserves collision
resistance; a normal UUID passes through unchanged). The raw id exists only in
memory: every dataset draft carries the hashed id in `metadata.user_id`, the
runner selects an account by exact hashed-id match, and neither the raw id nor a
password is ever written to a Dataset item, a Langfuse trace, a log line, or a
CLI summary. Two accounts that hash to the same id are refused rather than
overwritten.

## Secrets

| Variable | Used for |
| --- | --- |
| `AGENT_API` | Gateway base URL. The documented form includes `/api` (for example `http://127.0.0.1:8001/api`); the client accepts a base with or without that suffix and adds `/api` or `/api/v1` accordingly. |
| `AGENT_USERS` | JSON map of account label -> `{"username": ..., "password": ...}`. One credential entry per fixed account. |
| `USER_ID_SALT` | Salt for the hashed user id. It must match the Agent tracing configuration (`user_id_salt`); changing it changes every hashed id, so existing datasets must be rebuilt/re-labelled. |
| `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | Langfuse HTTP API: dataset reads/writes, experiments, and failed/low-scored trace reads. |
| `MCP_SERVER_HEADERS` | JSON map of MCP server name (or id) -> that server's auth headers, plus optional `url`/`transport` overrides. |

Secret material belongs in a secret store (CI secrets, a vault, or an untracked
local file). It must never appear in a Dataset item, a trace, a log line, a
README, a test fixture, or a commit. The package excludes passwords from
dataclass reprs, reports MCP failures by server id and exception type only, and
never echoes a header value, a token in a server URL query string, or a Langfuse
key.

Secret files are optional conveniences:

- `--env-file PATH` reads `KEY=VALUE` lines (comments and blank lines ignored).
  Real environment values win over the file, so a CI secret is never overridden
  by a file in the checkout.
- `--accounts-file PATH` replaces `AGENT_USERS` as the credential source.
- `--mcp-headers-file PATH` fills `MCP_SERVER_HEADERS` only when the environment
  variable is unset or empty.

`MCP_SERVER_HEADERS` example (placeholders only, never commit real values):

```json
{
  "project_db": {
    "X-Api-Key": "<token>",
    "url": "https://mcp.internal/rpc?tenant=<tenant>",
    "transport": "streamable_http"
  }
}
```

`url` and `transport` are reserved keys, not headers: they override a server whose
`GET /api/mcp-servers` URL lost its query string or whose listed transport is
wrong. The Gateway returns masked MCP header values, so the API can never be used
to call a server; this secret map is the only usable source.

## Commands

All three subcommands share these flags:

| Flag | Meaning |
| --- | --- |
| `--dataset NAME` | Required. The Langfuse dataset name. |
| `--limit N` | Positive integer cap. `build` caps drafts per source kind; `run` caps the selected items after mode selection; `ingest` caps the traces printed. |
| `--dry-run` | Do everything except writing. See each command for the exact distinction. |
| `--accounts-file PATH` | Fixed-account secret file instead of `AGENT_USERS`. |
| `--mcp-headers-file PATH` | MCP header secret file instead of `MCP_SERVER_HEADERS`. |
| `--env-file PATH` | `KEY=VALUE` settings file; real environment values win. |

`build` and `ingest` deliberately have no `--mode`/`--run-name`: those flags only
describe an experiment, and a dead flag would suggest behavior that does not
exist.

### `build`

```bash
uv run --project evals myflow-evals build \
  --dataset agent-eval/explore \
  --limit 100 \
  --dry-run
```

`build` logs in every fixed account and reads, as that account: `GET /api/mcp-servers`
plus a direct MCP `tools/list` per server for the real `inputSchema`; `GET /api/skills`
(private Skills only); `GET /api/knowledge/bases` -> `/documents` -> first chunk per
document; and, only with `--include-traces`, the account's own failed/low-scored
Langfuse traces. It then synthesizes one deterministic draft per source and,
unless `--dry-run` is given, upserts every draft into the dataset through Langfuse.

A trace replay declares no expectations (`expected_output` is `{}`), so no metric
can score it and any `run` over it fails on an `unscored` item. `build` therefore
excludes trace drafts by default and pages no traces at all; pass
`--include-traces` to build an exploration dataset. A regression dataset
(`agent-eval/regression/v1`) must be built without the flag, so it contains only
scorable samples.

`--dry-run` still logs in and still reads the accounts' data -- sources are
account-scoped, so there is no way to report real counts without them -- but it
writes nothing to Langfuse and needs no Langfuse credentials. It is not a
"no network" mode.

Item ids are namespaced per dataset: Langfuse item ids are unique across a
project, so `LangfuseDatasetWriter` writes `<8-hex digest of dataset name>-<draft
id>`. The same account can build `agent-eval/regression/v1` and
`agent-eval/explore` from the same sources without collision, and rebuilding one
dataset name reproduces its ids (a rebuild never deletes stale items -- a new
frozen version needs a new dataset name).

### `run`

```bash
uv run --project evals myflow-evals run \
  --dataset agent-eval/regression/v1 \
  --mode ci \
  --run-name "ci-$(date +%Y%m%d-%H%M)" \
  --max-concurrency 2
```

| Flag | Meaning |
| --- | --- |
| `--mode {ci,full}` | Default `ci`. `ci` executes the fixed stratified 30-item subset; `full` executes every item. |
| `--run-name NAME` | Per-run identity. Defaults to `<mode>-<UTC timestamp>`. The experiment label itself is always `agent-eval:<dataset>`. |
| `--max-concurrency N` | Default `2`. Parallel agent runs; keep it small for the Gateway and the subagent runtime. The item task runs off the SDK event loop (`asyncio.to_thread`), so this really overlaps runs instead of serialising them. |
| `--limit N` | Cap the selected items after mode selection. |

A real `run` requires the three Langfuse variables plus `AGENT_API` and
`USER_ID_SALT`. It reads the frozen dataset, selects items for the mode, logs in
every fixed account, and executes exactly the selected items -- the runner passes
`data=list(selected)` to Langfuse, so a large dataset is never silently run in
full. Each item gets its own thread and run (`on_disconnect=continue`), then the
runner polls the run's terminal status, reads the run-event ledger and the
thread checkpoint, normalizes them into evidence, scores the item with every
applicable rule evaluator, and uploads only a compact output (answer, status,
trace id, opaque evidence id) to Langfuse. Retrieved chunks and citation payloads
stay in the runner process.

`--dry-run` prints the dataset, mode, run name, and how many items would be
selected; it starts no thread, writes no score, and logs in nowhere -- but it
still reads the dataset through Langfuse, so the Langfuse variables are required
while `AGENT_API` and `USER_ID_SALT` are not (nothing logs in).

What can fail a run:

- any planned dataset item missing from the experiment result, or an unexpected
  item in it;
- a task exception, or a terminal run status other than `success` (`error`,
  `timeout`, `interrupted`);
- a completed item that produced no evaluation at all (`unscored`);
- any `citation_valid` score of 0.0, even if the average passes;
- any `forbidden_violation` score of 1.0, even if the average passes;
- a sample that needed a retrieval subagent and never reached it, loaded a Skill it
  never retrieved, or answered a KB question without KB retrieval evidence
  (`invalid_routing`), even when the routing average stays above its gate;
- a run in which no item produced a thresholded score at all
  (`no scorable samples`);
- a thresholded metric whose average misses its gate.

### `ingest`

```bash
uv run --project evals myflow-evals ingest \
  --dataset agent-eval/explore \
  --limit 50
```

`ingest` logs in the fixed accounts, reads each account's failed or low-scored
Langfuse traces by hashed user id, and prints one line per candidate:
`<category>: <trace id> -> <question preview>`. It writes nothing: no dataset is
created and no item is uploaded. The `--dry-run` flag is accepted for symmetry
with the other commands; it changes nothing because `ingest` never writes.

## Eval-environment preconditions

The Agent under test must be configured so evidence means what the metrics claim:

- `auth.enabled=true` -- otherwise every request runs as the fixed `default` user
  and account-scoped data filtering is meaningless.
- Agent tracing uses `user_id_mode=hashed`, with the same salt as `USER_ID_SALT`.
- `subagent_enabled=true`, and both `skill-finder` and `knowledge-researcher` are
  available. With subagents disabled or a retrieval subagent unavailable, the
  lead agent can fall back to a direct `knowledge_search`; that is a
  configuration regression for evaluation and must not be scored as a pass.
- `skills.usage.prompt_top_k=0` -- private Skills must not enter the static
  `<available_skills>` prompt index.
- Eval samples do not use slash activation. Every `expect_skill`/`expect_kb`
  sample must reach its subagent through a `task` call; a static `load_skill` or
  a direct `knowledge_search` without delegating makes the sample invalid, and
  the routing metrics score it 0.0 rather than `N/A`.
- `run_events.backend=db` or `jsonl`, never `memory`: the event ledger must survive
  the run, because evidence is read after the run finishes.

## CI and nightly

`.github/workflows/agent-eval.yml` runs on pull requests and nightly (`0 18 * * *`
UTC), with a 45-minute job timeout and the Gateway/Langfuse secrets injected from
GitHub secrets (a run never reads MCP headers). The dataset is frozen input: the
workflow never runs `build`.

| Trigger | Command | What it covers |
| --- | --- | --- |
| Pull request | `--mode ci --dataset agent-eval/regression/v1 --run-name ci-${GITHUB_SHA}` | The fixed stratified 30-item subset (round-robin over id-sorted `metadata.category` groups; a smaller dataset contributes every item). Rule metrics are hard gates. LLM metrics never hard-fail in `ci` mode. |
| Nightly schedule | `--mode full --dataset agent-eval/regression/v1 --run-name nightly-${GITHUB_SHA}` | Every dataset item. Rule metrics are hard gates; LLM metrics may become hard gates once they have at least 20 scored samples. |

A gated metric with zero scored samples is reported `insufficient` and does not
fail the run; it also does not count as a pass. Do not read "PASS" as "every
metric was measured" -- check the `samples=` column.

Today every entry in `THRESHOLDS` is a rule metric (`kind: rule`). The runner
already implements the LLM-metric policy -- below 20 scored samples an LLM metric
is `insufficient`, and with at least 20 it can gate hard in `full` mode while it
stays `soft` outside it -- but no LLM/DeepEval metric is wired into `THRESHOLDS`
yet.

## Results: gates, scores, and incomplete experiments

A successful run prints one summary line per thresholded metric, then the
verdict:

```text
dataset: agent-eval/regression/v1
run: ci-20261109 (mode ci)
items: 30 of 30 returned
scorable items: 30
retrieval_recall@5: 0.980 min 0.95 samples=30 PASS
tool_hit: 0.933 min 0.90 samples=30 PASS
args_accuracy: n/a min 0.85 samples=0 insufficient scored samples (LLM metrics need 20)
...
result: PASS
experiment: https://langfuse.example/project/p/experiments/<id>
```

`PASS`/`FAIL` compare the average of the metric's non-`None` samples with the
threshold (`min`: average must be at least the threshold; `max`: at most).
`None` means "this sample cannot be scored on this metric" and is not counted as
zero. `insufficient` means no sample produced a numeric score for that metric
(or an LLM metric has fewer than 20); the renderer uses one LLM-oriented sentence
for both cases, so read `samples=0` as "nothing scored this metric".

Metric catalogue (`THRESHOLDS` as merged):

| Score | Layer | Evidence it reads | Gate |
| --- | --- | --- | --- |
| `retrieval_recall@5` | T1 tool retrieval | `retrieval.tool` -> `tools[].call_name`, first 5 | `>= 0.95` |
| `tool_hit` | T2 tool selection | Business tool calls, with `tool_execute` unwrapped and infrastructure tools excluded | `>= 0.90` |
| `args_accuracy` | T2 arguments | `expected_calls[].check_keys` against the actual call arguments | `>= 0.85` |
| `extra_calls_ratio` | T3 trajectory | Business tool calls / expected calls | `<= 1.5` |
| `skill_routing` | K0 routing | A `task` call with `subagent_type="skill-finder"` vs `expect_skill` | `>= 0.95` |
| `skill_recall@5` | K1 Skill retrieval | `retrieval.skill` -> `skills[].name`, first 5 | `>= 0.95` |
| `skill_hit` | K2 Skill selection | `load_skill` names: every expected Skill loaded, and only retrieved Skills loaded | `>= 0.90` |
| `kb_routing` | R0 routing | A `task` call with `subagent_type="knowledge-researcher"` vs `expect_kb` | `>= 0.95` |
| `kb_recall@5` | R1 KB retrieval | `retrieval.kb` -> `items[].document_id`, first 5 | `>= 0.85` |
| `citation_valid` | R3 citations | `additional_kwargs.knowledge_citations` pairs vs chunks this run retrieved | `>= 0.95`; any 0.0 fails the whole run |
| `forbidden_violation` | S1 safety | Business tool calls vs `forbidden_tools`; 1.0 means a violation | `<= 0.0`; any 1.0 fails the whole run |

`citation_valid` returns `None` when any `retrieval.kb` row of the run was
truncated: the budget hid chunks, so a citation that looks forged may simply be
out of view. Recorded without a numeric gate: `retrieval_mrr`, `skill_mrr`,
`kb_mrr`, `skill_extra` (lower is better), and `no_tool_ok` (only items whose
`metadata.category` is `no_tool` are scored).

An item that finishes with no evaluations at all is a `failed item` with status
`unscored`, not an absent item: the design counts an unscored sample as a failed
experiment. This matters for trace-replay drafts, whose `expected_output` is `{}`:
they are intentionally inapplicable to every metric, so a dataset containing
them cannot ride in a regression run. `build` keeps them out unless
`--include-traces` is passed; put trace replays in the explore set, or give them
real expectations before running them as a regression.

An item that declares `expect_skill`/`expect_kb` but answers without that
subagent -- or that loads a Skill it never retrieved, or answers a KB question
with no KB retrieval evidence -- is a `failed item` with status `invalid_routing`.
Routing has its own score, but one direct fallback among 30 samples still
averages above the 0.95 gate, so the runner fails the sample instead.

`scorable items: N` counts the items that produced at least one thresholded
metric score. With `N = 0` the run fails with
`no scorable samples: no item produced a thresholded metric score`, even when
every item completed: an empty gate list is not a clean result.

An incomplete experiment is reported before any gate summary:
```text
error: experiment did not return every dataset item it planned; missing: kb-1a2b3c4d5e6f7a8b; failed: kb-1a2b3c4d5e6f7a8b=exception (RuntimeError: ...)
```
The CLI prints that line to stderr and exits `1`. Investigate the item ids named
there: a `missing` item was planned but never returned, an `unexpected` item was
not planned, and a `failed` item names its status (`exception`, `unscored`, or a
non-success run status). Do not rerun only the surviving items to get a green
result -- the next full run must show every planned item returned.

### Experiment metadata

Every experiment carries these string values on the run, so two experiments are
comparable without guessing what changed:

| Key | Value |
| --- | --- |
| `mode` | `ci` or `full` |
| `max_concurrency` | The concurrency the run used |
| `dataset` | The dataset name |
| `package_version` | The installed `myflow-evals` version (`0.1.0` fallback) |
| `git_sha` | `GIT_SHA`/`GITHUB_SHA` when present, else `unknown` |
| `salt_fingerprint` | First 8 hex of `sha256(USER_ID_SALT)`; never the salt |

The experiment label itself is stable (`agent-eval:<dataset>`), so `ci-<sha>` and
`nightly-<date>` runs land in the same experiment and stay comparable.

## Package layout

| Module | Responsibility |
| --- | --- |
| `accounts.py` | Load fixed accounts (`AGENT_USERS` or a secret file), log in once per account, compute the Langfuse hashed user id. |
| `api.py` | Gateway thread/run/event client (`AgentApi`, `RunSnapshot`). |
| `evidence.py` | Pure normalization of a finished run into `RunEvidence` (answer, tool calls, subagents, retrieval, citations). |
| `evaluators.py` | Pure per-sample rule metrics and the `RULE_EVALUATORS`/`THRESHOLDS` registry. |
| `mcp.py` | Direct MCP `tools/list` client (`McpClient`) plus `MCP_SERVER_HEADERS` secret loading. |
| `build_dataset.py` | Dataset sources, deterministic draft synthesis (`build_account_items`), optional trace fetch, and the `DatasetWriter` seam. |
| `runner.py` | Experiment execution, completeness gate, compact evidence handoff, metric gates. |
| `cli.py` | The `myflow-evals build` / `run` / `ingest` command line and its exit-code contract. |

## Building drafts from the fixed account

`load_build_sources(session, limit=...)` reads, as that account:

- `GET /api/mcp-servers`, then a direct MCP `tools/list` per server for the real
  `inputSchema` (`GET /api/mcp-servers/{id}/tools` returns summaries without
  schemas, so it is not a schema source);
- `GET /api/skills`, keeping `scope == "private"` and dropping builtin/public
  Skills and any Skill without `skill_id`/`name`;
- `GET /api/knowledge/bases` -> `/documents` -> first chunk per document;
- the account's own failed or low-scored Langfuse traces, only when the caller
  asks for them (`include_traces=True`, i.e. `--include-traces`); see below.

Only `streamable_http` MCP servers are supported. A disabled server
(`enabled: false` -- the agent cannot route to its tools), an `sse` server, a
server without a usable url, a server whose `tools/list` fails, a document
without a readable chunk, and an unavailable Langfuse are all skipped and
reported as warnings on `BuildSources.warnings` -- never silently dropped, and a
broken source costs exactly its own items instead of failing the build. A
Gateway failure that makes the account data unusable still raises.

`build_account_items(session, sources, limit=...)` is pure: no I/O, no clock, no
randomness. It emits one draft per source with a deterministic id
(`stable_item_id(kind, source_id, ordinal, hashed_user_id)`), so rebuilding is
idempotent and re-running against the same account never duplicates items. A tool
whose call name the Agent could not call back -- a dotted upstream name, or one
beyond the provider length bound -- is skipped with a warning and produces no
draft, because a sample built from it could never pass.

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
  inapplicable (`None`) instead of scoring it -- which also means it is
  `unscored` for the completeness gate, so keep replays out of regression runs.
- `metadata.user_id` is always the Langfuse hashed user id
  (`HMAC-SHA256(USER_ID_SALT, agent_user_id)[:32]`). The raw `/me.id`, usernames,
  and passwords never enter a draft, a log, or a report.

### Deterministic synthesis only

The build uses templates over real data; there is no LLM call or optional extra
in this path, so a build costs nothing and is reproducible byte for byte for the
same account data. Categories produced today are `single_tool`, `skill_single`,
`kb_single_hop`, and -- only with `--include-traces` -- `trace_replay`.

The remaining categories from the design table -- `multi_tool`, `similar_tools`
(hard negatives), `missing_params`, `similar_skills`, `skill_with_tools`,
`kb_multi_hop`, `kb_unanswerable`, and rewritten/human-reviewed questions --
need LLM synthesis or human review and are future work. Until then the built dataset has a narrower category mix than the
design's target ratios, and the template queries are placeholders, not realistic
user phrasing.

### Limits, reports, and dry runs

`--limit` (the `limit` argument) caps drafts per source kind: tools, private
Skills, knowledge documents, and traces are capped separately. It bounds work,
not truth: only the first `limit` documents get their chunk fetched, but every
kind is still counted in full (traces are paged to completion under a bounded
page guard, so even the trace count is real), so the "M sources" in a report is
the number of sources that account actually has, never the capped sample. Without
`--include-traces` the trace kind is not read at all and the report says
`trace: 0 drafts from 0 sources`. The returned `BuildReport` records both counts
and renders lines such as:

```text
dataset: myflow-eval
limit: 2 per kind
mode: dry-run (writes nothing)
tool: 2 drafts from 37 sources
skill: 2 drafts from 11 sources
knowledge: 2 drafts from 120 sources
trace: 2 drafts from 4 sources
total: 8 drafts
warning: skipped MCP server 'legacy_db': sse transport is not supported (only streamable_http is implemented)
```

Writes go through `DatasetWriter.upsert_items(dataset_name, items)`, implemented
by `LangfuseDatasetWriter` in `cli.py`. `write_drafts(..., dry_run=True)` returns 0
and never calls the writer, so a dry run cannot create or change a Langfuse
dataset. `myflow-evals build` is the only command that writes dataset items;
`run` treats the dataset as frozen input, and `ingest` only prints.

## Known limits

- Deterministic synthesis covers `single_tool`, `skill_single`, `kb_single_hop`,
  and `trace_replay` only. `multi_tool`, `similar_tools`, `missing_params`,
  `similar_skills`, `skill_with_tools`, `kb_multi_hop`, `kb_unanswerable`, and
  rewritten questions are not generated.
- K3 `skill_adherence`, R4 `refusal_ok`, Faithfulness, `answer_correctness`,
  `answer_quality`, AnswerRelevancy, ContextualPrecision/Recall/Relevancy, and
  every other DeepEval metric are not implemented. Every current `THRESHOLDS`
  entry is a rule metric.
- LLM-based dataset synthesis and the optional DeepEval extra are not present;
  there is no manual Golden set.
- `ingest` prints candidate failed/low-scored traces but does not write an
  explore dataset; promoting them into a dataset is still manual.
- Only `streamable_http` MCP servers contribute tools. `sse` servers, disabled
  servers, servers without a URL, and servers whose `tools/list` fails are skipped
  with a warning.
- Evidence carries the run trace id only because the runner hands
  `RunSnapshot.trace_id` to `collect_evidence`; a different caller must pass it
  explicitly or the evidence's `trace_id` is `None`.
- No user-id, permission, cross-user isolation, or long-term-memory metric is
  scored; `metadata.user_id` is only an account-selection/provenance key.
- Builtin Skills and slash activation are excluded from samples. Only
  `scope=private` Skills are sampled.
- The retrieval metrics compare names/ids over the first five results; adding or
  removing source documents invalidates `expected_doc_ids`. Re-chunking alone does
  not, because only document ids are compared.
- `retrieval.*` events carry only what the model saw: the query, the final
  cards/chunks, channels, warnings, and the `truncated` flag. They do not carry
  `task_query`, `domain_keywords`, or a tool `description`, so a metric that needs
  those must read them elsewhere; the ledger contract stays as merged.
- A dataset version is immutable once frozen: rebuilding the same name upserts
  items but never deletes the ones whose sources disappeared. Freeze a new
  version under a new dataset name (for example `agent-eval/regression/v2`).
- `trace_replay` items are unscorable by construction, so `build` excludes them
  unless `--include-traces` is passed and a dataset that contains them cannot
  pass a run.

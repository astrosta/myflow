# Agent Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the backend evidence API and an independent `evals` package that constructs a fixed-account Langfuse dataset, runs a black-box Agent evaluation, and fails the run when any sample is missing or any gate is not met.

**Architecture:** The Gateway gains a read-only run-event endpoint and structured `retrieval.*` events. A separate `evals` uv-workspace package logs in fixed test accounts, maps their hashed Langfuse user IDs, builds Dataset items from the account's real tools, Skills, and knowledge, runs the Agent over HTTP, normalizes run events into evidence, and writes scores through Langfuse `run_experiment`. Evaluation code never imports the Gateway application or reads its database.

**Tech Stack:** Python 3.12, FastAPI, LangGraph, SQLAlchemy async stores, Langfuse SDK 5.1.x, DeepEval (optional eval extra), httpx, pytest.

**Spec:** `docs/plans/Agent评测方案.md`

## Global Constraints

- The current uncommitted citations/skills refactor must be committed first. Assume `additional_kwargs.knowledge_citations` and the `load_skill(name, id)` tool exist.
- Work in the same repository, but in the whole-uv-workspace member `evals`; do not add eval dependencies to `backend/pyproject.toml`.
- Fixed test accounts only. Credentials come from `AGENT_USERS`/secret storage. Passwords and raw user IDs never enter a Dataset item, Langfuse trace, log line, or committed file.
- `metadata.user_id` is the Langfuse hashed user ID: `HMAC-SHA256(user_id_salt, agent_user_id)[:32]`. Raw user ID is used only while building/filtering that account's data.
- Every `expect_skill` and `expect_kb` sample must route to `skill-finder` / `knowledge-researcher`. Eval environment sets `subagent_enabled=true`, makes both subagents available, and sets `skills.usage.prompt_top_k=0`. Slash activation is excluded.
- Builtin Skills are excluded from dataset generation. Only `scope=private` Skills are sampled.
- R1 uses `expected_doc_ids` only. Do not introduce `expected_chunk_ids`.
- No user_id/permission/isolation metric is scored. User identity is only a data-filter/provenance key.
- Dataset completeness is a hard gate: any missing item, task exception, or unclassified retrieval makes the experiment fail.
- `run_events.backend` must be `db` or `jsonl` for evaluation; never `memory` for CI/nightly.
- Rule metrics are hard gates. LLM metrics use deterministic stratified sampling with a minimum sample count in CI; only nightly full runs use LLM metrics as hard gates.
- Every task follows TDD: write the failing test, run it, implement the minimum, rerun, commit.

## Review Focus

1. **Auth and identity:** a fixed account's cookie/CSRF lifecycle and the exact `make_safe_user_id` + HMAC hash must match the Agent trace's `userId`; a normal UUID account must not silently differ because of normalization.
2. **Dataset completeness:** a task exception inside Langfuse `run_experiment` must not turn a failed 30-item run into a passing 27-item run.
3. **Route vs retrieval separation:** K0/R0 must use `task.subagent_type` or `subagent.start`; K1/R1 must use `retrieval.*`. A missing internal search must not be scored as a failed route.
4. **Evidence fidelity:** `retrieval.kb` must carry the final model-visible chunks (not pre-rerank candidates), citations must be validated against those chunks, and truncated retrieval must make R3 unavailable rather than silently scored.
5. **Secret and payload hygiene:** no password/raw user ID in Dataset/trace/log; no full retrieval payload returned to Langfuse's experiment trace when it is only needed by local evaluators.

---

### Task 1: Add the run-event read API and bounded event queries

**Files:**
- Create: `backend/tests/test_run_events_route.py`
- Create: `backend/tests/test_event_store_pagination.py`
- Modify: `backend/app/gateway/models/response.py`
- Modify: `backend/app/gateway/routers/thread_runs.py`
- Modify: `backend/packages/harness/agentkit/persistence/events/base.py`
- Modify: `backend/packages/harness/agentkit/persistence/events/memory.py`
- Modify: `backend/packages/harness/agentkit/persistence/events/jsonl.py`
- Modify: `backend/packages/harness/agentkit/persistence/events/db.py`
- Modify: `backend/tests/test_m3_routes.py` (route inventory)

**Interfaces:**
- Consumes: existing `RunEventStore` rows and `Page[T]`.
- Produces:
  - `RunEventStore.list_events(thread_id: str, run_id: str, event_types: Sequence[str] | None = None, after_seq: int | None = None, task_id: str | None = None, limit: int | None = None) -> list[dict[str, Any]]`
  - `class RunEventResponse(BaseModel): seq: int; run_id: str; event_type: str; category: str; content: Any; metadata: dict[str, Any]; created_at: str | None`
  - `GET /api/threads/{thread_id}/runs/{run_id}/events?after_seq=&limit=100&event_types=&task_id=` returning `Page[RunEventResponse]` with cursor `str(seq)`.

- [ ] **Step 1: Write the failing route test**

Create `backend/tests/test_run_events_route.py` with a fake `RunServices`, one thread, one run, and three events. Assert:
- `runs:read` is required;
- another owner gets 404;
- `event_types=subagent.start` filters;
- `limit=1` returns `has_more` and `next_cursor`;
- response entries expose `event_type`, `content`, `metadata` but never `user_id`.

Run: `cd backend && uv run pytest tests/test_run_events_route.py -v`  
Expected: FAIL because the route does not exist.

- [ ] **Step 2: Write the failing pagination test**

Create `backend/tests/test_event_store_pagination.py` that inserts five events into a `MemoryRunEventStore` and asserts `list_events(..., limit=2)` returns the first two in ascending `seq` and that `after_seq=2, limit=2` returns the next two. Add the same contract test for `JsonlRunEventStore` with a temporary directory.

Run: `cd backend && uv run pytest tests/test_event_store_pagination.py -v`  
Expected: FAIL with unexpected `limit` argument.

- [ ] **Step 3: Add `limit` to the interface and all three stores**

Implement `limit: int | None = None` in `base.py`, `memory.py`, `jsonl.py`, and `db.py`. In DB use `.limit(limit)` when provided; memory/jsonl slice after existing filters. Do not change default behavior for existing callers.

- [ ] **Step 4: Add the response model and route**

Add `RunEventResponse` to `response.py`. In `thread_runs.py` add the route with `require_scope("runs:read")`, `require_visible_thread`, and the same run ownership check as `get_run`. Parse `event_types` as a comma-separated list, cap `limit` at 200, fetch `limit + 1`, and return `Page` with `next_cursor=str(last.seq)`.

Add the route to `test_m3_routes.py`'s inventory. Do not expose `user_id` from the event row.

- [ ] **Step 5: Run tests**

Run: `cd backend && uv run pytest tests/test_run_events_route.py tests/test_event_store_pagination.py tests/test_m3_routes.py -v`  
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/gateway/models/response.py backend/app/gateway/routers/thread_runs.py \
  backend/packages/harness/agentkit/persistence/events \
  backend/tests/test_run_events_route.py backend/tests/test_event_store_pagination.py \
  backend/tests/test_m3_routes.py
git commit -m "feat: expose bounded run event history"
```

### Task 2: Define structured retrieval events and their normalizer

**Files:**
- Create: `backend/packages/harness/agentkit/runtime/events/retrieval.py`
- Create: `backend/tests/test_retrieval_events.py`
- Modify: `backend/packages/harness/agentkit/runtime/events/catalog.py`
- Modify: `contracts/run_event_stream_contract.json`

**Interfaces:**
- Consumes: tool result content from `tool_search`, `search_skills`, `knowledge_search`.
- Produces:
  - `RUN_EVENT_CATEGORIES` includes `"retrieval"`.
  - `RETRIEVAL_TOOL_EVENT = RunEventDefinition("retrieval.tool", "retrieval")`
  - `RETRIEVAL_SKILL_EVENT = RunEventDefinition("retrieval.skill", "retrieval")`
  - `RETRIEVAL_KB_EVENT = RunEventDefinition("retrieval.kb", "retrieval")`
  - `@dataclass(frozen=True) class RetrievalEvent: event_type: str; content: dict[str, Any]`
  - `retrieval_event_for_tool(*, tool_name: str, content: Any) -> RetrievalEvent | None`

- [ ] **Step 1: Write fixture-based failing tests**

Cover:
- `tool_search` payload `{"tools": [...]}` -> `retrieval.tool` with `tools[].call_name`;
- `search_skills` payload `{"skills": [...]}` -> `retrieval.skill` with `skills[].skill_id/name/score`;
- `knowledge_search` payload `{"query": ..., "sources": [...], "results": [...]}` -> `retrieval.kb` with `document_id/chunk_id/citation_id/content`;
- malformed JSON or unknown tool -> `None`;
- a result with `truncated: true` preserves the flag.

Run: `cd backend && uv run pytest tests/test_retrieval_events.py -v`  
Expected: FAIL because the module/definitions do not exist.

- [ ] **Step 2: Add catalog definitions and contract entries**

Add the three definitions and the `retrieval` category. Update `contracts/run_event_stream_contract.json` to list the same pairs. Keep `list_messages` restricted to `category=message`.

- [ ] **Step 3: Implement the normalizer**

Parse string or content-block content with the same text extraction used by `message_content_to_text`. Validate required fields; never throw for malformed tool output. Return only normalized, JSON-safe values. Do not include secrets, headers, or full tool schemas in the payload.

- [ ] **Step 4: Run tests**

Run: `cd backend && uv run pytest tests/test_retrieval_events.py tests/test_event_catalog_contract.py -v`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/packages/harness/agentkit/runtime/events/retrieval.py \
  backend/packages/harness/agentkit/runtime/events/catalog.py \
  contracts/run_event_stream_contract.json backend/tests/test_retrieval_events.py
git commit -m "feat: add structured retrieval event types"
```

### Task 3: Emit retrieval events for lead-side tool_search results

**Files:**
- Create: `backend/tests/test_run_journal_retrieval.py`
- Modify: `backend/packages/harness/agentkit/runtime/events/journal.py`

**Interfaces:**
- Consumes: `retrieval_event_for_tool` from Task 2 and the existing `RunJournal._put`.
- Produces: `retrieval.tool` rows for a parent `tool_search` ToolMessage, emitted once per tool call.

- [ ] **Step 1: Write the failing journal test**

Construct a `MemoryRunEventStore` and `RunJournal`. Send a `ToolMessage(content=<tool_search JSON>, name="tool_search", tool_call_id="c1")` through `on_tool_end`. Assert one `retrieval.tool` row with the normalized call names. Send the same tool call twice and assert still one retrieval row.

Run: `cd backend && uv run pytest tests/test_run_journal_retrieval.py -v`  
Expected: FAIL.

- [ ] **Step 2: Emit from on_tool_end and reconcile path**

After the existing `llm.tool.result` write, call `retrieval_event_for_tool`. If a retrieval event is returned, write it with `_put` and metadata `{"tool_name": "tool_search"}`. Add a dedup set keyed by `tool_call_id` (or a stable content digest when no ID) so callback replay cannot duplicate it. Apply the same helper in `_reconcile_messages` for a ToolMessage that arrived without `on_tool_end`.

- [ ] **Step 3: Run tests**

Run: `cd backend && uv run pytest tests/test_run_journal_retrieval.py tests/test_run_journal.py -v`  
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add backend/packages/harness/agentkit/runtime/events/journal.py \
  backend/tests/test_run_journal_retrieval.py
git commit -m "feat: emit tool retrieval evidence from the run journal"
```

### Task 4: Forward subagent retrieval evidence without the 8192-character step cap

**Files:**
- Modify: `backend/packages/harness/agentkit/subagents/results.py`
- Modify: `backend/packages/harness/agentkit/subagents/executor.py`
- Modify: `backend/packages/harness/agentkit/subagents/step_events.py`
- Modify: `backend/packages/harness/agentkit/tools/builtin/task_tool.py`
- Modify: `backend/packages/harness/agentkit/runtime/runs/worker.py`
- Create: `backend/tests/test_subagent_retrieval_events.py`

**Interfaces:**
- Consumes: `retrieval_event_for_tool` and full ToolMessage content from `search_skills`/`knowledge_search` in the subagent state.
- Produces:
  - `SubagentResult.append_retrievals(events: Sequence[dict]) -> None`
  - `SubagentResult.pending_retrievals(reported: int) -> list[dict]`
  - a root custom frame `{"type": "task_retrieval", "task_id": "...", "retrieval": {...}}`
  - rows `retrieval.skill` / `retrieval.kb` with `metadata={"task_id": ...}`.

- [ ] **Step 1: Write the failing pure and buffer tests**

Test that the new step scanner recognizes a full `knowledge_search` ToolMessage even when its serialized content exceeds 8192 characters, and that `task_retrieval` maps to `retrieval.kb` in `_SubagentEventBuffer. Test the `search_skills` case as well.

Run: `cd backend && uv run pytest tests/test_subagent_retrieval_events.py -v`  
Expected: FAIL.

- [ ] **Step 2: Add a retrieval cursor to step capture**

In `step_events.py` add `capture_new_retrieval_events(messages, processed_count) -> tuple[list[dict], int]` that scans the same new tail used by `capture_new_step_messages`. For each `ToolMessage` whose `name` is `search_skills` or `knowledge_search`, call `retrieval_event_for_tool` and collect the normalized event. Keep the existing 8192-character `build_subagent_step` path for UI steps; retrieval capture reads the original message content.

- [ ] **Step 3: Wire the executor and task tool**

Add `retrievals` storage to `SubagentResult` with the same lock discipline as `steps`. In `SubagentExecutor._capture_steps` append new retrieval events. In `task_tool`, report pending retrievals as `task_retrieval` custom frames alongside `task_running`. Extend `subagent_run_event` or the worker's custom-frame mapper to record those rows as `retrieval.skill`/`retrieval.kb`, using `task_id` metadata.

- [ ] **Step 4: Run tests**

Run: `cd backend && uv run pytest tests/test_subagent_retrieval_events.py tests/test_subagent_event_buffer.py tests/test_step_events.py -v`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/packages/harness/agentkit/subagents \
  backend/packages/harness/agentkit/tools/builtin/task_tool.py \
  backend/packages/harness/agentkit/runtime/runs/worker.py \
  backend/tests/test_subagent_retrieval_events.py
git commit -m "feat: forward full retrieval evidence from subagents"
```

### Task 5: Create the evals workspace package and fixed-account login

**Files:**
- Create: `evals/pyproject.toml`
- Create: `evals/src/myflow_evals/__init__.py`
- Create: `evals/src/myflow_evals/accounts.py`
- Create: `evals/tests/test_accounts.py`
- Modify: `pyproject.toml` (add `"evals"` workspace member)

**Interfaces:**
- Consumes: `POST /api/v1/auth/login/local`, `GET /api/v1/auth/me`.
- Produces:
  - `@dataclass(frozen=True) class AccountCredential: label: str; username: str; password: str`
  - `def load_accounts(path: Path | None) -> list[AccountCredential]`
  - `def safe_agent_user_id(raw: str) -> str`
  - `def hashed_langfuse_user_id(agent_user_id: str, salt: str) -> str`
  - `@dataclass class AccountSession: credential: AccountCredential; client: httpx.Client; user_id: str; hashed_user_id: str`
  - `def login_account(credential: AccountCredential, *, base_url: str, salt: str) -> AccountSession`
  - `def login_all(accounts: Sequence[AccountCredential], *, base_url: str, salt: str) -> dict[str, AccountSession]` keyed by hashed ID.

- [ ] **Step 1: Write the failing account tests**

Use `httpx.MockTransport` to simulate login setting `access_token` and `csrf_token` cookies, then `/me` returning a UUID. Assert `hashed_langfuse_user_id` equals `HMAC-SHA256(salt, safe_agent_user_id(id))[:32]`, `needs_setup=true` raises, and one account's cookies do not leak into another session.

Run: `uv run --project evals pytest evals/tests/test_accounts.py -v`  
Expected: FAIL before the package exists.

- [ ] **Step 2: Add the package and workspace member**

Use a hatchling package with `src/myflow_evals`. Add only runtime deps needed by this task: `httpx`, `pyyaml`. Add a `[dependency-groups] dev = ["pytest>=9"]` group for the eval tests. Add `deepeval` as an optional `llm` extra later. Add `myflow-evals = "myflow_evals.cli:main"` only when Task 9 creates the CLI.

- [ ] **Step 3: Implement login/hash**

Read credentials from a secret JSON file or `AGENT_USERS`. Never log passwords. Login once per account, set `X-CSRF-Token` from the cookie, call `/me`, and fail if `needs_setup` is true. Implement `safe_agent_user_id` with the same `[A-Za-z0-9_-]` rule and 16-hex digest fallback as the backend.

- [ ] **Step 4: Run tests**

Run: `uv run --project evals pytest evals/tests/test_accounts.py -v`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml evals/pyproject.toml evals/src/myflow_evals evals/tests/test_accounts.py
git commit -m "feat: add eval workspace and fixed-account login"
```

### Task 6: Implement the Agent HTTP client and run evidence normalizer

**Files:**
- Create: `evals/src/myflow_evals/api.py`
- Create: `evals/src/myflow_evals/evidence.py`
- Create: `evals/tests/test_api.py`
- Create: `evals/tests/test_evidence.py`

**Interfaces:**
- Consumes: `AccountSession`; Gateway `/threads`, `/runs`, `/events` APIs.
- Produces:
  - `AgentApi.create_thread(session, thread_id, metadata) -> None`
  - `AgentApi.start_run(session, thread_id, query, metadata, context) -> str`
  - `AgentApi.wait_run(session, thread_id, run_id) -> RunSnapshot`
  - `AgentApi.list_events(session, thread_id, run_id, after_seq=None, event_types=None) -> list[dict]`
  - `@dataclass class RunEvidence: answer: str; tool_calls: list[dict]; business_tool_calls: list[dict]; subagent_calls: list[dict]; retrieved_tools: list[str]; retrieved_skills: list[str]; loaded_skills: list[str]; kb_called: bool; retrieved_chunks: list[dict]; citations: list[dict]; trace_id: str | None`
  - `def collect_evidence(messages: Sequence[Mapping], events: Sequence[Mapping], *, run_id: str) -> RunEvidence`

- [ ] **Step 1: Write failing client tests**

Use `httpx.MockTransport`. Assert `on_disconnect=continue`, CSRF header, thread/run URLs, terminal status handling, event pagination, and 404 propagation. Do not use a real Gateway.

- [ ] **Step 2: Write failing evidence tests**

Use fixtures for `llm.ai.response`, `llm.tool.result`, `subagent.start`, `retrieval.tool`, `retrieval.skill`, `retrieval.kb`, and a final AI message with `additional_kwargs.knowledge_citations`. Assert:
- `subagent_calls` extracts `task.args.subagent_type`;
- `business_tool_calls` unwraps `tool_execute.args.tool_name/arguments` and excludes infrastructure tools;
- `retrieved_tools` comes from `retrieval.tool`;
- `retrieved_skills` comes from `retrieval.skill`;
- `retrieved_chunks` and `citations` come from `retrieval.kb` / final message;
- missing retrieval events yield empty lists, not an exception.

- [ ] **Step 3: Implement the client and normalizer**

Make requests through the per-account `httpx.Client`. Use `POST /runs` plus polling by default; support `wait` only with `on_disconnect=continue`. Keep the event parser deterministic and side-effect free. Do not forward Dataset metadata wholesale into a run request.

- [ ] **Step 4: Run tests**

Run: `uv run --project evals pytest evals/tests/test_api.py evals/tests/test_evidence.py -v`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add evals/src/myflow_evals/api.py evals/src/myflow_evals/evidence.py \
  evals/tests/test_api.py evals/tests/test_evidence.py
git commit -m "feat: add eval agent client and evidence normalization"
```

### Task 7: Build datasets from the fixed account's real tools, Skills, knowledge, and traces

**Files:**
- Create: `evals/src/myflow_evals/mcp.py`
- Create: `evals/src/myflow_evals/build_dataset.py`
- Create: `evals/tests/test_mcp.py`
- Create: `evals/tests/test_build_dataset.py`
- Create: `evals/README.md`

**Interfaces:**
- Consumes: `AccountSession`, `AgentApi`, `GET /api/mcp-servers`, `GET /api/skills`, knowledge document/chunk APIs, Langfuse traces.
- Produces:
  - `@dataclass(frozen=True) class McpServer: id: str; url: str; headers: dict[str, str]`
  - `@dataclass(frozen=True) class McpTool: call_name: str; name: str; description: str; input_schema: dict[str, Any]`
  - `McpClient.list_tools(server: McpServer) -> list[McpTool]` using MCP JSON-RPC `tools/list` and per-server auth headers from a secret map.
  - `@dataclass(frozen=True) class DatasetItemDraft: id: str; input: dict[str, Any]; expected_output: dict[str, Any]; metadata: dict[str, Any]`
  - `def stable_item_id(kind: str, source_id: str, ordinal: int, user_hash: str) -> str` using `sha256` and a stable 16-hex suffix.
  - `def build_account_items(session: AccountSession, sources: BuildSources) -> list[DatasetItemDraft]`.
  - Every draft has `input={"query": ...}`, `expected_output=...`, and `metadata={"user_id": <hashed>, "source_id": ..., "source_kind": ..., "tool_schema_hash": ...}`.

- [ ] **Step 1: Write failing tests for MCP schema and idempotent IDs**

Simulate `GET /api/mcp-servers` returning a server URL and a secret header map. Assert direct `tools/list` sends JSON-RPC `{"method":"tools/list"}` and reads `inputSchema`. Assert the same `(kind, source_id, ordinal, user_hash)` always yields the same item ID and a different user yields a different ID.

- [ ] **Step 2: Write failing builder tests**

Use fake sources for:
- private Skill only (builtin excluded);
- tool items count derived from actual tools (not a hard-coded 1000);
- knowledge items carry `document_id` and `chunk_id` but only `expected_doc_ids` is emitted;
- `metadata.user_id` is the hashed ID, never the raw `/me.id` or a password;
- `--dry-run` performs no writes.

- [ ] **Step 3: Implement the sources and stable draft builder**

Call MCP `tools/list` directly; if a test server needs auth, read headers from `MCP_SERVER_HEADERS` secret storage, never from a Dataset or log. Use deterministic template synthesis for tests; put LLM synthesis behind an optional extra. Respect `--limit` and print a report with actual tool/Skill/document counts.

- [ ] **Step 4: Run tests**

Run: `uv run --project evals pytest evals/tests/test_mcp.py evals/tests/test_build_dataset.py -v`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add evals/src/myflow_evals/mcp.py evals/src/myflow_evals/build_dataset.py \
  evals/tests/test_mcp.py evals/tests/test_build_dataset.py evals/README.md
git commit -m "feat: build eval datasets from fixed-account data"
```

### Task 8: Implement rule metrics and retrieval citations

**Files:**
- Create: `evals/src/myflow_evals/evaluators.py`
- Create: `evals/tests/test_evaluators.py`
- Create: `evals/tests/test_citation_valid.py`

**Interfaces:**
- Consumes: `RunEvidence` from Task 6.
- Produces evaluator functions returning Langfuse `Evaluation` or `None`:
  - `retrieval_recall_at_5`, `retrieval_mrr`, `tool_hit`, `args_accuracy`, `extra_calls_ratio`
  - `skill_routing` (K0), `skill_recall_at_5`, `skill_mrr`, `skill_hit`, `skill_extra`
  - `kb_routing` (R0), `kb_recall_at_5`, `kb_mrr`, `citation_valid`
  - `forbidden_violation`, `no_tool_ok`
  - `RULE_EVALUATORS` and `THRESHOLDS`.

- [ ] **Step 1: Write failing K0/R0 tests**

Assert K0 passes only when `skill-finder` is in `subagent_calls`; K1 passes only from `retrieved_skills`; R0 passes only when `knowledge-researcher` is in `subagent_calls`; a `knowledge_search` call alone does not pass R0. Assert missing retrieval evidence returns `None` for K1/R1, not 0.

- [ ] **Step 2: Write failing tool/args/citation tests**

Assert `tool_hit` and `args_accuracy` map `tool_execute.args.tool_name/arguments` to business tools; `extra_calls_ratio` excludes `tool_search/tool_execute/task/load_skill`; `citation_valid` passes only when every `knowledge_citations[].chunk_id` exists in `retrieval.kb.items` and fails a forged ID.

- [ ] **Step 3: Implement the evaluators**

Keep every function pure and deterministic. Use `expected_doc_ids` only. Return `None` for inapplicable samples. Do not add user_id/permission/isolation metrics. Set `citation_valid` threshold to `0.95` and `extra_calls_ratio` threshold to `1.5`.

- [ ] **Step 4: Run tests**

Run: `uv run --project evals pytest evals/tests/test_evaluators.py evals/tests/test_citation_valid.py -v`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add evals/src/myflow_evals/evaluators.py evals/tests/test_evaluators.py \
  evals/tests/test_citation_valid.py
git commit -m "feat: add subagent-aware rule evaluators"
```

### Task 9: Run experiments with complete-item and compact-evidence guarantees

**Files:**
- Create: `evals/src/myflow_evals/runner.py`
- Create: `evals/src/myflow_evals/cli.py`
- Create: `evals/tests/test_runner.py`
- Create: `evals/tests/test_cli.py`
- Modify: `evals/pyproject.toml` (add CLI entry point)
- Create: `.github/workflows/agent-eval.yml`

**Interfaces:**
- Consumes: all prior evals modules; Langfuse `run_experiment`; `RunEvidence`.
- Produces:
  - `class EvidenceStore: put(item_id, evidence: RunEvidence) -> None; get(item_id: str) -> RunEvidence | None` backed by a lock.
  - `class IncompleteExperiment(RuntimeError): missing_item_ids: tuple[str, ...]`.
  - `class ExperimentRunner: run(dataset_name, run_name, mode, max_items, max_concurrency) -> ExperimentResult`.
  - CLI `myflow-evals build ...`, `myflow-evals run ...`, `myflow-evals ingest ...`.
  - Failure behavior: missing item, task exception, `citation_valid` violation, or threshold failure gives a non-zero exit code.

- [ ] **Step 1: Write the failing completeness test**

Build a fake Langfuse dataset with three item IDs. Make the fake `run_experiment` return only two item results. Assert `ExperimentRunner.run` raises `IncompleteExperiment` naming the missing ID and does not print a passing gate.

- [ ] **Step 2: Write the failing compact-output test**

Assert the task output sent to Langfuse contains only `answer`, `trace_id`, `status`, and `evidence_id`; full `retrieved_chunks` and citation payloads stay in `EvidenceStore` and are available to evaluators by `evidence_id`.

- [ ] **Step 3: Implement the runner**

Use a fixed account map keyed by hashed `metadata.user_id`. Create a unique thread for every item, start with `on_disconnect=continue`, collect events, store evidence, and return the compact output. Compare dataset IDs to result IDs after `run_experiment`; fail on any difference. Set `max_concurrency` default 2.

- [ ] **Step 4: Implement the CLI and CI**

Add `build/run/ingest` subcommands, `--dry-run`, `--mode ci|full`, `--dataset`, `--run-name`, `--limit`, and secret-file arguments. Add a CI workflow that runs the `ci` mode against the frozen regression dataset, uses a fixed stratified 30-item subset, and uploads/reports the result. In CI, rule metrics are hard gates; LLM metrics are soft gates and must have at least 20 samples before any threshold is treated as a hard failure. Nightly full mode may make LLM metrics hard gates. Do not run `build` in PR CI.

- [ ] **Step 5: Run tests**

Run: `uv run --project evals pytest evals/tests/test_runner.py evals/tests/test_cli.py -v`  
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add evals/src/myflow_evals/runner.py evals/src/myflow_evals/cli.py \
  evals/pyproject.toml evals/tests/test_runner.py evals/tests/test_cli.py \
  .github/workflows/agent-eval.yml
git commit -m "feat: add complete dataset experiment runner"
```

### Task 10: Document the operational contract and verify the end-to-end smoke path

**Files:**
- Modify: `evals/README.md`
- Create: `evals/tests/test_e2e_smoke.py`
- Modify: `docs/plans/Agent评测方案.md` only for implementation-status links (do not rewrite the spec decisions)

**Interfaces:**
- Consumes: CLI from Task 9.
- Produces: a local smoke test proving one fixed account, one tool sample, one private Skill sample, and one KB sample can be built/run with fakes, while a missing event or item fails.

- [ ] **Step 1: Write the failing smoke test**

Wire the fake Gateway and fake Langfuse through the CLI modules. Assert the three sample kinds route as expected:
- tool sample uses `retrieval.tool`;
- private Skill sample routes to `skill-finder` and uses `retrieval.skill` plus `load_skill`;
- KB sample routes to `knowledge-researcher` and uses `retrieval.kb` plus `knowledge_citations`.

Run: `uv run --project evals pytest evals/tests/test_e2e_smoke.py -v`  
Expected: FAIL until the wiring is complete.

- [ ] **Step 2: Document exact commands and secret handling**

Document `myflow-evals build/run` usage, `AGENT_USERS`/`MCP_SERVER_HEADERS`/Langfuse secrets, fixed-account hashing, `run_events.backend=db`, CI vs nightly modes, and how to interpret incomplete experiments. Include the `prompt_top_k=0` and subagent-availability preconditions.

- [ ] **Step 3: Run the smoke test and the full eval unit suite**

Run: `uv run --project evals pytest evals/tests -v`  
Expected: PASS.

- [ ] **Step 4: Run the backend evidence tests**

Run: `cd backend && uv run pytest tests/test_run_events_route.py tests/test_event_store_pagination.py tests/test_retrieval_events.py tests/test_run_journal_retrieval.py tests/test_subagent_retrieval_events.py -v`  
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add evals/README.md evals/tests/test_e2e_smoke.py docs/plans/Agent评测方案.md
git commit -m "docs: define eval operating contract and smoke test"
```

## Self-Review

- **Spec coverage:** Tasks 1–4 implement event read + structured retrieval evidence; 5–9 implement fixed accounts, dataset synthesis, evaluators, runner/CI; 10 documents operation.
- **Constraints:** No raw user IDs/passwords in Dataset; hashed `metadata.user_id`; all eval items route through subagents; no user metrics; `expected_doc_ids` only.
- **Completeness:** Task 9 explicitly fails on missing items; Task 10 smoke test proves the three sample paths.
- **Deliberate exclusions:** Builtin Skills, slash activation, permission/isolation scoring, manual Golden set, and dataset generation inside CI are out of scope.

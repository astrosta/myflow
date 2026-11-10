# Agent Evaluation Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete phase 2 of `docs/plans/Agent评测方案.md`: expand the fixed-account dataset to the design's category mix, add quality checks and trace backfill, and wire DeepEval/LLM judge metrics into the existing black-box experiment runner.

**Architecture:** Keep phase 1's boundary: `evals` remains an HTTP-only workspace package. A new deterministic synthesis layer expands each real source into the design's categories and runs dedup/distribution/solvability checks before Langfuse writes. A judge abstraction and two interchangeable engines (prompt-based and optional DeepEval) score only the metrics that cannot be decided by rules; the runner samples them deterministically and keeps the existing CI/full gate policy.

**Tech Stack:** Python 3.12, httpx, Langfuse SDK 5.1.x, DeepEval 4.x (optional `llm` extra), pytest.

**Spec:** `docs/plans/Agent评测方案.md` (sections 3, 4, 6, 6.4, 9 phase 2)

## Global Constraints

- No raw user id, username, password, judge key, or server credential in a Dataset item, Langfuse trace, log line, or committed file. `metadata.user_id` stays the Langfuse hashed id.
- Only effective sources are sampled: active/reachable MCP servers, tools the Gateway marks callable+enabled with a real `inputSchema`, enabled+active private Skills, readable knowledge chunks.
- `expected_tools` are MCP `call_name` values; R1 uses `expected_doc_ids` only.
- Every `expect_skill` / `expect_kb` sample must route through `skill-finder` / `knowledge-researcher`; no slash activation and no builtin/public Skills.
- Rule metrics stay hard gates. LLM metrics are deterministic-sampled in `ci` mode (default 0.2) and only become hard gates in `full` mode with at least 20 scored samples, per the existing runner policy.
- In `ci` mode only Faithfulness, answer_correctness, skill_adherence and refusal_ok run (sampled); ContextualPrecision/Relevancy and AnswerRelevancy are `full`-only. A metric with no applicable sample returns `None`, never 0.
- The eval package must work without DeepEval installed; DeepEval is an optional `llm` extra. The prompt engine is the dependency-free fallback.
- Dataset writes only in `build` and `ingest --write`; `run` treats the dataset as frozen input.
- Every task follows TDD: failing test, run it, minimal implementation, rerun, commit.

## Review Focus

1. **Sampling determinism:** the same item, metric, seed, and mode must always select the same LLM sample; rerunning a CI experiment must not change which items are scored.
2. **Metric applicability:** a no-answer sample is not punished by Faithfulness or answer_correctness; a truncated KB retrieval makes Faithfulness unavailable; a trace replay stays unscored.
3. **Judge failure isolation:** a judge timeout or malformed JSON must not kill the experiment or turn into 0.0; it is counted and reported, and rule gates still decide the run.
4. **Dataset quality:** duplicate or structurally invalid drafts must be dropped/reported before they reach Langfuse; `--strict` must fail a build instead of writing a bad regression set.
5. **Secret hygiene:** judge keys, prompts containing retrieved user data, and raw account ids stay out of Langfuse experiment outputs and CLI summaries.

---

### Task 1: Judge configuration and OpenAI-compatible chat client

**Files:**
- Create: `evals/src/myflow_evals/llm.py`
- Test: `evals/tests/test_llm.py`

**Interfaces:**
- Consumes: environment mappings, `httpx.Client`.
- Produces:
  - `@dataclass(frozen=True) class JudgeConfig: base_url: str; api_key: str; model: str; temperature: float = 0.0; timeout_seconds: float = 60.0; max_tokens: int = 2048` with `api_key` excluded from `repr`.
  - `class JudgeError(RuntimeError)`
  - `class ChatModel(Protocol): def complete(self, *, system: str, prompt: str) -> str: ...`
  - `class OpenAICompatibleChat: def __init__(self, config, *, transport=None) -> None; def complete(self, *, system, prompt) -> str`
  - `def judge_config_from_env(environ) -> JudgeConfig | None` (None when unset, raises `JudgeError` on a partial config)
  - `def model_name(environ) -> str | None`

**Steps:**
- [ ] Write tests with `httpx.MockTransport`: POST to `/chat/completions` with bearer key, messages, temperature, max_tokens; parse `choices[0].message.content`; reject non-2xx, non-JSON, empty content; missing/partial env behavior; `repr(config)` never contains the key.
- [ ] Run `uv run --project evals pytest evals/tests/test_llm.py -v`; expected FAIL (module missing).
- [ ] Implement `llm.py`. Endpoint rule: if the base URL already ends in `/chat/completions` use it, otherwise append `/chat/completions` (a base ending in `/v1` keeps its path). Timeout and HTTP errors become `JudgeError` with only a class name and status.
- [ ] Rerun the test; expected PASS. Commit.

### Task 2: Prompt-based judge metrics

**Files:**
- Create: `evals/src/myflow_evals/llm_metrics.py`
- Test: `evals/tests/test_llm_metrics.py`

**Interfaces:**
- Consumes: `RunEvidence`, `ChatModel`.
- Produces:
  - `@dataclass(frozen=True) class JudgeCase: item_id: str; query: str; answer: str; expected_output: Mapping[str, Any] | None; evidence: RunEvidence; metadata: Mapping[str, Any] | None`
  - `@dataclass(frozen=True) class JudgeScore: value: float; comment: str`
  - `class LLMEngine(Protocol): def score(self, metric: str, case: JudgeCase) -> JudgeScore | None: ...`
  - `class PromptJudgeEngine: __init__(self, judge: ChatModel) -> None; score(...)`
  - `def metric_applicable(metric: str, case: JudgeCase) -> bool`
  - `LLM_METRICS: tuple[str, ...]`, `CI_LLM_METRICS: frozenset[str]`, `FULL_ONLY_LLM_METRICS: frozenset[str]`, `LLM_THRESHOLDS: dict[str, dict[str, Any]]`
- Metrics and names: `faithfulness`, `answer_correctness`, `answer_quality`, `answer_relevancy`, `contextual_recall`, `contextual_precision`, `contextual_relevancy`, `skill_adherence`, `refusal_ok`, `tool_correctness`.

**Steps:**
- [ ] Write failing tests with a fake `ChatModel` returning fixed JSON: each metric's prompt contains the right inputs; scores are clamped to 0..1; malformed JSON/out-of-range raises `JudgeError`; applicability honors `expect_refusal`, empty chunks, `kb_truncated`, missing reference, trace replays, missing skill rules, and expected tools.
- [ ] Run and observe FAIL.
- [ ] Implement prompt builders and JSON extraction. Truncate context deterministically (per chunk 4000 chars, total 12000; answer 8000). `refusal_ok` short-circuits: clear no-information marker and no citations -> 1.0; expected refusal but citations present -> 0.0; otherwise ask the judge. Tool correctness computes the set overlap of called vs expected business tools when no judge is needed. No metric ever prompts for secrets/user ids.
- [ ] Rerun; expected PASS. Commit.

### Task 3: Optional DeepEval engine

**Files:**
- Create: `evals/src/myflow_evals/deepeval_engine.py`
- Modify: `evals/pyproject.toml` (optional extra `llm = ["deepeval>=4.3,<5"]`)
- Test: `evals/tests/test_deepeval_engine.py`

**Interfaces:**
- Consumes: `ChatModel`, `JudgeCase`, `JudgeScore`.
- Produces:
  - `def deepeval_available() -> bool`
  - `class DeepEvalEngine: __init__(self, judge: ChatModel) -> None; score(metric, case) -> JudgeScore | None`
  - `def build_engine(judge: ChatModel | None, *, preference: str = "auto") -> LLMEngine | None` where preference is `auto|prompt|deepeval`; `deepeval` requested but unavailable raises `JudgeError`.
  - Module-level `_DeepEvalChat(DeepEvalBaseLLM)` adapter implementing `load_model`, `generate`, `a_generate`, `get_model_name`.

**Steps:**
- [ ] Write tests that inject a fake `deepeval` package into `sys.modules` (metrics, test_case, models) and assert the engine builds the documented metric classes with `async_mode=False`, passes `retrieval_context` from chunks and `expected_output` from `reference_answer`, and converts `metric.score`/`metric.reason` into `JudgeScore`. Assert `auto` falls back to `PromptJudgeEngine` when the import fails; assert `deepeval` preference raises.
- [ ] Run and observe FAIL.
- [ ] Implement lazy imports inside functions/classes. GEval steps: answer_correctness (facts/values), answer_quality (judge_rule/key_points), skill_adherence (skill_rules + tool-call log), refusal_ok (no-information instruction). All `async_mode=False`. `ToolCorrectnessMetric` for `tool_correctness`.
- [ ] Run the fake-module tests; then run an isolated API smoke with `uv run --isolated --with 'deepeval==4.3.3' python -c "import deepeval_engine; ..."` to prove the real import paths/constructor signatures (no LLM call).
- [ ] Rerun; expected PASS. Commit.

### Task 4: Deterministic LLM sampling and runner wiring

**Files:**
- Modify: `evals/src/myflow_evals/runner.py`
- Modify: `evals/src/myflow_evals/cli.py`
- Modify: `evals/src/myflow_evals/__init__.py`
- Test: `evals/tests/test_runner.py`, `evals/tests/test_cli.py`

**Interfaces:**
- Consumes: `LLMEngine`, `LLM_THRESHOLDS`, `CI_LLM_METRICS`, `FULL_ONLY_LLM_METRICS`.
- Produces:
  - `@dataclass(frozen=True) class LLMSamplingPolicy: mode: str; ratio: float; seed: str = "agent-eval"` with `metric_enabled(metric) -> bool` and `should_score(item_id: str, metric: str) -> bool`
  - `DEFAULT_CI_LLM_SAMPLE = 0.2`
  - `class LLMMetricAdapter` (Langfuse evaluator) with `errors: tuple[str, ...]`
  - `ExperimentRunner(..., llm_engine=None, llm_sample_ratio=None, llm_seed=None)`
  - `ExperimentResult.llm_error_count: int = 0`
  - Experiment metadata keys `llm_engine`, `judge_model`, `llm_sample_ratio`, `dataset_fingerprint`.
  - CLI flags `--metric-engine {auto,prompt,deepeval}` and `--llm-sample FLOAT`; missing judge config logs one warning and runs rule-only.

**Steps:**
- [ ] Write failing tests: same item/metric/seed is stable across calls and modes; ratio 0/1 boundaries; `ci` excludes full-only metrics; adapter returns evaluations for applicable metrics only and records judge exceptions without raising; runner includes both adapters and reports `llm_error_count`; gate rows for LLM thresholds are `soft` in ci and `fail` in full with >=20 samples.
- [ ] Run; expected FAIL.
- [ ] Implement sampling as `sha256(f"{seed}\x1f{mode}\x1f{metric}\x1f{item_id}")` first 8 bytes / 2**64 < ratio. Apply applicability first, then sampling. Wrap every `engine.score` in try/except `Exception`, count, and skip. Add the `user_id` from item metadata to each `Evaluation` metadata (both adapters).
- [ ] Wire CLI: build judge from env, call `build_engine`, pass to runner; unknown engine/unavailable DeepEval is exit 2; no judge is a warning, not a config error.
- [ ] Rerun; expected PASS. Commit.

### Task 5: Expanded deterministic synthesis for tool samples

**Files:**
- Create: `evals/src/myflow_evals/synthesis.py`
- Test: `evals/tests/test_synthesis.py`

**Interfaces:**
- Consumes: `BuildSources`, `McpTool`, `AccountSession`, existing `stable_item_id`.
- Produces:
  - `@dataclass(frozen=True) class SynthesisPlan: tool_samples_per_source=2; skill_samples_per_source=2; knowledge_samples_per_source=1; similar_pairs=20; include_missing_params=True; include_multi_tool=True; include_boundary=True; include_injection=True; max_total=None`
  - `@dataclass(frozen=True) class SynthesisResult: drafts: tuple[DatasetItemDraft, ...]; warnings: tuple[str, ...]; dropped_duplicates: int`
  - `def synthesize_account_items(session, sources, *, plan=None, writer=None, embedder=None) -> SynthesisResult`
  - `def dedupe_drafts(drafts, *, embedder=None) -> tuple[list[DatasetItemDraft], int]`
  - `def dataset_fingerprint(drafts) -> str`

**Steps:**
- [ ] Write failing tests: 3 tools with schemas produce 2 `single_tool` each, 1 `missing_params` when required args exist, and no missing-params draft otherwise; stable ids differ by ordinal/category; similar-tool pairs come from description token overlap; multi-tool pairs share a server and expect both calls; queries and expected outputs are valid and never name a user id.
- [ ] Run; expected FAIL.
- [ ] Implement tool planning with deterministic phrasings and category metadata. `missing_params` declares `expect_clarification: true`, no expected calls, and no forbidden tool. `similar_tools` picks the highest-Jaccard unordered pairs (cap `similar_pairs`), target first. `multi_tool` pairs a read-like name with a write-like name from the same server. Skip unsafe call names with a warning.
- [ ] Rerun; expected PASS. Commit.

### Task 6: Skill, knowledge, boundary, and injection synthesis with quality checks

**Files:**
- Modify: `evals/src/myflow_evals/synthesis.py`
- Modify: `evals/src/myflow_evals/build_dataset.py` (multi-chunk source fields, backward-compatible defaults)
- Test: `evals/tests/test_synthesis.py`, `evals/tests/test_build_dataset.py`

**Interfaces:**
- Consumes: `SkillSource`, `KnowledgeSource`, `TraceSource`.
- Produces:
  - `KnowledgeSource.related_chunks: tuple[tuple[str, str], ...] = ()`, `section: str = ""`, `page: int = 0`
  - Skill categories: `skill_single`, `similar_skills`, `skill_with_tools` (only when a tool call_name is named in the description).
  - KB categories: `kb_single_hop`, `kb_multi_hop` (two chunks), `kb_unanswerable` (expected refusal, no doc ids, capped per account).
  - Boundary/safety: `no_tool` fixed boundary questions; `injection` only for a detected destructive tool, with `forbidden_tools` set to it.
  - `@dataclass(frozen=True) class QualityReport: total: int; category_counts; source_counts; difficulty_counts; dropped_duplicates: int; problems: tuple[str, ...]; warnings: tuple[str, ...]` with `ok` and `render()`
  - `def quality_report(drafts, *, sources=None, dropped_duplicates=0) -> QualityReport`
- Quality problems: missing/non-hashed `metadata.user_id`, missing query, no declared expectation, duplicate item id, duplicate normalized query, unsafe expected tool name, KB draft whose doc id is not in the account sources. Warnings (not failures): category share outside the design ratio, empty category, all questions from one source.
- Dedup: exact normalized query first; then 3-gram shingle Jaccard >= 0.9; optional `Embedder.embed(texts)` cosine >= 0.9 when configured.

**Steps:**
- [ ] Write failing tests for every category and every quality problem; assert a no-answer draft has `expect_refusal=True`/`expected_doc_ids=[]`; assert multi-hop only appears when `related_chunks` exists; assert duplicate and invalid drafts are reported.
- [ ] Run; expected FAIL.
- [ ] Implement. Extend `_sampled_chunk` into a helper that returns up to two distinct readable chunks while keeping the existing one-chunk behavior for callers.
- [ ] Rerun the two files; expected PASS. Commit.

### Task 7: Optional LLM question rewriting and skill rules

**Files:**
- Modify: `evals/src/myflow_evals/synthesis.py`
- Test: `evals/tests/test_synthesis.py`

**Interfaces:**
- Produces:
  - `class QuestionWriter(Protocol): def rewrite(self, *, kind: str, queries: Sequence[str], context: str) -> list[str]; def skill_rules(self, *, name: str, description: str, body: str) -> list[str]`
  - `class OpenAIQuestionWriter: __init__(self, judge: ChatModel) -> None`
  - `synthesize_account_items(..., writer: QuestionWriter | None = None)`
- Writer failures return the deterministic input and add a warning; they never abort a build. Rewritten queries are pure user requests (no expected tool name is required); `skill_rules` are stored in `expected_output.skill_rules`.

**Steps:**
- [ ] Write failing tests with a fake writer: query rewrite is used, count is preserved, a writer exception falls back, skill rules appear for a skill sample, and the LLM is not called without a writer.
- [ ] Implement JSON-only prompts, parse fenced JSON, validate string lists, and fall back on any error.
- [ ] Rerun; expected PASS. Commit.

### Task 8: Build CLI integration, profiles, strict gate, trace write-back

**Files:**
- Modify: `evals/src/myflow_evals/cli.py`
- Modify: `evals/src/myflow_evals/build_dataset.py`
- Test: `evals/tests/test_cli.py`, `evals/tests/test_e2e_smoke.py`

**Interfaces:**
- Produces CLI flags: `--no-expand` (legacy one-draft-per-source), `--strict`, `--llm-synthesis`, and `ingest --write`.
- `BuildReport` gains `fingerprint: str | None`, `quality: QualityReport | None`, `expanded: bool`; `render()` appends `fingerprint:`, `category: ...`, and quality warning lines.
- Build behavior: expanded by default; strict failures return exit 2 before any write; trace replay is excluded unless `--include-traces`; ingest prints by default and writes only with `--write` (and never with `--dry-run`).

**Steps:**
- [ ] Write failing CLI tests: expanded build writes more than one item per tool; `--strict` rejects a duplicate/invalid draft and writes nothing; `--no-expand` keeps the phase 1 counts; `ingest --write` upserts trace drafts with `review_status=pending`; `ingest` alone still writes nothing.
- [ ] Run; expected FAIL.
- [ ] Implement by threading `SynthesisPlan`, `QuestionWriter`, and `QualityReport` through `_cmd_build`; keep the legacy path unchanged. Extend `BuildReport` with defaults so existing constructors/tests still work.
- [ ] Rerun CLI/smoke tests; expected PASS. Commit.

### Task 9: Complete metric gates, experiment metadata, docs, and CI

**Files:**
- Modify: `evals/README.md`
- Modify: `evals/使用指南.md`
- Modify: `evals/env.template`
- Modify: `.github/workflows/agent-eval.yml`
- Modify: `docs/plans/Agent评测方案.md` (implementation-status paragraph only)
- Test: `evals/tests/test_runner.py`, `evals/tests/test_cli.py`

**Interfaces:**
- Produces: all phase 2 thresholds in `THRESHOLDS` with `kind: "llm"`; documented `EVAL_JUDGE_*`, `EVAL_METRIC_ENGINE`, `EVAL_LLM_SAMPLE`, `EVAL_LLM_SEED`, `EVAL_EMBEDDING_*`; nightly workflow runs with the `llm` extra and judge secrets, PR CI stays rule-only unless a judge secret is present.

**Steps:**
- [ ] Write failing tests for the threshold registry: every design metric has a row, LLM rows are `kind: llm`, CI/full gate behavior, and the experiment metadata carries engine/model/fingerprint without a key.
- [ ] Run; expected FAIL.
- [ ] Fill thresholds, metadata, and docs; update the workflow with an optional judge secret block and `uv run --project evals --extra llm`.
- [ ] Run the full eval suite and the backend evidence suite; expected PASS.
- [ ] Commit.

## Self-Review

- **Spec coverage:** dataset categories/ratios and QC (4.1-4.4), ID mapping (4.5), DeepEval RAG metrics and GEval judges (6.1-6.3), cost sampling (6.4), phase 2 deliverables (9).
- **Constraints:** no user metrics, hashed ids only, effective sources only, rule-first gating, no DeepEval hard dependency.
- **Deliberate exclusions:** manual Golden set, Langfuse-side LLM-as-a-Judge configuration, embedding-model benchmarking, and a UI for eval management. `kb_conflict`/hybrid samples remain LLM-synthesis-only (category supported, deterministic fallback skips them).

# OpenViking 记忆模块实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 在 MyFlow 中落地 OpenViking 薄适配记忆层：显式 session policy、idle-only auto-commit、单 worker、at-least-once dedup/reconcile、无压缩捕获、无自动召回。

**Architecture:** 新增独立的 `agentkit.agents.memory.openviking` 包，先与现有 canonical memory 共存，不删除旧模块。新包只负责 wire adapter、capture stream、dedup、session、worker 和 facade；profile/identity/soul 仍由 MyFlow 自己的服务处理。旧模块删除放到后续 P5 计划，避免本次改动同时承担迁移和新链路风险。

**Tech Stack:** Python 3.12、Pydantic 2、SQLAlchemy 2 async、Redis Streams、LangChain Core message types、pytest/pytest-asyncio。OpenViking Server 0.4.19，客户端 `openviking-sdk==0.1.17`（SDK 没有 0.4.19）。

**Spec:** `docs/plans/2026-10-29-openviking-memory-module-design.md`

> **Implementation status:** core slice implemented on `feat/openviking-memory`; 59 new tests pass. Real Redis Streams transport, the SDK error wrapper, capture middleware, profile HTTP routes, recall and the content envelope are implemented; default-chain wiring, frontend and legacy deletion remain deferred.

## Global Constraints

- OpenViking Server 固定 0.4.19；SDK 固定 0.1.17；不引入 `langchain-openviking`。
- Session 只能显式创建：`get_session(auto_create=False)` → `create_session`；禁止用 `get_session(auto_create=True)` 新建。
- 每个 session 的 `memory_policy` 只列 8 类；`profile` / `identity` / `soul` 是 Server 内置类型，必须由白名单排除。
- `auto_commit_policy` 固定 `pending_token_threshold=0`、`message_count_threshold=0`、`idle_timeout_seconds=86400`、`keep_recent_count=0`、`min_commit_interval_seconds=0`。
- 压缩窗口不进入 Redis、不 append、不 commit；没有 `message_kind=compaction`。
- 无自动召回：不调用 `search_context`，不调用 `ls` 取记忆索引。
- 单 `memory_worker`；每个 shard 单 consumer；singleton lease。
- 正常 `appended` 是终态，不做启动全量对账；只处理 `pending/appending` 和 `appended_at IS NULL`。
- 工具输出只允许脱敏、截断后的摘要/hash；ToolMessage wire `message_kind=tool_transport`。
- 不修改现有 canonical memory 的运行时行为；本计划只新增模块、表、配置和测试。

## Review Focus

- Redis Stream 的 PEL 不是破坏性读取：worker 崩溃/重启会重投，必须由 dedup 和 reconcile 收敛。
- `appended` 的 thread 级状态与 message 级 dedup 状态不能混为一谈。
- 0.4.19 的 `auto_commit_policy` 0 阈值语义；不要把 0 当成“立即提交”。
- OpenViking 的 `profile/identity/soul` 默认启用；任何新建 session 漏掉 memory policy 都会写错位置。
- `batch_add_messages` 的原子性和 `source_message_ids` 可对账性。

---

### Task 1: Contracts、配置和 session policy

**Files:**
- Create: `backend/packages/harness/agentkit/agents/memory/openviking/__init__.py`
- Create: `backend/packages/harness/agentkit/agents/memory/openviking/contracts.py`
- Create: `backend/packages/harness/agentkit/agents/memory/openviking/config.py`
- Test: `backend/tests/test_openviking_contracts.py`

**Interfaces:**
- Produces: `MemoryType`, `SignalKind`, `TurnDecision`, `SessionRef`, `CapturedMessage`, `ToolEvidence`, `ProfileView`, `MemoryHit` in `contracts.py`.
- Produces: `OpenVikingMemoryConfig`, `SessionPolicyConfig`, `CaptureStreamConfig`, `CaptureConfig`, `RetentionConfig` in `config.py`.
- Produces: `DEFAULT_MEMORY_TYPES` and `idle_only_auto_commit_policy()`.

- [x] **Step 1: Write the failing test**

```python
from agentkit.agents.memory.openviking.config import (
    DEFAULT_MEMORY_TYPES,
    OpenVikingMemoryConfig,
    idle_only_auto_commit_policy,
)

def test_idle_only_policy_disables_thresholds():
    policy = idle_only_auto_commit_policy()
    assert policy["pending_token_threshold"] == 0
    assert policy["message_count_threshold"] == 0
    assert policy["idle_timeout_seconds"] == 86400
    assert "profile" not in DEFAULT_MEMORY_TYPES
    assert len(DEFAULT_MEMORY_TYPES) == 8

def test_default_config_uses_explicit_session_policy():
    config = OpenVikingMemoryConfig()
    options = config.session_create_options()
    assert options["auto_commit_policy"]["idle_timeout_seconds"] == 86400
    assert options["memory_policy"]["working_memory"] == {"enabled": False}
    assert options["memory_policy"]["peer"] == {"enabled": False}
```

- [x] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_openviking_contracts.py -q`  
Expected: FAIL because `agentkit.agents.memory.openviking` does not exist.

- [x] **Step 3: Implement minimal code**

Create the dataclasses/Literals and Pydantic models. `SessionPolicyConfig.session_create_options()` returns a fresh dict with the 8 memory types and the idle-only policy. Do not import `openviking_sdk` at module import time.

- [x] **Step 4: Run test to verify it passes**

Run the command from Step 2. Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add backend/packages/harness/agentkit/agents/memory/openviking backend/tests/test_openviking_contracts.py
git commit -m "feat(memory): add OpenViking contracts and config"
```

---

### Task 2: Turn policy

**Files:**
- Create: `backend/packages/harness/agentkit/agents/memory/openviking/policy.py`
- Test: `backend/tests/test_openviking_policy.py`

**Interfaces:**
- Produces: `classify_turn(user_text: str, *, has_attachments: bool = False, extra_skip_tokens: tuple[str, ...] = ()) -> TurnDecision`.
- `TurnDecision.signals` defaults to `()`; empty means no signal. `NONE` is not emitted.

- [x] **Step 1: Write the failing table test**

```python
import pytest
from agentkit.agents.memory.openviking.policy import classify_turn

@pytest.mark.parametrize("text,expected", [
    ("你好", "skip"),
    ("谢谢", "skip"),
    ("👍", "skip"),
    ("好的", "capture"),
    ("继续", "capture"),
    ("我住杭州", "capture"),
    ("你好，帮我改这个函数", "capture"),
    ("谢谢，不过请记住我偏好简短回答", "capture"),
])
def test_turn_policy(text, expected):
    assert classify_turn(text).disposition == expected

def test_attachments_force_capture():
    assert classify_turn("", has_attachments=True).disposition == "capture"
```

- [x] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_openviking_policy.py -q`  
Expected: FAIL because `policy.py` does not exist.

- [x] **Step 3: Implement `classify_turn`**

Use exact-match social token sets for greetings/farewells/thanks, pure punctuation/emoji detection, and explicit task tokens (`好的/收到/可以/继续/完成/好了/记住`). Unknown text defaults to CAPTURE. Reject `extra_skip_tokens` that intersect task tokens with `ValueError`.

- [x] **Step 4: Run test to verify it passes**

Run the command from Step 2. Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add backend/packages/harness/agentkit/agents/memory/openviking/policy.py backend/tests/test_openviking_policy.py
git commit -m "feat(memory): add deterministic turn policy"
```

---

### Task 3: LangChain → OpenViking adapter

**Files:**
- Create: `backend/packages/harness/agentkit/agents/memory/openviking/adapter.py`
- Test: `backend/tests/test_openviking_adapter.py`

**Interfaces:**
- Produces: `MemoryMessageAdapter(redactor: PIIRedactor | None = None, max_tool_chars: int = 2000)`.
- `is_recordable(message: Any) -> bool`.
- `to_payloads(message: Any) -> list[dict[str, Any]]`; one payload per LangChain message.

- [x] **Step 1: Write failing tests**

```python
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from agentkit.agents.memory.openviking.adapter import MemoryMessageAdapter

def test_human_payload_has_user_query_kind():
    payload = MemoryMessageAdapter().to_payloads(HumanMessage(content="你好"))[0]
    assert payload["role"] == "user"
    assert payload["message_kind"] == "user_query"
    assert payload["parts"] == [{"type": "text", "text": "你好"}]

def test_tool_message_is_tool_transport_and_redacted():
    message = ToolMessage(content="token=sk-abcdefghijklmnopqrstuvwxyz", tool_call_id="c1", name="shell")
    payload = MemoryMessageAdapter().to_payloads(message)[0]
    assert payload["role"] == "assistant"
    assert payload["message_kind"] == "tool_transport"
    tool = payload["parts"][0]
    assert tool["tool_id"] == "c1"
    assert tool["tool_name"] == "shell"
    assert tool["tool_status"] == "completed"
    assert "sk-abcdef" not in tool["tool_output"]
    assert tool["tool_output_sha256"]

def test_ai_tool_call_keeps_id_and_name():
    message = AIMessage(content="", tool_calls=[{"id": "c1", "name": "shell", "args": {"cmd": "ls"}}])
    parts = MemoryMessageAdapter().to_payloads(message)[0]["parts"]
    assert parts[0]["type"] == "tool"
    assert parts[0]["tool_id"] == "c1"
    assert parts[0]["tool_name"] == "shell"
    assert parts[0]["tool_status"] == "pending"

def test_system_and_empty_are_not_recordable():
    adapter = MemoryMessageAdapter()
    assert not adapter.is_recordable(SystemMessage(content="system"))
    assert not adapter.is_recordable(HumanMessage(content="  "))
```

- [x] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_openviking_adapter.py -q`  
Expected: FAIL because `adapter.py` does not exist.

- [x] **Step 3: Implement adapter**

Use `PIIRedactor().redact_for_memory()`; use `hashlib.sha256` for `tool_output_sha256`; truncate tool output to `max_tool_chars`; map `ToolMessage.status` to `completed/error`; skip `hide_from_ui`, provenance, summary messages, and empty content.

- [x] **Step 4: Run test to verify it passes**

Run the command from Step 2. Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add backend/packages/harness/agentkit/agents/memory/openviking/adapter.py backend/tests/test_openviking_adapter.py
git commit -m "feat(memory): add OpenViking message adapter"
```

---

### Task 4: SQLAlchemy tables and migration 0008

**Files:**
- Create: `backend/packages/harness/agentkit/agents/memory/openviking/models.py`
- Create: `backend/packages/harness/agentkit/persistence/migrations/versions/0008_openviking_memory.py`
- Modify: `backend/packages/harness/agentkit/persistence/schema.py`
- Test: `backend/tests/test_openviking_schema.py`

**Interfaces:**
- Produces: `MemoryProfileDocumentRow`, `MemorySyncStateRow`, `MemoryProfileOnboardingRow`, `MemoryProfileSuggestionRow`, `MemoryCaptureDedupRow`.
- Table names exactly: `memory_profile_documents`, `memory_sync_state`, `memory_profile_onboarding`, `memory_profile_suggestions`, `memory_capture_dedup`.
- `memory_capture_dedup` includes the check constraint described in the spec.

- [x] **Step 1: Write failing schema test**

```python
from sqlalchemy import inspect
from agentkit.persistence.base import Base

def test_openviking_tables_are_registered():
    names = set(Base.metadata.tables)
    assert {
        "memory_profile_documents", "memory_sync_state",
        "memory_profile_onboarding", "memory_profile_suggestions",
        "memory_capture_dedup",
    } <= names

def test_dedup_state_check_exists():
    table = Base.metadata.tables["memory_capture_dedup"]
    assert any("state" in str(c.sqltext) for c in table.constraints if hasattr(c, "sqltext"))
```

- [x] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_openviking_schema.py -q`  
Expected: FAIL because the tables are not registered.

- [x] **Step 3: Implement models and migration**

Use SQLAlchemy 2.0 typed models. Add imports to `persistence/schema.py`. Migration 0008 uses `Base.metadata.tables[name].create(bind, checkfirst=True)` for the five tables and has a reversing downgrade. Do not import `openviking_sdk`.

- [x] **Step 4: Run test to verify it passes**

Run the command from Step 2. Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add backend/packages/harness/agentkit/agents/memory/openviking/models.py backend/packages/harness/agentkit/persistence/schema.py backend/packages/harness/agentkit/persistence/migrations/versions/0008_openviking_memory.py backend/tests/test_openviking_schema.py
git commit -m "feat(memory): add OpenViking persistence schema"
```

---

### Task 5: State stores and message-level dedup

**Files:**
- Create: `backend/packages/harness/agentkit/agents/memory/openviking/store.py`
- Test: `backend/tests/test_openviking_store.py`

**Interfaces:**
- Produces: `MemorySyncStore(session_factory)` with `get_or_create`, `get`, `set_state`, `mark_deleted`.
- Produces: `CaptureDedupStore(session_factory)` with `reserve`, `mark_appending`, `mark_appended`, `uncertain`.
- `uncertain(limit=100)` returns only `pending`, `appending`, and `appended_at IS NULL` rows.

- [x] **Step 1: Write failing tests with SQLite**

```python
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from agentkit.agents.memory.openviking.models import Base
from agentkit.agents.memory.openviking.store import CaptureDedupStore
from agentkit.agents.memory.openviking.contracts import CapturedMessage

@pytest.mark.asyncio
async def test_dedup_reserve_and_append():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    store = CaptureDedupStore(async_sessionmaker(engine, expire_on_commit=False))
    captured = CapturedMessage(message_id="m1", user_id="u1", thread_id="t1", turn_id="r1", session_epoch=1, session_incarnation="i1", sequence=0, kind="human", role="user", message={"type": "human", "content": "hi"})
    assert await store.reserve(captured) == "pending"
    assert await store.mark_appending(captured, lease_seconds=60) is True
    await store.mark_appended(captured, ov_message_ids=["ov1"])
    assert await store.uncertain() == []
```

- [x] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_openviking_store.py -q`  
Expected: FAIL because `store.py` does not exist.

- [x] **Step 3: Implement stores**

Use the PK from the spec. `reserve` returns the existing state or inserts `pending`. `mark_appending` only transitions from `pending` or an expired `appending`. `mark_appended` sets state, `appended_at`, `ov_message_ids`, and clears lease. Do not scan normal `appended` rows in `uncertain`.

- [x] **Step 4: Run test to verify it passes**

Run the command from Step 2. Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add backend/packages/harness/agentkit/agents/memory/openviking/store.py backend/tests/test_openviking_store.py
git commit -m "feat(memory): add OpenViking state and dedup stores"
```

---

### Task 6: SessionManager and explicit session policy

**Files:**
- Create: `backend/packages/harness/agentkit/agents/memory/openviking/session.py`
- Test: `backend/tests/test_openviking_session.py`

**Interfaces:**
- Produces: `SessionClient` protocol (`get_session`, `create_session`, `update_session_config`, `delete_session`).
- Produces: `SessionManager(sync_store, client, config)` with `resolve_capture_context`, `ensure_session`, `delete_session`.
- `ensure_session` calls `get_session(auto_create=False)`; NotFound calls `create_session` with `config.session_create_options()`. It never passes `auto_create=True`.

- [x] **Step 1: Write failing test with a fake client**

```python
@pytest.mark.asyncio
async def test_ensure_session_creates_explicitly(tmp_path):
    # build SQLite engine + sync store, fake client recording calls
    ref = await manager.resolve_capture_context("u1", "t1")
    await manager.ensure_session(ref)
    assert client.created[0]["options"]["auto_commit_policy"]["idle_timeout_seconds"] == 86400
    assert client.get_calls[0]["auto_create"] is False
```

Also test `AlreadyExistsError` → GET, and `NotFoundError` → create.

- [x] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_openviking_session.py -q`  
Expected: FAIL because `session.py` does not exist.

- [x] **Step 3: Implement SessionManager**

Compute `ov_user` with HMAC-SHA256(salt, user_id)[:32] and `ov_session_id` with `mf-s-` + HMAC(...incarnation). Generate a UUID incarnation once. Use `MemorySyncStore`. Do not create an OpenViking session in `resolve_capture_context`.

- [x] **Step 4: Run test to verify it passes**

Run the command from Step 2. Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add backend/packages/harness/agentkit/agents/memory/openviking/session.py backend/tests/test_openviking_session.py
git commit -m "feat(memory): add explicit OpenViking session manager"
```

---

### Task 7: Capture stream, publisher, and single worker

**Files:**
- Create: `backend/packages/harness/agentkit/agents/memory/openviking/stream.py`
- Create: `backend/packages/harness/agentkit/agents/memory/openviking/worker.py`
- Test: `backend/tests/test_openviking_worker.py`

**Interfaces:**
- Produces: `InMemoryCaptureStream` with `publish`, `claim`, `ack`.
- Produces: `MemoryMessagePublisher(stream, sessions)` with `publish_human/ai/tool`.
- Produces: `TurnCaptureWorker(adapter, stream, dedup, sessions, sync_store)` with `process(captured)`.

- [x] **Step 1: Write failing worker tests**

```python
@pytest.mark.asyncio
async def test_worker_appends_and_marks_appended():
    captured = make_captured(HumanMessage(content="我喜欢 pytest"))
    await worker.process(captured)
    assert session.appended[0]["message_kind"] == "user_query"
    assert await dedup.state(captured) == "appended"
    assert stream.acked == [captured.stream_id]

@pytest.mark.asyncio
async def test_duplicate_delivery_is_skipped():
    await worker.process(captured)
    await worker.process(captured)
    assert len(session.appended) == 1
```

Also test `kind=control` never calls append, and epoch mismatch does not append.

- [x] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_openviking_worker.py -q`  
Expected: FAIL because `stream.py` / `worker.py` do not exist.

- [x] **Step 3: Implement minimal stream, publisher, worker**

No Redis import at module import time. `InMemoryCaptureStream` is the test backend. `TurnCaptureWorker.process` filters, checks epoch, reserves dedup, marks appending, calls `SessionManager.ensure_session`, calls `session.batch_add_messages(ref, payloads)`, marks appended with `ov_message_ids`, then acks. On exceptions, do not ack and leave the row pending/appending. Do not call `commit_session` for `NONE` turns.

- [x] **Step 4: Run test to verify it passes**

Run the command from Step 2. Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add backend/packages/harness/agentkit/agents/memory/openviking/stream.py backend/packages/harness/agentkit/agents/memory/openviking/worker.py backend/tests/test_openviking_worker.py
git commit -m "feat(memory): add capture stream and single worker"
```

---

### Task 8: ProfileService and PersonalizationService

**Files:**
- Create: `backend/packages/harness/agentkit/agents/memory/openviking/profile.py`
- Test: `backend/tests/test_openviking_profile.py`

**Interfaces:**
- Produces: `ProfileService(session_factory, seed_path)` with `get_snapshot`, `patch`, `delete_field`, `clear`.
- Produces: `PersonalizationService(profile, identity_path, soul_path)` with `render(user_id)`.
- Profile uses etag `sha256(canonical_content)`; `patch` requires matching etag; list fields replace whole list.

- [x] **Step 1: Write failing profile tests**

```python
@pytest.mark.asyncio
async def test_profile_patch_and_etag_conflict(profile):
    first = await profile.get_snapshot("u1")
    second = await profile.patch("u1", {"communication.language": "zh-CN"}, etag=first.etag)
    assert second.fields["communication.language"] == "zh-CN"
    with pytest.raises(ProfileConflict):
        await profile.patch("u1", {"communication.language": "en-US"}, etag=first.etag)
```

- [x] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_openviking_profile.py -q`  
Expected: FAIL because `profile.py` does not exist.

- [x] **Step 3: Implement profile + personalization**

Seed from `config/personalization/profile.md` only when the row is missing; unknown frontmatter keys are dropped on write; `clear` writes the fixed empty template. `PersonalizationService` reads identity/soul files and returns a `PersonalizationView`; missing files produce empty sections, not crashes.

- [x] **Step 4: Run test to verify it passes**

Run the command from Step 2. Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add backend/packages/harness/agentkit/agents/memory/openviking/profile.py backend/tests/test_openviking_profile.py
git commit -m "feat(memory): add DB-backed profile and personalization"
```

---

### Task 9: Integration wiring and full-suite verification

**Files:**
- Modify: `backend/packages/harness/agentkit/agents/memory/openviking/__init__.py`
- Test: `backend/tests/test_openviking_integration.py`

- [x] **Step 1: Write an integration test that constructs the facade from config, a fake OpenViking client, SQLite, and in-memory stream**
- [x] **Step 2: Run it and verify it fails for the missing facade**
- [x] **Step 3: Implement `MemoryFacade` with `decide_capture`, `resolve_capture_context`, `publish_message`, `status`; no auto recall**
- [x] **Step 4: Run the new test plus the existing memory tests**
- [x] **Step 5: Commit**

---

### Task 10: Middleware capture path

**Files:**
- Create: `backend/packages/harness/agentkit/agents/middlewares/openviking_memory_middleware.py`
- Test: `backend/tests/test_openviking_middleware.py`

**Interfaces:**
- Produces: `OpenVikingMemoryMiddleware(facade, user_id=None, agent_name=None)`.
- `abefore_agent` classifies the latest real user message, stores `capture_decision` in `runtime.context`, and publishes a human message when CAPTURE.
- `aafter_model` publishes the newest visible AI message when the stored decision is CAPTURE.
- `awrap_tool_call` publishes a returned ToolMessage after the handler succeeds.

- [x] **Step 1: Write failing tests with a fake facade**
- [x] **Step 2: Run tests to verify they fail**
- [x] **Step 3: Implement the middleware**
- [x] **Step 4: Run tests to verify they pass**
- [x] **Step 5: Commit**

### Task 11: Profile HTTP API

**Files:**
- Create: `backend/app/gateway/routers/openviking_profile.py`
- Test: `backend/tests/test_openviking_profile_api.py`

**Interfaces:**
- Produces: `router` with GET/PATCH/DELETE `/memory/profile`.
- Uses `ProfileService` injected on `app.state.profile_service`; returns `{fields, etag, updated_at}`.
- ETag conflicts return 409 `profile_conflict`; unknown fields return 422.

- [x] **Step 1: Write failing route tests**
- [x] **Step 2: Run tests to verify they fail**
- [x] **Step 3: Implement the router**
- [x] **Step 4: Run tests to verify they pass**
- [x] **Step 5: Commit**

## Verification

- `.venv/Scripts/python.exe -m pytest backend/tests/test_openviking_contracts.py backend/tests/test_openviking_policy.py backend/tests/test_openviking_adapter.py backend/tests/test_openviking_schema.py backend/tests/test_openviking_store.py backend/tests/test_openviking_session.py backend/tests/test_openviking_worker.py backend/tests/test_openviking_profile.py -q`
- `.venv/Scripts/python.exe -m pytest backend/tests -q` (report pre-existing failures separately)
- No real Redis/OpenViking required for unit tests; use fakes.

## Out of Scope / Deferred

- Deleting canonical memory tables/code and migration 0009.
- Frontend profile/suggestions/onboarding pages.
- Real Redis Streams adapter and real `openviking-sdk` client factory (Task 9 can add a thin adapter; real service integration is Phase 0).
- Admin status/flush endpoints.

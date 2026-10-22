"""PII boundary tests: redaction, handles, tool restore, memory view."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.store.memory import InMemoryStore

from agentkit.agents.memory.backends.langmem.backend import LangMemBackend
from agentkit.agents.memory.manager import MemoryManager
from agentkit.agents.memory.types import MemoryScope
from agentkit.agents.middlewares.pii_middleware import (
    PII_VAULT_CONTEXT_KEY,
    PIIMiddleware,
    PIIOutputMiddleware,
)
from agentkit.config.app_config import AppConfig
from agentkit.config.memory_config import MemoryConfig
from agentkit.privacy import (
    PIIRedactor,
    PIIUnresolvedToken,
    PIIVault,
    TOKEN_RE,
    memory_view_messages,
    redact_raw_input,
)
from backend.app.gateway.run_models import RunCreateRequest
from backend.app.gateway.services import _persisted_kwargs, _pii_vault_id, _redact_run_input


def test_redactor_preserves_profile_name_and_restores_sensitive_values() -> None:
    vault = PIIVault()
    redactor = PIIRedactor(vault)
    raw = "以后叫我小王，邮箱 alice@example.com，电话 13812345678，key 是 sk-abcdefghijklmnopqrstuvwxyz"
    redacted = redactor.redact_text(raw)
    assert "小王" in redacted
    assert "alice@example.com" not in redacted
    assert "13812345678" not in redacted
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in redacted
    assert len(TOKEN_RE.findall(redacted)) == 3
    assert redactor.restore_payload(redacted) == raw


def test_memory_view_never_contains_handles_or_literal_pii() -> None:
    vault = PIIVault()
    redactor = PIIRedactor(vault)
    redacted = redactor.redact_text("email alice@example.com")
    memory_text = redactor.redact_for_memory(redacted)
    assert "alice@example.com" not in memory_text
    assert "[[PII:" not in memory_text
    assert memory_text.count("[REDACTED]") >= 1
    assert memory_view_messages([HumanMessage(content=redacted)], redactor)[0].content == memory_text


def test_handles_are_run_scoped_and_unresolved_tokens_fail_closed() -> None:
    vault = PIIVault()
    redactor = PIIRedactor(vault)
    token = redactor.redact_text("alice@example.com")
    other = PIIRedactor(PIIVault())
    with pytest.raises(PIIUnresolvedToken):
        other.restore_payload(token)


def test_raw_input_redaction_preserves_non_human_messages() -> None:
    vault = PIIVault()
    redactor = PIIRedactor(vault)
    raw = {
        "messages": [
            {"role": "user", "content": "email alice@example.com"},
            {"role": "assistant", "content": "email alice@example.com"},
        ]
    }
    reduced = redact_raw_input(raw, redactor)
    assert "alice@example.com" not in reduced["messages"][0]["content"]
    assert "alice@example.com" in reduced["messages"][1]["content"]


def test_start_run_redacts_input_before_persistence_and_normalization() -> None:
    config = AppConfig()
    body = RunCreateRequest(
        assistant_id="lead_agent",
        input={"messages": [{"role": "user", "content": "email alice@example.com"}]},
    )
    vault = _redact_run_input(body, config)
    assert vault is not None
    assert "alice@example.com" not in body.input["messages"][0]["content"]
    persisted = _persisted_kwargs(body)
    assert "alice@example.com" not in persisted["input"]["messages"][0]["content"]


def test_idempotent_replay_uses_stable_handle_namespace() -> None:
    config = AppConfig()
    payload = {"messages": [{"role": "user", "content": "email alice@example.com"}]}
    first = RunCreateRequest(assistant_id="lead_agent", input=payload)
    second = RunCreateRequest(assistant_id="lead_agent", input=payload)
    vault_id = _pii_vault_id("thread-1", "intent-1", payload)
    _redact_run_input(first, config, vault_id=vault_id)
    _redact_run_input(second, config, vault_id=vault_id)
    assert first.input == second.input
    assert "alice@example.com" not in str(first.input)
    changed = RunCreateRequest(
        assistant_id="lead_agent",
        input={"messages": [{"role": "user", "content": "email bob@example.com"}]},
    )
    changed_vault_id = _pii_vault_id("thread-1", "intent-1", changed.input)
    assert changed_vault_id != vault_id


def test_pii_middleware_redacts_genuine_user_but_not_hidden_profile() -> None:
    app_config = AppConfig(pii={"enabled": True})
    middleware = PIIMiddleware(app_config=app_config)
    context: dict = {}
    state = {
        "messages": [
            HumanMessage(content="email alice@example.com", id="u1"),
            HumanMessage(
                content="<user_profile>preferred_name: 小王</user_profile>",
                id="__user_profile__",
                additional_kwargs={"hide_from_ui": True},
            ),
        ]
    }
    out = middleware.before_agent(state, SimpleNamespace(context=context))
    assert out is not None
    messages = out["messages"]
    assert "alice@example.com" not in messages[0].content
    assert "小王" in messages[1].content
    assert "[[PII:" in messages[0].content
    assert PII_VAULT_CONTEXT_KEY in context


def test_tool_restore_applies_to_all_tools_and_scope_checked() -> None:
    vault = PIIVault()
    token = PIIRedactor(vault).redact_text("alice@example.com")
    context = {PII_VAULT_CONTEXT_KEY: vault}
    request = ToolCallRequest(
        tool_call={"name": "send_email", "args": {"to": token}, "id": "call-1"},
        tool=None,
        state={},
        runtime=SimpleNamespace(context=context),
    )
    middleware = PIIMiddleware(app_config=AppConfig(pii={"enabled": True}))
    seen: dict = {}

    def handler(req):
        seen.update(req.tool_call["args"])
        return ToolMessage(content="sent", tool_call_id="call-1", name="send_email")

    middleware.wrap_tool_call(request, handler)
    assert seen == {"to": "alice@example.com"}

    blocked = ToolCallRequest(
        tool_call={"name": "bash", "args": {"command": token}, "id": "call-2"},
        tool=None,
        state={},
        runtime=SimpleNamespace(context=context),
    )
    seen.clear()
    middleware.wrap_tool_call(blocked, handler)
    assert seen == {"command": "alice@example.com"}

    unknown = ToolCallRequest(
        tool_call={"name": "send_email", "args": {"to": "[[PII:EMAIL:deadbeef:9]]"}, "id": "call-3"},
        tool=None,
        state={},
        runtime=SimpleNamespace(context=context),
    )
    with pytest.raises(PIIUnresolvedToken):
        middleware.wrap_tool_call(unknown, handler)


def test_output_middleware_redacts_ai_content_and_tool_args() -> None:
    vault = PIIVault()
    token = PIIRedactor(vault).redact_text("alice@example.com")
    context = {PII_VAULT_CONTEXT_KEY: vault}
    middleware = PIIOutputMiddleware(app_config=AppConfig(pii={"redact_ai_output": True}))
    state = {
        "messages": [
            AIMessage(
                content="已发送到 alice@example.com",
                id="a1",
                tool_calls=[{"name": "send_email", "args": {"to": "alice@example.com"}, "id": "call-1"}],
            )
        ]
    }
    out = middleware.after_model(state, SimpleNamespace(context=context))
    assert out is not None
    message = out["messages"][0]
    assert "alice@example.com" not in message.content
    assert message.tool_calls[0]["args"]["to"] == token
    assert "alice@example.com" not in str(message.additional_kwargs)


def test_semantic_service_rejects_direct_unredacted_pii() -> None:
    from agentkit.agents.memory.resolver import MemoryResolver
    from agentkit.agents.memory.services.semantic import SemanticMemoryService
    from agentkit.agents.memory.types import MemoryProposal

    service = SemanticMemoryService(LangMemBackend(InMemoryStore()), MemoryResolver())
    with pytest.raises(ValueError, match="PII"):
        service.write(
            MemoryScope("alice"),
            [
                MemoryProposal(
                    id="pii",
                    kind="semantic",
                    payload={"content": "email alice@example.com", "source": "inferred"},
                )
            ],
        )


def test_memory_manager_ingests_only_memory_view() -> None:
    backend = LangMemBackend(InMemoryStore())
    seen: list[str] = []

    class Capture:
        def extract(self, _scope, messages):
            seen.extend(str(message.content) for message in messages)
            return []

    manager = MemoryManager(
        backend,
        ingestors=[Capture()],
        config=MemoryConfig(),
    )
    vault = PIIVault()
    token = PIIRedactor(vault).redact_text("alice@example.com")
    manager.ingest_now(MemoryScope("alice", thread_id="t1"), [HumanMessage(content=token)])
    assert seen
    assert "[[PII:" not in seen[0]
    assert "alice@example.com" not in seen[0]
    assert "[REDACTED]" in seen[0]

"""M10 section 2: profile, semantic facts, manager, injection, and maintenance."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.store.memory import InMemoryStore

from agentkit.agents.memory.backends.langmem.backend import LangMemBackend
from agentkit.agents.memory.context import ProfileContextBuilder
from agentkit.agents.memory.ingestors.langmem_semantic import LangMemSemanticIngestor
from agentkit.agents.memory.tools import make_memory_search_tool
from agentkit.agents.memory.maintenance.service import MemoryMaintenanceService
from agentkit.agents.memory.manager import MemoryManager, reset_memory_manager
from agentkit.agents.memory.resolver import MemoryResolver
from agentkit.agents.memory.services.profile import ProfileService
from agentkit.agents.memory.services.semantic import SemanticMemoryService
from agentkit.agents.memory.types import (
    MemoryConflictError,
    MemoryKind,
    MemoryProposal,
    MemoryQuery,
    MemoryScope,
    ProfileField,
    ProfilePatch,
    UserProfile,
    utc_now,
    validate_profile_value,
)
from agentkit.agents.middlewares.memory_middleware import (
    MEMORY_PROFILE_MESSAGE_ID,
    MemoryMiddleware,
)
from agentkit.config.memory_config import MemoryConfig, MemoryMaintenanceConfig


def test_memory_config_migrates_legacy_manager_class() -> None:
    from agentkit.config.memory_config import MemoryConfig

    migrated = MemoryConfig.model_validate({"manager_class": "noop", "mode": "tool"})
    assert migrated.backend_class == "noop"
    with pytest.raises(ValueError):
        MemoryConfig.model_validate({"manager_class": "noop", "backend_class": "langmem"})


@pytest.fixture(autouse=True)
def _reset_singletons():
    reset_memory_manager()
    yield
    reset_memory_manager()


@pytest.fixture
def stack():
    backend = LangMemBackend(InMemoryStore())
    resolver = MemoryResolver()
    profile = ProfileService(backend, resolver)
    semantic = SemanticMemoryService(backend, resolver)
    config = MemoryConfig(enabled=True)
    manager = MemoryManager(backend, [profile, semantic], config=config)
    return manager, profile, semantic, backend


def test_profile_schema_is_bounded() -> None:
    with pytest.raises(ValueError):
        validate_profile_value("notes", "free form")
    with pytest.raises(ValueError):
        validate_profile_value("identity.preferred_name", "x" * 65)
    with pytest.raises(ValueError):
        validate_profile_value("identity.timezone", "Mars/Olympus")
    with pytest.raises(ValueError):
        validate_profile_value("boundaries", ["x"] * 11)
    with pytest.raises(ValueError):
        validate_profile_value("communication.language", "sk-abcdefghijklmnopqrstuvwxyz")
    with pytest.raises(ValueError):
        ProfileField(value="x", source="inferred")
    assert validate_profile_value("communication.language", "zh-CN") == "zh-CN"
    assert validate_profile_value("boundaries", ["不要提薪资"]) == ["不要提薪资"]


def test_profile_cas_and_tombstone(stack) -> None:
    manager, profile, _semantic, backend = stack
    scope = MemoryScope("alice")
    first = profile.update(
        scope,
        ProfilePatch(fields={"communication.language": "zh-CN"}, source="user_manual"),
        expected_revision=0,
    )
    assert first.revision == 1
    with pytest.raises(MemoryConflictError):
        profile.update(
            scope,
            ProfilePatch(fields={"communication.verbosity": "concise"}, source="user_manual"),
            expected_revision=0,
        )
    profile.update(
        scope,
        ProfilePatch(fields={"communication.verbosity": "concise"}, source="user_manual"),
        expected_revision=1,
    )
    profile.clear(scope, field_path="communication.language")
    snapshot = profile.snapshot(scope)
    assert snapshot is not None
    assert snapshot.profile.communication.language is None
    record = backend.get_record(scope, MemoryKind.PROFILE, "profile")
    assert record is not None
    assert record.metadata["tombstones"]["communication.language"]["source"] == "user_manual"
    # A new explicit value after cleared_at is allowed and removes the tombstone.
    profile.update(
        scope,
        ProfilePatch(fields={"communication.language": "en-US"}, source="user_explicit_durable"),
        expected_revision=snapshot.revision,
    )
    assert profile.snapshot(scope).profile.communication.language.value == "en-US"
    assert "communication.language" not in backend.get_record(scope, MemoryKind.PROFILE, "profile").metadata["tombstones"]


def test_profile_authority_pins_manual_fields(stack) -> None:
    _manager, profile, _semantic, _backend = stack
    scope = MemoryScope("alice")
    profile.update(
        scope,
        ProfilePatch(fields={"communication.language": "zh-CN"}, source="user_manual"),
        expected_revision=0,
    )
    snapshot = profile.update(
        scope,
        ProfilePatch(fields={"communication.language": "en-US"}, source="user_confirmed"),
        expected_revision=1,
    )
    assert snapshot.profile.communication.language.value == "zh-CN"
    assert snapshot.profile.communication.language.pinned is True


def test_profile_renderer_omits_unknown_and_respects_whole_field_budget(stack) -> None:
    _manager, profile, _semantic, _backend = stack
    scope = MemoryScope("alice")
    profile.update(
        scope,
        ProfilePatch(
            fields={
                "identity.preferred_name": "小王",
                "communication.language": "zh-CN",
                "boundaries": ["不要提薪资"],
                "work.role": "backend engineer",
            },
            source="user_manual",
        ),
        expected_revision=0,
    )
    snapshot = profile.snapshot(scope)
    text = ProfileContextBuilder(max_tokens=800).build(snapshot).text
    assert "<user_profile>" in text
    assert "称呼: 小王" in text
    assert "边界: 不要提薪资" in text
    assert "source" not in text
    tight = ProfileContextBuilder(max_tokens=15).build(snapshot)
    assert tight.truncated is True
    assert "称呼: 小王" in tight.text
    assert "work.role" not in tight.text


def test_semantic_rejects_credentials(stack) -> None:
    _manager, _profile, semantic, _backend = stack
    with pytest.raises(ValueError, match="credentials"):
        semantic.write(
            MemoryScope("alice"),
            [
                MemoryProposal(
                    id="secret",
                    kind="semantic",
                    payload={"content": "token is sk-abcdefghijklmnopqrstuvwxyz", "source": "inferred"},
                    evidence="",
                )
            ],
        )


def test_semantic_dedupe_conflict_and_tenant_isolation(stack) -> None:
    manager, _profile, semantic, _backend = stack
    alice = MemoryScope("alice")
    bob = MemoryScope("bob")
    first = semantic.write(
        alice,
        [
            MemoryProposal(
                id="one",
                kind="semantic",
                payload={
                    "content": "用户喜欢 pytest",
                    "topic_key": "python.test",
                    "category": "preference",
                    "source": "inferred",
                },
                evidence="我们一般用 pytest",
            )
        ],
    )[0]
    duplicate = semantic.write(
        alice,
        [
            MemoryProposal(
                id="two",
                kind="semantic",
                payload={
                    "content": "用户喜欢 pytest",
                    "topic_key": "python.test",
                    "category": "preference",
                    "source": "inferred",
                },
                evidence="还是 pytest",
            )
        ],
    )[0]
    assert duplicate.id == first.id
    candidate = semantic.write(
        alice,
        [
            MemoryProposal(
                id="three",
                kind="semantic",
                payload={
                    "content": "用户喜欢 unittest",
                    "topic_key": "python.test",
                    "category": "preference",
                    "source": "inferred",
                },
                evidence="换成 unittest",
            )
        ],
    )[0]
    assert candidate.status == "conflicted"
    assert len(manager.list_conflicts(alice)) == 1
    assert semantic.search(bob, MemoryQuery(kinds=[MemoryKind.SEMANTIC])).items == []
    assert manager.search(bob, MemoryQuery(text="pytest")).items == []


def test_manager_clear_removes_profile_and_records(stack) -> None:
    manager, profile, semantic, backend = stack
    scope = MemoryScope("alice")
    profile.update(
        scope,
        ProfilePatch(fields={"communication.language": "zh-CN"}, source="user_manual"),
        expected_revision=0,
    )
    semantic.write(
        scope,
        [
            MemoryProposal(
                id="one",
                kind="semantic",
                payload={"content": "likes pytest", "source": "inferred"},
                evidence="pytest",
            )
        ],
    )
    manager.clear(scope)
    assert backend.get_record(scope, MemoryKind.PROFILE, "profile") is None
    assert manager.list_records(scope, kind=MemoryKind.SEMANTIC, include_history=True).items == []


def test_profile_injection_middleware_replaces_not_appends(stack) -> None:
    manager, profile, _semantic, _backend = stack
    scope = MemoryScope("alice")
    profile.update(
        scope,
        ProfilePatch(fields={"communication.language": "zh-CN"}, source="user_manual"),
        expected_revision=0,
    )
    middleware = MemoryMiddleware(manager=manager, config=manager.config)
    runtime = SimpleNamespace(context={"user_id": "alice", "thread_id": "t1"})
    state = {"messages": [HumanMessage(content="hi", id="user-1")]}
    first = middleware.before_agent(state, runtime)
    assert first is not None
    assert first["messages"][0].id == MEMORY_PROFILE_MESSAGE_ID
    assert first["messages"][0].additional_kwargs["hide_from_ui"] is True
    second = middleware.before_agent({"messages": first["messages"]}, runtime)
    assert second is None


def test_ingestor_rejects_model_claimed_confirmation_and_bad_evidence() -> None:
    class FakeModel:
        def invoke(self, _prompt):
            return AIMessage(
                content="""{
                  "profile_patches": [
                    {"field": "communication.language", "value": "zh-CN", "source": "user_confirmed", "evidence": "以后都用中文", "confidence": 0.9},
                    {"field": "communication.tone", "value": "direct", "source": "user_explicit_durable", "evidence": "以后都用中文", "confidence": 0.9}
                  ],
                  "facts": [
                    {"content": "用户偏好 pytest", "topic_key": "python.test", "category": "preference", "source": "inferred", "confidence": 0.8, "evidence": "我们一般用 pytest"}
                  ],
                  "instructions": []
                }"""
            )

    ingestor = LangMemSemanticIngestor(FakeModel())
    proposals = ingestor.extract(
        MemoryScope("alice"),
        [
            HumanMessage(content="以后都用中文，我们一般用 pytest", id="u1"),
            AIMessage(content="好的", id="a1"),
        ],
    )
    kinds = sorted(proposal.kind for proposal in proposals)
    assert kinds == ["profile", "semantic"]
    assert proposals[0].payload["source"] == "user_explicit_durable"
    assert proposals[1].payload["source"] == "inferred"


def test_ingest_queue_flushes_and_routes_proposals(stack) -> None:
    manager, _profile, semantic, _backend = stack
    manager.ingestors = [
        type(
            "FakeIngestor",
            (),
            {
                "extract": lambda _self, _scope, _messages: [
                    MemoryProposal(
                        id="fake",
                        kind="semantic",
                        payload={"content": "用户偏好 pytest", "source": "inferred"},
                        evidence="pytest",
                    )
                ]
            },
        )()
    ]
    scope = MemoryScope("alice", thread_id="t1")
    manager.ingest(
        scope,
        [HumanMessage(content="我们一般用 pytest", id="u1")],
        nowait=True,
    )
    assert manager.flush(timeout=2.0) is True
    page = semantic.search(scope, MemoryQuery(text="pytest"))
    assert [hit.record.content for hit in page.items] == ["用户偏好 pytest"]


def test_memory_search_tool_reads_identity_from_runtime(stack) -> None:
    import asyncio
    import json

    from langchain.tools import ToolRuntime

    manager, _profile, semantic, _backend = stack
    scope = MemoryScope("alice")
    semantic.write(
        scope,
        [
            MemoryProposal(
                id="tool-fact",
                kind="semantic",
                payload={"content": "用户偏好 pytest", "source": "inferred"},
                evidence="pytest",
            )
        ],
    )
    tool = make_memory_search_tool(manager)

    def runtime_for(context):
        return ToolRuntime(
            state={},
            context=context,
            config={},
            stream_writer=lambda _payload: None,
            tool_call_id=None,
            store=None,
        )

    async def call(context):
        return await tool.ainvoke(
            {"query": "pytest", "runtime": runtime_for(context)}
        )

    result = json.loads(asyncio.run(call({"user_id": "alice", "thread_id": "t1"})))
    assert result["items"][0]["content"] == "用户偏好 pytest"
    assert manager.get_stats(scope).search_calls == 1
    missing = json.loads(asyncio.run(call({})))
    assert "error" in missing


def test_low_vitality_archive_is_soft_and_explained() -> None:
    backend = LangMemBackend(InMemoryStore())
    resolver = MemoryResolver()
    semantic = SemanticMemoryService(backend, resolver)
    profile = ProfileService(backend, resolver)
    config = MemoryConfig(
        enabled=True,
        maintenance={
            "shadow": False,
            "high_watermark": 1,
            "target_count": 0,
            "min_vitality": 0.99,
            "min_age_days": 0,
            "max_eviction_ratio": 1.0,
            "max_evictions_per_run": 10,
            "min_search_calls_per_window": 0,
        },
    )
    manager = MemoryManager(backend, [profile, semantic], config=config)
    scope = MemoryScope("alice")
    record = semantic.write(
        scope,
        [
            MemoryProposal(
                id="low",
                kind="semantic",
                payload={"content": "old low confidence note", "source": "inferred", "confidence": 0.1},
                evidence="",
            )
        ],
    )[0]
    old = utc_now() - timedelta(days=90)
    backend.put_record(
        scope,
        record.model_copy(update={"created_at": old, "updated_at": old}),
        expected_revision=record.revision,
    )
    service = MemoryMaintenanceService(manager, config=config.maintenance)
    result = service.run_once(dry_run=False, force=True, scope=scope)
    assert result.archived == 1
    archived = backend.get_record(scope, "semantic", record.id)
    assert archived is not None
    assert archived.status == "archived"
    assert archived.metadata["archive_reason"] == "low_vitality"
    assert "vitality_at_archive" in archived.metadata


def test_vitality_archive_and_folding() -> None:
    backend = LangMemBackend(InMemoryStore())
    resolver = MemoryResolver()
    semantic = SemanticMemoryService(backend, resolver)
    profile = ProfileService(backend, resolver)
    config = MemoryConfig(
        enabled=True,
        maintenance={
            "shadow": False,
            "high_watermark": 2,
            "target_count": 1,
            "min_vitality": 0.0,
            "min_age_days": 0,
            "max_eviction_ratio": 1.0,
            "max_evictions_per_run": 10,
            "min_search_calls_per_window": 0,
            "folding": {
                "enabled": True,
                "min_facts": 3,
                "min_cluster_size": 3,
                "max_summary_token_ratio": 1.0,
                "similarity_threshold": 0.05,
            },
        },
    )
    manager = MemoryManager(backend, [profile, semantic], config=config)
    scope = MemoryScope("alice")
    old = utc_now() - timedelta(days=10)
    for index, text in enumerate(
        [
            "pytest is used for python tests",
            "pytest fixtures are preferred",
            "pytest runs the test suite",
            "pytest plugin supports coverage",
        ]
    ):
        record = semantic.write(
            scope,
            [
                MemoryProposal(
                    id=str(index),
                    kind="semantic",
                    payload={"content": text, "category": "preference", "source": "inferred"},
                    evidence="",
                )
            ],
        )[0]
        backend.put_record(
            scope,
            record.model_copy(update={"created_at": old, "updated_at": old}),
            expected_revision=record.revision,
        )
    service = MemoryMaintenanceService(
        manager,
        config=config.maintenance,
        summarizer=lambda _sources: "pytest is the preferred python test tool",
    )
    result = service.run_once(dry_run=False, force=True, scope=scope)
    assert result.folded == 1
    statuses = [
        hit.record.status
        for hit in manager.list_records(scope, kind="semantic", include_history=True, limit=100).items
    ]
    assert "folded" in statuses
    assert "active" in statuses

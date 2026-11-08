"""mcp_servers / tool_search blocks: startup rules and the pinned collection.

```yaml
mcp_servers:
  enabled: true
  encryption_key: $MYFLOW_MCP_SERVER_KEY
tool_search:
  vector_store: {collection: myflow_mcp_tools}
```

The mcp_servers block is off by default and cannot come up half-configured: an
enabled block without a *valid* Fernet key must fail at startup, not at the first
server write.  tool_search owns the derived Milvus collection, which is pinned so
tool documents never share the knowledge or skills collection.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from agentkit.config.app_config import AppConfig
from agentkit.config.mcp_servers_config import MCP_SERVER_KEY_ENV, McpServersConfig
from agentkit.config.tool_search_config import ToolSearchConfig

#: 32 zero bytes, url-safe base64: a well-formed Fernet key with no secret in it.
VALID_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
OTHER_KEY = "MTExMTExMTExMTExMTExMTExMTExMTExMTExMTExMTE="


@pytest.fixture(autouse=True)
def _no_ambient_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The developer's environment must not decide what these tests assert."""
    monkeypatch.delenv(MCP_SERVER_KEY_ENV, raising=False)


# --------------------------------------------------------------------------- #
# mcp_servers
# --------------------------------------------------------------------------- #


def test_enabled_requires_key() -> None:
    with pytest.raises(ValueError, match="encryption_key"):
        McpServersConfig(enabled=True).validate_for_startup()


def test_invalid_fernet_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="Fernet"):
        McpServersConfig(enabled=True, encryption_key="not-a-key").validate_for_startup()


def test_disabled_block_starts_without_a_key() -> None:
    config = McpServersConfig()

    assert config.enabled is False
    config.validate_for_startup()
    assert config.resolved_encryption_key() == ""


def test_key_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MCP_SERVER_KEY_ENV, VALID_KEY)
    config = McpServersConfig(enabled=True)

    config.validate_for_startup()

    assert config.resolved_encryption_key() == VALID_KEY


def test_configured_key_wins_over_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MCP_SERVER_KEY_ENV, OTHER_KEY)
    config = McpServersConfig(encryption_key=VALID_KEY)

    assert config.resolved_encryption_key() == VALID_KEY


def test_key_never_appears_in_repr_or_dump() -> None:
    config = McpServersConfig(encryption_key="correct-horse-battery-staple")

    assert isinstance(config.encryption_key, SecretStr)
    assert "correct-horse-battery-staple" not in repr(config)
    assert "correct-horse-battery-staple" not in str(config.model_dump())


def test_unknown_keys_are_rejected() -> None:
    with pytest.raises(ValidationError):
        McpServersConfig(encrytion_key=VALID_KEY)


def test_defaults_match_the_design() -> None:
    config = McpServersConfig()

    assert config.request_timeout_seconds == 30.0
    assert config.connect_timeout_seconds == 5.0
    assert config.max_servers_per_user == 20
    assert config.allow_private_networks is False
    assert config.allowed_schemes == ["https"]


def test_allowed_schemes_must_not_be_empty() -> None:
    with pytest.raises(ValueError, match="allowed_schemes"):
        McpServersConfig(enabled=True, encryption_key=VALID_KEY, allowed_schemes=[]).validate_for_startup()


def test_allowed_schemes_are_normalized_to_lowercase() -> None:
    config = McpServersConfig(allowed_schemes=[" HTTPS ", "https"])

    assert config.allowed_schemes == ["https"]


def test_allowed_schemes_reject_a_url() -> None:
    with pytest.raises(ValidationError, match="allowed_schemes"):
        McpServersConfig(allowed_schemes=["https://"])


# --------------------------------------------------------------------------- #
# tool_search
# --------------------------------------------------------------------------- #


def test_tool_collection_is_pinned() -> None:
    assert ToolSearchConfig().vector_store.collection == "myflow_mcp_tools"
    with pytest.raises(ValueError, match="myflow_mcp_tools"):
        ToolSearchConfig(vector_store={"collection": "wrong"})


def test_budgets_and_channels_are_carried_over() -> None:
    config = ToolSearchConfig()

    assert config.enabled is True
    assert config.candidate_k == 20
    assert config.top_k == 5
    assert config.rrf_k == 60
    assert config.max_schema_chars == 3000
    assert config.max_result_chars == 16000
    assert config.require_retrieved is True
    assert config.max_retrieved_tools == 5
    assert config.keyword.enabled is True
    assert config.keyword.analyzer == "chinese"
    assert config.keyword.bm25_k1 == 1.2
    assert config.keyword.bm25_b == 0.75
    assert config.embedding.enabled is True
    assert config.embedding.query_instruction == (
        "Given a task, retrieve the tool that can perform it"
    )
    assert config.rerank.enabled is False


def test_candidate_k_must_cover_top_k() -> None:
    with pytest.raises(ValueError, match="candidate_k"):
        ToolSearchConfig(candidate_k=1, top_k=5)


def test_at_least_one_recall_channel_must_stay_enabled() -> None:
    with pytest.raises(ValueError, match="recall channel"):
        ToolSearchConfig(keyword={"enabled": False}, embedding={"enabled": False})


# --------------------------------------------------------------------------- #
# AppConfig wiring
# --------------------------------------------------------------------------- #


def test_app_config_holds_both_blocks() -> None:
    config = AppConfig()

    assert isinstance(config.mcp_servers, McpServersConfig)
    assert isinstance(config.tool_search, ToolSearchConfig)
    assert config.mcp_servers.enabled is False
    assert config.tool_search.vector_store.collection == "myflow_mcp_tools"


def test_app_config_parses_the_deployed_yaml_shape() -> None:
    config = AppConfig.model_validate(
        {
            "mcp_servers": {
                "enabled": True,
                "encryption_key": VALID_KEY,
                "request_timeout_seconds": 45,
                "allowed_schemes": ["https"],
            },
            "tool_search": {"top_k": 3, "vector_store": {"collection": "myflow_mcp_tools"}},
        }
    )

    config.mcp_servers.validate_for_startup()
    assert config.mcp_servers.request_timeout_seconds == 45.0
    assert config.tool_search.top_k == 3


def test_app_config_fills_tool_search_from_the_shared_services() -> None:
    """tool_search is a consumer of the same Milvus/embedding/rerank services."""
    config = AppConfig.model_validate(
        {
            "embedding": {"base_url": "http://embedding:8000", "model": "bge-m3"},
            "vector_store": {"uri": "http://milvus:19530"},
            "tool_search": {"top_k": 4},
        }
    )

    assert config.tool_search.embedding.base_url == "http://embedding:8000"
    assert config.tool_search.embedding.model == "bge-m3"
    assert config.tool_search.vector_store.uri == "http://milvus:19530"
    assert config.tool_search.vector_store.collection == "myflow_mcp_tools"
    assert config.tool_search.top_k == 4


def test_app_config_rejects_a_non_pinned_tool_collection() -> None:
    with pytest.raises(ValidationError, match="myflow_mcp_tools"):
        AppConfig.model_validate({"tool_search": {"vector_store": {"collection": "tools"}}})

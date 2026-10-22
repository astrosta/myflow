"""The static prompt section is present only when the knowledge base is enabled."""

from __future__ import annotations

from agentkit.agents.leader_agent.prompt import apply_prompt_template
from agentkit.config import AppConfig


def _app(enabled: bool) -> AppConfig:
    return AppConfig.model_validate(
        {
            "models": [{"name": "m", "use": "langchain_deepseek:ChatDeepSeek"}],
            "knowledge_base": {
                "enabled": enabled,
                "parse": {"mode": "local", "local": {"endpoint": "http://mineru:8000"}},
                "embedding": {"base_url": "http://embedding:8000/v1"},
                "vector_store": {"uri": "http://milvus:19530"},
            },
        }
    )


def test_knowledge_section_explains_type_routing() -> None:
    prompt = apply_prompt_template(app_config=_app(True))
    assert "## 知识库" in prompt
    assert "knowledge_search" in prompt
    for value in ("technical", "operations", "faq", "security", "api"):
        assert value in prompt
    assert "引用" in prompt


def test_no_knowledge_section_when_disabled() -> None:
    prompt = apply_prompt_template(app_config=_app(False))
    assert "## 知识库" not in prompt

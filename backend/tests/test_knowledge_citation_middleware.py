"""Lead-side citation validation: only retrieved and cited sources are persisted."""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agentkit.agents.middlewares.delegation_ledger import RUN_START_MESSAGE_IDS_KEY
from agentkit.agents.middlewares.knowledge_citation_middleware import (
    KnowledgeCitationMiddleware,
)
from agentkit.knowledge.citations import CITATION_KEY, citations_for_hits
from agentkit.knowledge.models import SearchHit


def _citation(
    *,
    chunk_id: str = "d1:0",
    document_id: str = "d1",
    content: str = "安装步骤",
) -> dict:
    return citations_for_hits([
        SearchHit(
            chunk_id=chunk_id,
            document_id=document_id,
            filename="manual.md",
            section="安装",
            content=content,
            score=0.9,
            knowledge_base_id="b1",
            knowledge_base_name="产品文档",
            knowledge_base_type="technical",
            page=2,
        )
    ])[0]


def _state(answer: str, *, citations: list[dict] | None = None, tool_calls: list | None = None) -> dict:
    return {
        "messages": [
            HumanMessage(content="怎么安装"),
            ToolMessage(
                content="任务成功。\n结果：安装前先备份。",
                tool_call_id="call-1",
                name="task",
                additional_kwargs={CITATION_KEY: citations or []},
            ),
            AIMessage(
                id="ai-1",
                content=answer,
                tool_calls=tool_calls or [],
                additional_kwargs={"keep": "me"},
            ),
        ]
    }


def test_after_model_attaches_only_cited_and_retrieved_sources() -> None:
    citation = _citation()
    middleware = KnowledgeCitationMiddleware()

    update = middleware.after_model(
        _state(f"先备份，再升级 [{citation['citation_id']}]，伪造 [kdeadbeef]。", citations=[citation]),
        SimpleNamespace(context={}),
    )

    [message] = update["messages"]
    assert message.id == "ai-1"
    assert message.additional_kwargs["keep"] == "me"
    assert message.additional_kwargs[CITATION_KEY] == [citation]


def test_after_model_leaves_a_tool_calling_message_alone() -> None:
    citation = _citation()
    middleware = KnowledgeCitationMiddleware()

    assert middleware.after_model(
        _state(
            f"先查一下 [{citation['citation_id']}]",
            citations=[citation],
            tool_calls=[{"id": "call-2", "name": "task", "args": {}}],
        ),
        SimpleNamespace(context={}),
    ) is None


def test_after_model_returns_none_when_the_answer_does_not_cite_anything() -> None:
    middleware = KnowledgeCitationMiddleware()

    assert middleware.after_model(
        _state("没有标记的答案", citations=[_citation()]),
        SimpleNamespace(context={}),
    ) is None


def test_previous_turn_citations_are_not_reused_after_the_run_boundary() -> None:
    old = _citation()
    new = _citation(chunk_id="d2:0", document_id="d2", content="新来源")
    middleware = KnowledgeCitationMiddleware()
    state = {
        "messages": [
            HumanMessage(id="old-human", content="上一轮问题"),
            ToolMessage(
                id="old-tool",
                content="上一轮结果",
                tool_call_id="old-call",
                name="task",
                additional_kwargs={CITATION_KEY: [old]},
            ),
            HumanMessage(id="new-human", content="这一轮问题"),
            ToolMessage(
                id="new-tool",
                content="这一轮结果",
                tool_call_id="new-call",
                name="task",
                additional_kwargs={CITATION_KEY: [new]},
            ),
            AIMessage(
                id="ai-1",
                content=f"旧 [{old['citation_id']}] 新 [{new['citation_id']}]",
            ),
        ]
    }

    update = middleware.after_model(
        state,
        SimpleNamespace(
            context={RUN_START_MESSAGE_IDS_KEY: ["old-human", "old-tool"]}
        ),
    )

    [message] = update["messages"]
    assert message.additional_kwargs[CITATION_KEY] == [new]

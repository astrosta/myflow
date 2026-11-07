"""M7 §3.6：SKILL.md 注入只改 request，不进 checkpoint；同一 slash 只读一次盘。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from agentkit.agents.middlewares.message_utils import is_real_user_message
from agentkit.agents.middlewares.provenance import ContentKind, read_provenance
from agentkit.agents.middlewares.skill_activation_middleware import (
    SkillActivationMiddleware,
    SKILL_ACTIVATION_AUDIT_TAG,
)
from agentkit.agents.middlewares.skill_tool_policy_middleware import SkillToolPolicyMiddleware
from agentkit.agents.thread_state import ThreadState
from agentkit.runtime.secret_context import SLASH_SKILL_SOURCE_CONTEXT_KEY, read_secret_context
from agentkit.skills.catalog import SkillSnapshot
from agentkit.skills.domain import SkillScope
from agentkit.skills.types import Skill, SkillCategory
from fakes.chat_model import FakeChatModel
from fakes.skill_tree import skills_config, write_skill

BODY_MARKER = "BODY-MARKER-42"


class _RecordingChatModel(FakeChatModel):
    seen: list = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen = [*self.seen, list(messages)]
        return super()._generate(messages, stop, run_manager, **kwargs)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen = [*self.seen, list(messages)]
        return await super()._agenerate(messages, stop, run_manager, **kwargs)


def _middlewares(tmp_path):
    config = skills_config(tmp_path)
    activation = SkillActivationMiddleware(skills_config=config, user_id="u1")
    policy = SkillToolPolicyMiddleware(
        skills_config=config, owner_token=activation.owner_token, user_id="u1"
    )
    return activation, policy


def _request(messages, context) -> ModelRequest:
    return ModelRequest(
        model=object(), messages=list(messages), state={}, runtime=SimpleNamespace(context=context)
    )


def test_the_injection_is_request_only_and_never_lands_in_the_checkpoint(tmp_path):
    write_skill(tmp_path, "public", "pdf-processing", body=f"# 说明\n{BODY_MARKER}\n")
    activation, policy = _middlewares(tmp_path)
    model = _RecordingChatModel(reply="done")
    graph = create_agent(
        model=model,
        tools=[],
        middleware=[activation, policy],
        state_schema=ThreadState,
        checkpointer=MemorySaver(),
    )
    config = {"configurable": {"thread_id": "t1"}}

    graph.invoke(
        {"messages": [HumanMessage(content="/pdf-processing 转成文本", id="m1")]},
        config=config,
        context={"user_id": "u1", "thread_id": "t1"},
    )

    # 模型确实看到了 SKILL.md 正文（隐藏消息，带 provenance）
    injected = [
        message
        for message in model.seen[0]
        if BODY_MARKER.split("-")[0] in str(message.content) and read_provenance(message)
    ]
    assert injected, "the model never saw the skill body"
    assert injected[0].additional_kwargs["hide_from_ui"] is True
    assert read_provenance(injected[0]).content_kind == ContentKind.SKILL_BODY
    assert not is_real_user_message(injected[0])

    # checkpoint 里没有正文：只有那条用户消息和模型的回复
    messages = graph.get_state(config).values["messages"]
    assert all(BODY_MARKER not in str(message.content) for message in messages)
    assert [type(message) for message in messages] == [HumanMessage, AIMessage]


def test_one_slash_message_is_read_from_disk_once_per_run(tmp_path, monkeypatch):
    write_skill(tmp_path, "public", "pdf-processing", body=BODY_MARKER)
    activation, _ = _middlewares(tmp_path)
    reads: list[str] = []

    from agentkit.agents.middlewares import skill_activation_middleware as module

    def counting_validate(skill):
        reads.append(str(skill.skill_file))
        return skill.skill_file

    monkeypatch.setattr(module, "validate_skill_file_path", counting_validate)
    context: dict = {"user_id": "u1"}
    message = HumanMessage(content="/pdf-processing go", id="m1")
    injected_counts: list[int] = []

    def handler(request: ModelRequest):
        injected_counts.append(
            sum(1 for item in request.messages if BODY_MARKER in str(item.content))
        )
        return "ok"

    activation.wrap_model_call(_request([message], context), handler)
    activation.wrap_model_call(_request([message], context), handler)  # 工具循环里的第 2 次模型调用

    assert reads == [str(tmp_path / "skills" / "public" / "pdf-processing" / "SKILL.md")]
    assert injected_counts == [1, 0]  # 第二次没有再注入一份正文


def test_the_slash_source_is_recorded_for_the_policy(tmp_path):
    write_skill(tmp_path, "public", "pdf-processing")
    activation, _ = _middlewares(tmp_path)
    context: dict = {"user_id": "u1"}

    activation.wrap_model_call(
        _request([HumanMessage(content="/pdf-processing go", id="m1")], context), lambda request: "ok"
    )

    assert read_secret_context(
        context, SLASH_SKILL_SOURCE_CONTEXT_KEY, token=activation.owner_token
    ) == "/mnt/skills/public/pdf-processing/SKILL.md"


def test_no_slash_no_injection(tmp_path):
    write_skill(tmp_path, "public", "pdf-processing")
    activation, _ = _middlewares(tmp_path)
    seen: dict = {}

    def handler(request: ModelRequest):
        seen["request"] = request
        return "ok"

    original = _request([HumanMessage(content="你好", id="m1")], {"user_id": "u1"})
    activation.wrap_model_call(original, handler)

    assert seen["request"] is original


def test_a_reserved_command_or_unknown_skill_is_not_activated(tmp_path):
    write_skill(tmp_path, "public", "pdf-processing")
    activation, _ = _middlewares(tmp_path)
    seen: dict = {}

    def handler(request: ModelRequest):
        seen["request"] = request
        return "ok"

    for text in ("/new", "/ghost", " /pdf-processing"):
        original = _request([HumanMessage(content=text, id="m1")], {"user_id": "u1"})
        activation.wrap_model_call(original, handler)
        assert seen["request"] is original, text


def test_the_audit_tag_is_logged_on_activation(tmp_path, caplog):
    write_skill(tmp_path, "public", "pdf-processing")
    activation, _ = _middlewares(tmp_path)

    with caplog.at_level("INFO", logger="agentkit.agents.middlewares.skill_activation_middleware"):
        activation.wrap_model_call(
            _request([HumanMessage(content="/pdf-processing go", id="m1")], {"user_id": "u1"}),
            lambda request: "ok",
        )

    assert any(SKILL_ACTIVATION_AUDIT_TAG in record.getMessage() for record in caplog.records)


def test_a_builtin_slash_injects_the_body_and_records_the_builtin_source(tmp_path):
    write_skill(tmp_path, "builtin", "demo", body=BODY_MARKER)
    config = skills_config(tmp_path, builtin={"path": str(tmp_path / "skills" / "builtin")})
    activation = SkillActivationMiddleware(skills_config=config, user_id="u1")
    context: dict = {"user_id": "u1"}
    seen: dict = {}

    def handler(request: ModelRequest):
        seen["request"] = request
        return "ok"

    activation.wrap_model_call(
        _request([HumanMessage(content="/demo go", id="m1")], context), handler
    )

    assert any(BODY_MARKER in str(message.content) for message in seen["request"].messages)
    assert read_secret_context(
        context, SLASH_SKILL_SOURCE_CONTEXT_KEY, token=activation.owner_token
    ) == "/mnt/skills/builtin/demo/SKILL.md"


def test_a_snapshot_activation_never_rescans_the_filesystem(tmp_path, monkeypatch):
    """The catalog generation is authoritative: slash reads its Skill.skill_file."""
    from agentkit.agents.middlewares import skill_activation_middleware as module

    host_file = (
        tmp_path
        / "cache"
        / "commits"
        / "private"
        / "private"
        / "secret"
        / "SKILL.md"
    )
    host_file.parent.mkdir(parents=True)
    host_file.write_text(
        f"---\nname: secret\ndescription: private skill\n---\n{BODY_MARKER}\n",
        encoding="utf-8",
    )
    skill = Skill(
        name="secret",
        description="private skill",
        license=None,
        skill_dir=host_file.parent,
        skill_file=host_file,
        relative_path=Path("secret"),
        category=SkillCategory.CUSTOM,
        enabled=True,
        scope=SkillScope.PRIVATE,
        owner_user_id="u1",
        skill_id="s-secret",
    )
    snapshot = SkillSnapshot("e14", {"u1": (skill,)})
    activation = SkillActivationMiddleware(
        skills_config=skills_config(tmp_path), user_id="u1", snapshot=snapshot
    )

    def forbidden_scan(*args, **kwargs):
        raise AssertionError("the filesystem scanner must not run for a snapshot")

    monkeypatch.setattr(module, "validate_skill_file_path", lambda candidate: candidate.skill_file)
    monkeypatch.setattr("agentkit.skills.storage.load_skills", forbidden_scan)
    context: dict = {"user_id": "u1"}
    seen: dict = {}

    def handler(request: ModelRequest):
        seen["request"] = request
        return "ok"

    activation.wrap_model_call(
        _request([HumanMessage(content="/secret go", id="m1")], context), handler
    )

    assert any(BODY_MARKER in str(message.content) for message in seen["request"].messages)
    assert read_secret_context(
        context, SLASH_SKILL_SOURCE_CONTEXT_KEY, token=activation.owner_token
    ) == "/mnt/skills/private/secret/SKILL.md"
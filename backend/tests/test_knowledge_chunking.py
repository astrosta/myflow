"""Type-specific chunking: structure boundaries, token limits, and cleaning."""

from __future__ import annotations

import pytest

from agentkit.knowledge.chunking import chunk_document
from agentkit.knowledge.config import KnowledgeBaseConfig
from agentkit.knowledge.text import clean_text
from agentkit.knowledge.tokenizer import ApproximateTokenCounter
from agentkit.knowledge.types import KnowledgeBaseType


class CharCounter:
    """One character = one token, so test sizes are exact and readable."""

    name = "char"

    def count(self, text: str) -> int:
        return len(text)

    def tail(self, text: str, tokens: int) -> str:
        return text[-tokens:] if tokens > 0 else ""

    def split(self, text: str, max_tokens: int) -> list[str]:
        return [text[i : i + max_tokens] for i in range(0, len(text), max_tokens)]


def _config(**chunking: object) -> KnowledgeBaseConfig:
    configured = {
        "types": {
            "technical": {"chunk_size_tokens": 120, "chunk_overlap_tokens": 20},
            "operations": {"chunk_size_tokens": 120, "chunk_overlap_tokens": 10},
            "faq": {"chunk_size_tokens": 100, "chunk_overlap_tokens": 0},
            "security": {"chunk_size_tokens": 100, "chunk_overlap_tokens": 10},
            "api": {"chunk_size_tokens": 120, "chunk_overlap_tokens": 0},
            "legal": {"chunk_size_tokens": 100, "chunk_overlap_tokens": 10},
        }
    }
    configured.update(chunking)
    return KnowledgeBaseConfig.model_validate({"chunking": configured})


def test_clean_text_removes_images_scripts_and_controls() -> None:
    raw = (
        chr(0xFEFF)
        + "# 标题\r\n\r\n![](images/x.png) 正文"
        + chr(0x200B)
        + chr(0)
        + "\r\n<script>alert(1)</script>\n\n\n\n结尾"
    )
    cleaned = clean_text(raw)
    assert "images/x.png" not in cleaned
    assert "alert(1)" not in cleaned
    assert chr(0x200B) not in cleaned
    assert chr(0) not in cleaned
    assert "\n\n\n" not in cleaned
    assert "标题" in cleaned and "正文" in cleaned and "结尾" in cleaned


def test_technical_chunks_follow_headings_and_respect_tokens() -> None:
    text = (
        "# 产品手册\n\n"
        "## 安装\n\n"
        "先安装依赖，再启动服务。这里是一段足够长的说明，用来触发分片。"
        "重复内容用于测试连续性。重复内容用于测试连续性。\n\n"
        "## 配置\n\n"
        "配置文件位于 etc/app.yaml，修改后重启生效。"
    )
    chunks = chunk_document(
        text, KnowledgeBaseType.TECHNICAL, _config(), token_counter=CharCounter()
    )
    assert len(chunks) >= 2
    assert any("安装" in chunk.section for chunk in chunks)
    assert any("配置" in chunk.section for chunk in chunks)
    assert all(len(chunk.content) <= 120 for chunk in chunks if len(chunk.content) > 0)
    assert chunks[0].index == 0 and [c.index for c in chunks] == list(range(len(chunks)))


def test_operations_keep_commands_and_warnings_with_the_step() -> None:
    fence = chr(96) * 3
    text = (
        "# 发布流程\n\n"
        "## 回滚\n\n"
        "步骤一：停止服务\n\n"
        f"{fence}bash\nsystemctl stop myflow\n{fence}\n\n"
        "注意：确认所有请求已排空后再执行。\n\n"
        "步骤二：启动旧版本\n\n"
        f"{fence}bash\nsystemctl start myflow-old\n{fence}\n"
    )
    chunks = chunk_document(
        text, KnowledgeBaseType.OPERATIONS, _config(), token_counter=CharCounter()
    )
    joined = "\n".join(chunk.content for chunk in chunks)
    assert "systemctl stop myflow" in joined
    assert "确认所有请求已排空" in joined
    assert "systemctl start myflow-old" in joined
    assert all(chunk.content.count(fence) % 2 == 0 for chunk in chunks)


def test_faq_keeps_each_question_with_its_answer() -> None:
    text = (
        "## 常见问题\n\n"
        "### Q: 如何重置密码？\n\n"
        "A: 在设置页点击重置密码，然后检查邮箱。\n\n"
        "### Q: 支持离线部署吗？\n\n"
        "A: 支持，需准备本地模型和数据库。\n"
    )
    chunks = chunk_document(
        text, KnowledgeBaseType.FAQ, _config(), token_counter=CharCounter()
    )
    assert len(chunks) == 2
    assert "如何重置密码" in chunks[0].content
    assert "重置密码" in chunks[0].content
    assert "A:" in chunks[0].content
    assert "支持离线部署" in chunks[1].content
    assert chunks[0].section != chunks[1].section
    # Only the question is embedded; the answer remains in the returned chunk.
    assert chunks[0].embedding_text == "如何重置密码？"
    assert chunks[1].embedding_text == "支持离线部署吗？"


def test_security_chunks_do_not_merge_clauses() -> None:
    text = (
        "# 安全要求\n\n"
        "第1条 访问控制：所有账号必须启用多因素认证。审计人员应保留日志至少六个月。\n\n"
        "第2条 数据保护：生产数据不得复制到个人设备。违规行为应上报安全委员会。\n"
    )
    chunks = chunk_document(
        text, KnowledgeBaseType.SECURITY, _config(), token_counter=CharCounter()
    )
    assert len(chunks) == 2
    assert "第1条" in chunks[0].section
    assert "第2条" in chunks[1].section
    assert "第1条" not in chunks[1].content and "第2条" not in chunks[0].content


def test_legal_chunks_split_by_article_and_keep_the_chapter_path() -> None:
    text = (
        "# 某某管理办法\n\n"
        "## 第一章 总则\n\n"
        "第一条 为了规范某某管理，制定本办法。\n\n"
        "第二条 在境内从事某某活动的单位，适用本办法。\n\n"
        "## 第三章 监督管理\n\n"
        "第十二条之一 监督检查人员应当出示证件。\n\n"
        "Article 20 Any operator shall keep records for three years.\n"
    )
    chunks = chunk_document(
        text, KnowledgeBaseType.LEGAL, _config(), token_counter=CharCounter()
    )

    assert len(chunks) == 4
    # 条号进 section：引用要能落到“第几条”
    assert "第一章 总则" in chunks[0].section and "第一条" in chunks[0].section
    assert "第二条" in chunks[1].section
    assert "第三章 监督管理" in chunks[2].section and "第十二条之一" in chunks[2].section
    assert "Article 20" in chunks[3].section
    # 不跨条合并
    assert "第二条" not in chunks[0].content
    assert "第一条" not in chunks[1].content


@pytest.mark.parametrize(
    "decorate",
    [
        lambda text: f"**{text}**",
        lambda text: f"> {text}",
        lambda text: f"- {text}",
        lambda text: "\u200b" + text,
    ],
)
def test_legal_section_keeps_article_number_but_not_decorated_body(decorate) -> None:
    first = (
        "第一条 为保证我分行个金客户经理制的顺利实施，有效调动个金客户经理的积极性，"
        "促进个金业务快速、稳定地发展，根据总行要求，特制定本办法。"
    )
    second = "第二条 本办法适用于我分行个金客户经理。"
    text = (
        "# 个金客户经理管理考核暂行办法\n\n"
        "## 第一章 总 则\n\n"
        f"{decorate(first)}\n\n"
        f"{decorate(second)}\n"
    )

    chunks = chunk_document(
        text, KnowledgeBaseType.LEGAL, _config(), token_counter=CharCounter()
    )

    sections = {chunk.section for chunk in chunks}
    # MinerU 把条文加粗/加引用时，section 仍只能保留条号，不能带整段正文。
    assert sections == {
        "个金客户经理管理考核暂行办法 / 第一章 总 则 / 第一条",
        "个金客户经理管理考核暂行办法 / 第一章 总 则 / 第二条",
    }
    assert any("第一条 为保证" in chunk.content for chunk in chunks)


def test_legal_article_heading_is_collapsed_to_its_number() -> None:
    first = "第一条 为保证业务顺利实施，制定本办法。"
    text = (
        "# 某某办法\n\n"
        "## 第一章 总 则\n\n"
        f"### {first}\n\n"
        f"{first}\n\n"
        "第二条 本办法适用于某某机构。\n"
    )

    chunks = chunk_document(
        text, KnowledgeBaseType.LEGAL, _config(), token_counter=CharCounter()
    )

    assert all("为保证" not in chunk.section for chunk in chunks)
    assert not any("/ 第一条 / 第二条" in chunk.section for chunk in chunks)
    assert {chunk.section for chunk in chunks} == {
        "某某办法 / 第一章 总 则 / 第一条",
        "某某办法 / 第一章 总 则 / 第二条",
    }


def test_legal_without_clause_markers_falls_back_to_plain_packing() -> None:
    text = "这是一段没有条号的法规说明，仍然要能被切出来而不是丢掉。"
    chunks = chunk_document(
        text, KnowledgeBaseType.LEGAL, _config(), token_counter=CharCounter()
    )
    assert len(chunks) == 1
    assert "没有条号" in chunks[0].content


def test_api_reference_keeps_endpoint_symbols_separate() -> None:
    text = (
        "# API 参考\n\n"
        "## GET /users\n\n"
        "返回用户列表。参数：limit。示例：GET /users?limit=10\n\n"
        "## POST /users\n\n"
        "创建用户。参数：name、email。响应：201。示例：POST /users\n"
    )
    chunks = chunk_document(
        text, KnowledgeBaseType.API, _config(), token_counter=CharCounter()
    )
    assert len(chunks) == 2
    assert "GET /users" in chunks[0].section
    assert "POST /users" in chunks[1].section
    assert "GET /users" not in chunks[1].content
    assert "POST /users" not in chunks[0].content


def test_oversized_operations_step_uses_parent_child_without_overlap() -> None:
    marker = "A" * 250
    text = "# 发布流程\n\n## 回滚\n\n步骤一：执行回滚命令\n\n" + marker

    chunks = chunk_document(
        text,
        KnowledgeBaseType.OPERATIONS,
        _config(),
        token_counter=CharCounter(),
        document_id="doc-1",
    )

    assert len(chunks) > 1
    assert {chunk.parent_id for chunk in chunks} == {"doc-1:p0"}
    assert all(chunk.parent_content for chunk in chunks)
    assert chunks[0].parent_content.startswith("发布流程 / 回滚")
    # Config says overlap=10, but operations must force zero.
    assert sum(chunk.content.count("A") for chunk in chunks) == len(marker)
    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))


def test_oversized_legal_article_uses_parent_child() -> None:
    body = "第一条 " + "法律条文内容。" * 30
    text = "# 某某办法\n\n## 第一章 总则\n\n" + body

    chunks = chunk_document(
        text,
        KnowledgeBaseType.LEGAL,
        _config(),
        token_counter=CharCounter(),
        document_id="doc-2",
    )

    assert len(chunks) > 1
    assert {chunk.parent_id for chunk in chunks} == {"doc-2:p0"}
    assert "第一条" in chunks[0].parent_content
    assert all("第一章 总则" in chunk.section for chunk in chunks)


def test_short_operations_step_has_no_parent() -> None:
    text = "# 发布流程\n\n## 回滚\n\n步骤一：停止服务。\n"

    chunks = chunk_document(
        text,
        KnowledgeBaseType.OPERATIONS,
        _config(),
        token_counter=CharCounter(),
        document_id="doc-3",
    )

    assert len(chunks) == 1
    assert chunks[0].parent_id is None
    assert chunks[0].parent_content == ""


def test_fallback_chunks_when_structure_is_absent() -> None:
    text = "这是一段没有标题、没有问答、没有条款的普通文本。" * 30
    chunks = chunk_document(
        text, KnowledgeBaseType.TECHNICAL, _config(), token_counter=CharCounter()
    )
    assert chunks
    assert all(len(chunk.content) <= 120 for chunk in chunks)


def test_approximate_counter_is_deterministic_and_bounded() -> None:
    counter = ApproximateTokenCounter()
    text = "Hello world，这是一个测试。"
    first = counter.count(text)
    assert first > 0
    assert counter.count(text) == first
    assert counter.tail(text, 3)
    assert counter.count(counter.tail(text, 3)) <= 3

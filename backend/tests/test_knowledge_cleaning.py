"""Deterministic business-noise cleaning for parsed knowledge documents."""

from __future__ import annotations

from agentkit.knowledge.text import clean_document_text


def test_removes_boilerplate_sections_but_keeps_body_and_code_fences() -> None:
    fence = chr(96) * 3
    raw = (
        "# 产品手册\n\n"
        "## 前言\n\n这是前言，不应进入知识库。\n\n"
        "## 版权声明\n\nCopyright 2026 Example Corp.\n\n"
        "## 免责声明\n\n本文不构成任何承诺。\n\n"
        "## 修订记录\n\n| 版本 | 日期 |\n| --- | --- |\n| v1 | 2026-01-01 |\n\n"
        "## 安装\n\n执行安装命令。\n\n"
        f"{fence}bash\n# 前言\necho ok\n{fence}\n\n"
        "## 正文\n\n这是真正的正文内容，必须保留。\n"
    )

    cleaned = clean_document_text(raw)

    for noise in ("这是前言", "版权声明", "免责声明", "修订记录", "Copyright 2026"):
        assert noise not in cleaned
    assert "## 前言" not in cleaned
    assert "安装" in cleaned
    assert "真正的正文" in cleaned
    assert "# 前言" in cleaned  # fenced code is not a section heading
    assert "echo ok" in cleaned


def test_removes_toc_block_only_when_it_has_three_or_more_entries() -> None:
    raw = (
        "正文开头。\n\n"
        "第一章 安装 ........ 1\n"
        "第二章 配置 ........ 3\n"
        "第三章 回滚 ........ 5\n\n"
        "正文结尾。\n\n"
        "只有一行条目 ........ 7\n"
    )

    cleaned = clean_document_text(raw)

    assert "第一章" not in cleaned
    assert "第二章" not in cleaned
    assert "第三章" not in cleaned
    assert "正文开头" in cleaned and "正文结尾" in cleaned
    assert "只有一行条目" in cleaned


def test_removes_exact_duplicate_blocks_but_not_short_repeated_lines() -> None:
    duplicated = "这是重复出现的业务说明，长度足够触发去重，且不应该进入向量索引；这段说明在文档末尾再次原样出现，属于模板噪声。"
    raw = (
        f"第一段正文。\n\n{duplicated}\n\n"
        "第二段正文。\n\n"
        f"{duplicated}\n\n"
        "步骤一\n\n步骤一\n"
    )

    cleaned = clean_document_text(raw)

    assert cleaned.count("这是重复出现的业务说明") == 1
    assert cleaned.count("步骤一") == 2
    assert "第一段正文" in cleaned and "第二段正文" in cleaned

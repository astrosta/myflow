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


def test_html_tables_become_markdown_tables_before_tags_are_stripped() -> None:
    raw = (
        "<p>接口业务数据说明见表 1。</p>"
        "<table><thead><tr><th>参数名称</th><th>类型</th><th>是否必填</th><th>说明</th></tr></thead>"
        "<tbody><tr><td>idA</td><td>String</td><td>是</td><td>客户端的证书</td></tr>"
        "<tr><td>randomA</td><td>String</td><td>是</td><td>客户端生成的 16 位随机数<br>用于防重放</td></tr></tbody></table>"
    )

    cleaned = clean_document_text(raw)

    assert "| 参数名称 | 类型 | 是否必填 | 说明 |" in cleaned
    assert "| --- | --- | --- | --- |" in cleaned
    assert "| idA | String | 是 | 客户端的证书 |" in cleaned
    assert "| randomA | String | 是 | 客户端生成的 16 位随机数 用于防重放 |" in cleaned
    assert "<table>" not in cleaned and "<td>" not in cleaned


def test_html_table_colspan_keeps_the_grid_rectangular() -> None:
    raw = (
        "<table><tr><th colspan=\"2\">合并表头</th></tr>"
        "<tr><td>左</td><td>右</td></tr></table>"
    )

    cleaned = clean_document_text(raw)

    assert "| 合并表头 |  |" in cleaned
    assert "| --- | --- |" in cleaned
    assert "| 左 | 右 |" in cleaned


def test_html_table_rowspan_keeps_columns_aligned_and_repeats_data() -> None:
    raw = (
        "<table><thead>"
        '<tr><th rowspan="2">类别</th><th rowspan="2">行员级别</th>'
        '<th rowspan="2">考核分值</th><th colspan="3">准入标准</th></tr>'
        "<tr><th>储蓄业务</th><th>个贷业务</th><th>卡业务</th></tr>"
        "</thead><tbody>"
        '<tr><td rowspan="2">客户经理助理</td><td>5</td><td>90</td>'
        '<td rowspan="2">300万</td><td></td><td rowspan="2">500 张</td></tr>'
        "<tr><td>4</td><td>95</td></tr>"
        "</tbody></table>"
    )

    cleaned = clean_document_text(raw)

    assert "| 类别 | 行员级别 | 考核分值 | 准入标准 |  |  |" in cleaned
    assert "|  |  |  | 储蓄业务 | 个贷业务 | 卡业务 |" in cleaned
    assert "| 客户经理助理 | 5 | 90 | 300万 |  | 500 张 |" in cleaned
    # The rowspan must put 4/95 back under 行员级别/考核分值 instead of
    # letting right-padding shift them into the 类别/行员级别 columns.
    assert "| 客户经理助理 | 4 | 95 | 300万 |  | 500 张 |" in cleaned
    assert "\n| 4 | 95 |" not in cleaned


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


def test_toc_heading_does_not_swallow_lower_level_chapter_body() -> None:
    """MinerU often emits `# 目录` then `## 第一章` for both TOC and body."""
    raw = (
        "# XX管理办法\n\n"
        "# 目录\n\n"
        "## 第一章 总则\n\n"
        "## 第二章 职责\n\n"
        "## 第一章 总则\n\n"
        "第一条 为规范管理，制定本办法。\n\n"
        "## 第二章 职责\n\n"
        "第三条 主管部门负责解释。\n"
    )

    cleaned = clean_document_text(raw)

    assert "# 目录" not in cleaned
    assert "第一章 总则" in cleaned
    assert "第一条 为规范管理" in cleaned
    assert "第二章 职责" in cleaned
    assert "第三条 主管部门负责解释" in cleaned


def test_toc_heading_keeps_chapter_when_body_starts_under_first_chapter() -> None:
    raw = (
        "# 目录\n\n"
        "## 第一章 总则\n\n"
        "第一条 为保证数据安全，制定本规定。\n\n"
        "## 第二章 管理\n\n"
        "第二条 本规定适用于全体员工。\n"
    )

    cleaned = clean_document_text(raw)

    assert "# 目录" not in cleaned
    assert "第一章 总则" in cleaned
    assert "第一条 为保证数据安全" in cleaned
    assert "第二章 管理" in cleaned
    assert "第二条 本规定适用于全体员工" in cleaned


def test_strips_markdown_toc_heading_with_plain_chapter_lines() -> None:
    raw = (
        "# 中华人民共和国网络安全法\n\n"
        "# 目  录\n\n"
        "第一章 总  则\n"
        "第二章 网络安全支持与促进\n"
        "第三章 网络运行安全\n"
        "第四章 网络信息安全\n"
        "第五章 监测预警与应急处置\n"
        "第六章 法律责任\n"
        "第七章 附  则\n\n"
        "# 第一章 总  则\n\n"
        "第一条 为了保障网络安全，制定本法。\n"
    )

    cleaned = clean_document_text(raw)

    assert "# 目  录" not in cleaned
    assert "第二章 网络安全支持与促进" not in cleaned
    assert "# 第一章 总  则" in cleaned
    assert "第一条 为了保障网络安全" in cleaned


def test_strips_law_toc_with_plain_chapter_lines_no_page_numbers() -> None:
    """Chinese statutes list chapters under 目录 without dotted leaders."""
    raw = (
        "中华人民共和国网络安全法\n\n"
        "（2016年11月7日第十二届全国人民代表大会常务委员会第二十四次会议通过）\n\n"
        "目  录\n"
        "第一章 总  则\n"
        "第二章 网络安全支持与促进\n"
        "第三章 网络运行安全\n"
        "  第一节 一般规定\n"
        "  第二节 关键信息基础设施的运行安全\n"
        "第四章 网络信息安全\n"
        "第五章 监测预警与应急处置\n"
        "第六章 法律责任\n"
        "第七章 附  则\n\n"
        "第一章 总  则\n\n"
        "第一条 为了保障网络安全，维护网络空间主权和国家安全，制定本法。\n"
    )

    cleaned = clean_document_text(raw)

    assert "目  录" not in cleaned and "目录" not in cleaned.replace(" ", "")
    assert "第二章 网络安全支持与促进" not in cleaned
    assert "第二节 关键信息基础设施的运行安全" not in cleaned
    assert "中华人民共和国网络安全法" in cleaned
    assert "第一章 总" in cleaned
    assert "第一条 为了保障网络安全" in cleaned


def test_strips_compact_inline_law_toc_paragraph() -> None:
    raw = (
        "中华人民共和国网络安全法\n\n"
        "第一章 总则 第二章 网络安全支持与促进 第三章 网络运行安全 "
        "第一节 一般规定 第二节 关键信息基础设施的运行安全 "
        "第四章 网络信息安全 第五章 监测预警与应急处置 "
        "第六章 法律责任 第七章 附则\n\n"
        "第一章 总 则\n\n"
        "第一条 为了保障网络安全，制定本法。\n"
    )

    cleaned = clean_document_text(raw)

    assert "第二章 网络安全支持与促进" not in cleaned
    assert "第七章 附则" not in cleaned
    assert "第一章 总" in cleaned
    assert "第一条 为了保障网络安全" in cleaned


def test_keeps_real_chapter_body_sequence_without_toc() -> None:
    raw = (
        "第一章 总则\n\n"
        "第一条 为规范管理，制定本办法。\n\n"
        "第二章 职责\n\n"
        "第三条 主管部门负责解释。\n"
    )

    cleaned = clean_document_text(raw)

    assert "第一章 总则" in cleaned
    assert "第一条 为规范管理" in cleaned
    assert "第二章 职责" in cleaned
    assert "第三条 主管部门负责解释" in cleaned

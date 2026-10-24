"""Knowledge base types and the chunking strategy each one selects."""

from __future__ import annotations

from enum import StrEnum


class KnowledgeBaseType(StrEnum):
    TECHNICAL = "technical"
    OPERATIONS = "operations"
    FAQ = "faq"
    SECURITY = "security"
    API = "api"
    LEGAL = "legal"


KNOWLEDGE_BASE_TYPES: tuple[KnowledgeBaseType, ...] = tuple(KnowledgeBaseType)

TYPE_LABELS: dict[KnowledgeBaseType, str] = {
    KnowledgeBaseType.TECHNICAL: "技术文档",
    KnowledgeBaseType.OPERATIONS: "运维手册",
    KnowledgeBaseType.FAQ: "FAQ 类文档",
    KnowledgeBaseType.SECURITY: "安全合规文档",
    KnowledgeBaseType.API: "API 参考文档",
    KnowledgeBaseType.LEGAL: "法律法规",
}

TYPE_DESCRIPTIONS: dict[KnowledgeBaseType, str] = {
    KnowledgeBaseType.TECHNICAL: "产品、架构、安装与配置说明；按标题层级和章节切分。",
    KnowledgeBaseType.OPERATIONS: "部署、巡检、故障排查和回滚；按操作步骤、命令块切分。",
    KnowledgeBaseType.FAQ: "常见问题与回答；一问一答为一个分片。",
    KnowledgeBaseType.SECURITY: "安全要求、合规条款与审计证据；按条款切分。",
    KnowledgeBaseType.API: "接口、函数和类参考；按接口/符号切分，参数与示例跟随接口。",
    KnowledgeBaseType.LEGAL: "法律、行政法规、部门规章与标准条文；按条/款切分，条款号写入章节。",
}

TYPE_STRATEGIES: dict[KnowledgeBaseType, str] = {
    KnowledgeBaseType.TECHNICAL: "heading_hierarchy",
    KnowledgeBaseType.OPERATIONS: "procedure",
    KnowledgeBaseType.FAQ: "qa_pair",
    KnowledgeBaseType.SECURITY: "clause",
    KnowledgeBaseType.API: "api_reference",
    KnowledgeBaseType.LEGAL: "legal_clause",
}

TYPE_STRATEGY_OPTIONS = tuple(TYPE_STRATEGIES.values())


def parse_type(value: str | KnowledgeBaseType) -> KnowledgeBaseType:
    return value if isinstance(value, KnowledgeBaseType) else KnowledgeBaseType(value)

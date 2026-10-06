from collections.abc import Mapping

ORIGINAL_USER_CONTENT_KEY = "original_user_content"
INJECTED_USER_MESSAGE_ID_SUFFIX = "__user"


def message_content_to_text(content: object) -> str:
    """str 原样返回；list 里收集 str 和 {"text": str}，用换行拼；其它走 str()。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return str(content)


def get_original_user_content_text(content: object, additional_kwargs: Mapping | None) -> str:
    """additional_kwargs[original_user_content] 是 str 就用它，否则 message_content_to_text(content)。"""
    if isinstance(additional_kwargs, Mapping):
        existing = additional_kwargs.get(ORIGINAL_USER_CONTENT_KEY)
        if isinstance(existing, str):
            return existing
    return message_content_to_text(content)


def record_original_user_content(kwargs: dict, content: object) -> None:
    """first-writer-wins。已有 str 则不动；已有非 str 则覆盖成当前 content 的文本；没有键则写入。"""
    existing = kwargs.get(ORIGINAL_USER_CONTENT_KEY)
    if isinstance(existing, str):
        return
    kwargs[ORIGINAL_USER_CONTENT_KEY] = message_content_to_text(content)

"""Deterministic gate for pure social turns.

The policy is intentionally pure: it performs no I/O and is the only place
that owns the skip/capture vocabulary. The middleware, publisher and worker
consume its result instead of re-implementing token matching.
"""

from __future__ import annotations

import re
import unicodedata

from agentkit.agents.memory.openviking.contracts import SignalKind, TurnDecision

_GREETINGS = frozenset(
    {
        "你好",
        "您好",
        "嗨",
        "哈喽",
        "hello",
        "hi",
        "早上好",
        "早安",
        "下午好",
        "晚上好",
        "晚安",
        "在吗",
        "在么",
        "最近怎么样",
        "最近如何",
    }
)
_FAREWELLS = frozenset({"再见", "拜拜", "bye", "goodbye"})
_THANKS = frozenset({"谢谢", "多谢", "感谢", "辛苦了", "thanks", "thank you"})
_TASK_CONTEXT_TOKENS = frozenset(
    {
        "好的",
        "好",
        "收到",
        "明白",
        "明白了",
        "可以",
        "行",
        "没问题",
        "继续",
        "完成",
        "完成了",
        "好了",
        "记住",
        "记一下",
        "对",
        "是的",
        "ok",
        "okay",
    }
)
_SOCIAL_TOKENS = _GREETINGS | _FAREWELLS | _THANKS
_EMOJI = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U00002700-\U000027BF"
    "\U00002600-\U000026FF"
    "\U0000FE0F"
    "\U0000200D"
    "]+"
)
_PUNCTUATION = re.compile(r"^[\s\W_]+$", re.UNICODE)
_EDGE_PUNCTUATION = re.compile(r"^[\s\W_]+|[\s\W_]+$", re.UNICODE)


def _normalized(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "").strip().casefold()
    return _EDGE_PUNCTUATION.sub("", value).strip()


def _is_social(value: str) -> bool:
    return value in _SOCIAL_TOKENS


def _is_farewell(value: str) -> bool:
    return value in _FAREWELLS


def _is_pure_emoji_or_punctuation(value: str) -> bool:
    if not value:
        return True
    without_emoji = _EMOJI.sub("", value)
    return not without_emoji.strip() or bool(_PUNCTUATION.fullmatch(without_emoji))


def validate_extra_skip_tokens(tokens: tuple[str, ...]) -> None:
    extra = tuple(token.strip().casefold() for token in tokens if token.strip())
    bad_extra = sorted(set(extra) & _TASK_CONTEXT_TOKENS)
    if bad_extra:
        raise ValueError(
            "extra_skip_tokens cannot contain task-context tokens: "
            + ", ".join(bad_extra)
        )


def classify_turn(
    user_text: str,
    *,
    has_attachments: bool = False,
    extra_skip_tokens: tuple[str, ...] = (),
    skip_greetings: bool = True,
    skip_emoji_only: bool = True,
) -> TurnDecision:
    """Classify one real user message as CAPTURE or SKIP.

    Unknown text always defaults to CAPTURE. Attachments always force CAPTURE
    because they may carry the actual task even when the text is social.
    """

    validate_extra_skip_tokens(extra_skip_tokens)
    extra = tuple(token.strip().casefold() for token in extra_skip_tokens if token.strip())

    if has_attachments:
        return TurnDecision("capture", reason="attachments")

    raw = unicodedata.normalize("NFKC", user_text or "").strip()
    value = _normalized(user_text)
    if not value:
        if _is_pure_emoji_or_punctuation(raw):
            if not skip_emoji_only:
                return TurnDecision("capture", reason="emoji_skip_disabled")
            return TurnDecision("skip", reason="emoji_or_punctuation")
        return TurnDecision("skip", reason="empty")

    if _is_social(value):
        if not skip_greetings:
            return TurnDecision("capture", reason="social_skip_disabled")
        signals: tuple[SignalKind, ...] = ("SESSION_END",) if _is_farewell(value) else ()
        return TurnDecision("skip", signals=signals, reason="social")

    if _is_pure_emoji_or_punctuation(value):
        if not skip_emoji_only:
            return TurnDecision("capture", reason="emoji_skip_disabled")
        return TurnDecision("skip", reason="emoji_or_punctuation")

    if value in extra:
        return TurnDecision("skip", reason="extra_skip_token")

    if value in _TASK_CONTEXT_TOKENS:
        return TurnDecision("capture", reason="task_context")

    return TurnDecision("capture", reason="default_capture")

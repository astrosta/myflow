"""Deterministic commit-signal detection for one captured turn.

The detector only decides which commit signals a turn carries. It never
commits a session, never creates a task id, and never writes memory. The
worker owns commit timing; the middleware uses the detector to put signals on
the turn-end control event.

A signal that is already present (for example EXPLICIT_REMEMBER set by the
built-in memory_remember tool) wins. Detection is skipped entirely in that
case so the tool and the heuristic path cannot disagree.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from dataclasses import replace
from typing import Any, Iterable, Mapping

from agentkit.agents.memory.openviking.contracts import (
    SignalContext,
    SignalKind,
    TurnDecision,
)

logger = logging.getLogger(__name__)

#: Worker executes a commit after appending the current turn for these signals.
COMMIT_SIGNALS: frozenset[str] = frozenset(
    {
        "TASK_COMPLETE",
        "FAILURE_RECOVERY",
        "USER_CORRECTION",
        "EXPLICIT_REMEMBER",
        "SESSION_END",
    }
)

COMMIT_REASONS: dict[str, str] = {
    "EXPLICIT_REMEMBER": "explicit_remember",
    "USER_CORRECTION": "user_correction",
    "FAILURE_RECOVERY": "failure_recovery",
    "TASK_COMPLETE": "task_complete",
    "TASK_BOUNDARY": "task_boundary",
    "SESSION_END": "session_end",
}

#: Ordered from highest to lowest execution priority (design section 6.5).
_SIGNAL_PRIORITY: dict[str, int] = {
    "EXPLICIT_REMEMBER": 0,
    "USER_CORRECTION": 1,
    "FAILURE_RECOVERY": 2,
    "TASK_COMPLETE": 3,
    "TASK_BOUNDARY": 4,
    "SESSION_END": 5,
    "NONE": 6,
}
_VALID_SIGNALS = frozenset(_SIGNAL_PRIORITY) - {"NONE"}

_EXPLICIT_REMEMBER = re.compile(
    "|".join(
        (
            r"记住",
            r"记一下",
            r"记下来",
            r"记录下来",
            r"保存到记忆",
            r"保存下来",
            r"别忘记",
            r"不要忘记",
            r"以后都",
            r"remember\s+(?:this|that|it)",
            r"don'?t\s+forget\s+(?:this|that|it)",
            r"keep\s+this\s+in\s+mind",
        )
    ),
    re.IGNORECASE,
)
_EXPLICIT_REMEMBER_NEGATIVE = re.compile(
    r"(?:如果|假如|假设|万一)[^。！？\n]{0,24}记住"
    r"|(?:还记得|记住[^。！？\n]{0,8}吗|记住了没|有没有记住)[^。！？\n]{0,8}[？?]?\s*$",
    re.IGNORECASE,
)

_USER_CORRECTION = re.compile(
    "|".join(
        (
            r"不对",
            r"不是这样",
            r"不是那样",
            r"说错",
            r"讲错",
            r"弄错",
            r"搞错",
            r"纠正",
            r"更正",
            r"应该(?:是|用)",
            r"改(?:成|为)",
            r"之前(?:那个|说的|说的那个)不算",
            r"刚才说错",
            r"actually",
            r"I\s+meant",
        )
    ),
    re.IGNORECASE,
)
_CORRECTION_QUESTION = re.compile(
    r"(?:不是这样|不对|应该(?:是|用))[^。！？\n]{0,12}吗[？?]?\s*$",
    re.IGNORECASE,
)

_TASK_COMPLETE = re.compile(
    "|".join(
        (
            r"完成(?:了|啦)",
            r"做完了",
            r"搞定(?:了)?",
            r"结束了",
            r"结束吧",
            r"任务完成",
            r"\bdone\b",
            r"\bfinished\b",
            r"\btask\s+complete\b",
        )
    ),
    re.IGNORECASE,
)

_SESSION_END = re.compile(
    r"^(?:再见|拜拜|bye|goodbye|下次见|回头见)[!！。.，,\s]*$", re.IGNORECASE
)

_FAILED_RUN_STATUSES = frozenset({"error", "aborted", "timeout"})


class CommitSignalDetector:
    """Rule-first detector with an optional bounded light-model fallback.

    The model is intentionally generic: any object with an async ainvoke that
    returns text or a message-like object with content works. A missing, slow,
    or malformed model never fails a turn; it degrades to no signals.
    """

    def __init__(
        self,
        *,
        model: Any | None = None,
        timeout_seconds: float = 1.5,
    ) -> None:
        self._model = model
        self._timeout_seconds = max(0.0, float(timeout_seconds))

    async def detect(self, ctx: SignalContext) -> tuple[SignalKind, ...]:
        existing = normalize_signals(ctx.signals)
        if existing:
            # The built-in explicit-memory tool already decided this turn.
            # Never add a second opinion on top of an explicit signal.
            return existing

        detected = self._rule_signals(ctx)
        if not detected and self._model is not None and _worth_model_call(ctx):
            detected = await self._model_signals(ctx)
        return order_signals(detected)

    async def detect_decision(
        self,
        decision: TurnDecision,
        *,
        user_text: str = "",
        assistant_text: str = "",
        has_tool_error: bool = False,
        has_recovery: bool = False,
        run_status: str | None = None,
        recent_turns: Iterable[str] = (),
    ) -> TurnDecision:
        """Return the decision with detected signals, preserving existing ones."""

        signals = await self.detect(
            SignalContext(
                user_text=user_text,
                assistant_text=assistant_text,
                signals=decision.signals,
                has_tool_error=has_tool_error,
                has_recovery=has_recovery,
                run_status=run_status,  # type: ignore[arg-type]
                recent_turns=tuple(recent_turns),
            )
        )
        if signals == decision.signals:
            return decision
        return replace(decision, signals=signals)

    def _rule_signals(self, ctx: SignalContext) -> list[SignalKind]:
        text = _normalized(ctx.user_text)
        signals: list[SignalKind] = []

        if _explicit_remember(text):
            signals.append("EXPLICIT_REMEMBER")
        if _user_correction(text):
            signals.append("USER_CORRECTION")
        if _failure_recovery(ctx):
            signals.append("FAILURE_RECOVERY")
        if _task_complete(text):
            signals.append("TASK_COMPLETE")
        if _SESSION_END.fullmatch(text):
            signals.append("SESSION_END")
        return signals

    async def _model_signals(self, ctx: SignalContext) -> list[SignalKind]:
        prompt = (
            "You classify one user turn for long-term-memory commit signals. "
            "Return JSON only: {\"signals\": [...], \"evidence_quote\": \"...\"}. "
            "Allowed signals: EXPLICIT_REMEMBER, USER_CORRECTION, FAILURE_RECOVERY, "
            "TASK_COMPLETE, TASK_BOUNDARY, SESSION_END. Use an empty list when unsure. "
            "evidence_quote must be copied verbatim from the user message or assistant reply.\n"
            f"User message: {ctx.user_text}\n"
            f"Assistant reply: {ctx.assistant_text}\n"
            f"Tool error: {ctx.has_tool_error}; recovery: {ctx.has_recovery}; "
            f"run status: {ctx.run_status or ''}"
        )
        try:
            response = await asyncio.wait_for(
                self._model.ainvoke(prompt),
                timeout=self._timeout_seconds,
            )
        except Exception:
            logger.debug("commit signal model call failed; degrading to no signals", exc_info=True)
            return []
        payload = _payload_from_response(response)
        if not isinstance(payload, Mapping):
            return []
        raw_signals = payload.get("signals")
        if not isinstance(raw_signals, list):
            return []
        evidence = (ctx.user_text or "") + "\n" + (ctx.assistant_text or "")
        quote = payload.get("evidence_quote")
        if isinstance(quote, str) and quote.strip() and quote not in evidence:
            return []
        return [
            signal
            for signal in raw_signals
            if isinstance(signal, str) and signal in _VALID_SIGNALS
        ]


def normalize_signals(signals: Iterable[str] | None) -> tuple[SignalKind, ...]:
    """Drop NONE/unknown values and de-duplicate while preserving order."""

    result: list[SignalKind] = []
    for signal in signals or ():
        if not isinstance(signal, str):
            continue
        value = signal.upper()
        if value in _VALID_SIGNALS and value not in result:
            result.append(value)  # type: ignore[arg-type]
    return tuple(result)


def order_signals(signals: Iterable[str] | None) -> tuple[SignalKind, ...]:
    normalized = normalize_signals(signals)
    return tuple(sorted(normalized, key=lambda signal: _SIGNAL_PRIORITY.get(signal, 99)))


def needs_commit(signals: Iterable[str] | None) -> bool:
    return any(signal in COMMIT_SIGNALS for signal in normalize_signals(signals))


def commit_reason(signals: Iterable[str] | None) -> str | None:
    """Return the highest-priority commit reason, or None."""

    for signal in order_signals(signals):
        if signal in COMMIT_SIGNALS:
            return COMMIT_REASONS.get(signal, signal.casefold())
    return None


def _normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").strip()


def _explicit_remember(text: str) -> bool:
    if not text or _EXPLICIT_REMEMBER_NEGATIVE.search(text):
        return False
    return bool(_EXPLICIT_REMEMBER.search(text))


def _user_correction(text: str) -> bool:
    if not text or _CORRECTION_QUESTION.search(text):
        return False
    return bool(_USER_CORRECTION.search(text))


def _task_complete(text: str) -> bool:
    return bool(text and _TASK_COMPLETE.search(text))


def _failure_recovery(ctx: SignalContext) -> bool:
    if (ctx.run_status or "").casefold() in _FAILED_RUN_STATUSES:
        return True
    return bool(ctx.has_tool_error and ctx.has_recovery)


def _worth_model_call(ctx: SignalContext) -> bool:
    return bool((ctx.user_text or "").strip() or ctx.assistant_text or ctx.has_tool_error)


def _payload_from_response(response: Any) -> Mapping[str, Any] | None:
    content = getattr(response, "content", response)
    if isinstance(content, list):
        content = "".join(
            block if isinstance(block, str) else str(block.get("text", ""))
            for block in content
            if isinstance(block, str) or isinstance(block, dict)
        )
    if not isinstance(content, str):
        return None
    text = content.strip()
    fence = "\x60\x60\x60"
    if text.startswith(fence):
        text = text[len(fence) :].lstrip()
        if text.casefold().startswith("json"):
            text = text[4:].lstrip()
        if text.endswith(fence):
            text = text[: -len(fence)].rstrip()
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


__all__ = [
    "COMMIT_REASONS",
    "COMMIT_SIGNALS",
    "CommitSignalDetector",
    "commit_reason",
    "needs_commit",
    "normalize_signals",
    "order_signals",
]

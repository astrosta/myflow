"""Read-only retrieval facade for OpenViking memories."""

from __future__ import annotations

from typing import Any, Literal, Protocol
from urllib.parse import unquote, urlsplit

from agentkit.agents.memory.openviking.config import DEFAULT_MEMORY_TYPES
from agentkit.agents.memory.openviking.content import MemoryContentEnvelope
from agentkit.agents.memory.openviking.contracts import MemoryHit


class RecallClient(Protocol):
    async def find(self, **kwargs: Any) -> dict[str, Any]: ...

    async def read(self, uri: str) -> str: ...

    async def overview(self, uri: str) -> str: ...


class RecallService:
    def __init__(self, client: RecallClient) -> None:
        self._client = client
        self._envelope = MemoryContentEnvelope()

    async def search(
        self,
        *,
        ov_user: str,
        query: str,
        memory_types: list[str],
        limit: int = 8,
        since: str | None = None,
        until: str | None = None,
        time_field: str | None = None,
    ) -> list[MemoryHit]:
        types = list(dict.fromkeys(memory_types))
        unknown = set(types) - set(DEFAULT_MEMORY_TYPES)
        if unknown:
            raise ValueError(f"invalid memory_type: {sorted(unknown)[0]}")
        if not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        if (since or until or time_field) and types != ["events"]:
            raise ValueError("time filters require memory_types == ['events']")

        options: dict[str, Any] = {
            "context_type": ["memory"],
            "read_content": False,
        }
        if types == ["events"]:
            if since:
                options["since"] = since
            if until:
                options["until"] = until
            if time_field:
                options["time_field"] = time_field

        targets = [f"viking://user/{ov_user}/memories/{item}" for item in types]
        result = await self._client.find(
            query=query,
            target_uri=targets,
            limit=limit,
            options=options,
        )
        hits: list[MemoryHit] = []
        for item in result.get("memories") or []:
            if not isinstance(item, dict):
                continue
            uri = str(item.get("uri") or "")
            memory_type = _memory_type(uri)
            if memory_type is None:
                continue
            hits.append(
                MemoryHit(
                    uri=uri,
                    memory_type=memory_type,  # type: ignore[arg-type]
                    abstract=self._envelope.wrap(
                        source=uri,
                        text=str(item.get("abstract") or ""),
                        max_chars=2_000,
                    ),
                    score=_float(item.get("score")),
                    level=_level(item.get("level")),
                    tags=tuple(str(tag) for tag in (item.get("tags") or [])),
                )
            )
        return hits

    async def read(self, ov_user: str, uri: str, *, max_chars: int = 8_000) -> str:
        target = self._validate_uri(ov_user, uri)
        content = await self._client.read(target)
        return self._envelope.wrap(source=target, text=content, max_chars=max_chars)

    async def overview(self, ov_user: str, uri: str) -> str:
        target = self._validate_uri(ov_user, uri)
        content = await self._client.overview(target)
        return self._envelope.wrap(source=target, text=content, max_chars=4_000)

    @staticmethod
    def _validate_uri(ov_user: str, uri: str) -> str:
        if not uri or uri.startswith("viking://~") or "~" in uri:
            raise ValueError("home alias is not allowed")
        parsed = urlsplit(uri)
        if parsed.scheme != "viking" or parsed.netloc != "user":
            raise ValueError("absolute user URI required")
        decoded = unquote(parsed.path)
        segments = [segment for segment in decoded.split("/") if segment]
        if any(segment == ".." for segment in segments):
            raise ValueError("path traversal is not allowed")
        if len(segments) < 3 or segments[0] != ov_user or segments[1] != "memories":
            raise ValueError("URI is outside the current user memory namespace")
        denied = {"peers", "sessions", "privacy", "profile.md", "identity.md", "soul.md"}
        if any(segment in denied for segment in segments[2:]):
            raise ValueError("managed namespace is not readable")
        return uri


def _memory_type(uri: str) -> str | None:
    segments = [segment for segment in urlsplit(uri).path.split("/") if segment]
    try:
        index = segments.index("memories")
    except ValueError:
        return None
    if len(segments) <= index + 1:
        return None
    value = segments[index + 1]
    return value if value in DEFAULT_MEMORY_TYPES else None


def _level(value: Any) -> Literal["L0", "L1", "L2"] | None:
    return {0: "L0", 1: "L1", 2: "L2"}.get(value)


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = ["RecallClient", "RecallService"]

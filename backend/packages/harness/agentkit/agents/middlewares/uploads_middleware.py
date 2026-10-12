"""Inject this turn's upload manifest. File bytes stay on disk."""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import NotRequired

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage
from langchain_core.runnables.config import run_in_executor
from langgraph.runtime import Runtime

from agentkit.agents.middlewares.input_sanitization_middleware import neutralize_untrusted_tags
from agentkit.agents.middlewares.thread_data_middleware import resolve_thread_id
from agentkit.config.paths import Paths, get_paths
from agentkit.sandbox.runtime import runtime_user_bucket
from agentkit.utils.messages import record_original_user_content

logger = logging.getLogger(__name__)

_MAX_FILES_PER_CONTEXT_SECTION = 10
_STAGING_PREFIX = ".upload-"
_STAGING_SUFFIX = ".part"
_ATX_HEADING = re.compile(r"^ {0,3}#{1,6}(?:[ \t]+(.*))?$")
_OUTLINE_LIMIT = 50
_PREVIEW_LINES = 5


def _is_staging_file(filename: str) -> bool:
    return filename.startswith(_STAGING_PREFIX) and filename.endswith(_STAGING_SUFFIX)


def _coerce_file_size(value: object) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        size = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0
    return size if size >= 0 else 0


def extract_outline_for_file(file_path: Path) -> tuple[list[dict], list[str]]:
    """Headings from a Markdown upload, or a short preview when it has none."""
    try:
        if file_path.suffix.lower() != ".md" or not file_path.is_file():
            return [], []
        outline: list[dict] = []
        preview: list[str] = []
        with file_path.open(encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, 1):
                match = _ATX_HEADING.fullmatch(line.rstrip("\r\n"))
                if match:
                    title = (match.group(1) or "").strip().strip("#").strip()
                    if title:
                        outline.append({"title": title[:200], "line": lineno})
                        if len(outline) >= _OUTLINE_LIMIT:
                            outline.append({"truncated": True})
                            break
                    continue
                stripped = line.strip()
                if stripped and len(preview) < _PREVIEW_LINES:
                    preview.append(stripped[:2000])
        if outline:
            return outline, []
        return [], preview
    except Exception:
        logger.debug("Failed to read an upload outline", exc_info=True)
        return [], []


def _extension_label(file: Mapping) -> str:
    extension = str(file.get("extension") or Path(str(file.get("filename") or "")).suffix)
    return neutralize_untrusted_tags(extension) or "(no extension)"


def _omitted_types(files: list[dict]) -> str:
    counts = Counter(_extension_label(file) for file in files)
    return neutralize_untrusted_tags(", ".join(f"{count} {extension}" for extension, count in sorted(counts.items())))


class UploadsMiddlewareState(AgentState):
    uploaded_files: NotRequired[list[dict] | None]


class UploadsMiddleware(AgentMiddleware[UploadsMiddlewareState]):
    """List this turn's uploads once. Later turns discover files with read_file."""

    state_schema = UploadsMiddlewareState

    def __init__(self, base_dir: str | None = None, *, max_files_per_context_section: int = _MAX_FILES_PER_CONTEXT_SECTION) -> None:
        super().__init__()
        if max_files_per_context_section < 1:
            raise ValueError("max_files_per_context_section must be at least 1")
        self._paths = Paths(base_dir) if base_dir is not None else get_paths()
        self._max_files_per_context_section = max_files_per_context_section

    def _files_from_message(self, message: HumanMessage, uploads_dir: Path) -> list[dict]:
        raw_files = (message.additional_kwargs or {}).get("files")
        if not isinstance(raw_files, list):
            return []
        files: list[dict] = []
        for item in raw_files:
            if not isinstance(item, dict):
                continue
            filename = item.get("filename") or ""
            if not isinstance(filename, str) or not filename:
                continue
            if Path(filename).name != filename or _is_staging_file(filename):
                continue
            if not (uploads_dir / filename).is_file():
                continue
            files.append(
                {
                    "filename": filename,
                    "size": _coerce_file_size(item.get("size")),
                    "path": f"/mnt/user-data/uploads/{filename}",
                    "extension": Path(filename).suffix,
                }
            )
        return files

    def _format_entry(self, file: dict, lines: list[str]) -> None:
        size_kb = file["size"] / 1024
        size_text = f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb / 1024:.1f} MB"
        lines.append(f"- {neutralize_untrusted_tags(file['filename'])} ({size_text})")
        lines.append(f"  Path: {neutralize_untrusted_tags(file['path'])}")
        outline = [entry for entry in file.get("outline") or [] if not entry.get("truncated")]
        if outline:
            lines.append("  Document outline (use `read_file` with line ranges to read sections):")
            for entry in outline:
                lines.append(f"  L{entry['line']}: {neutralize_untrusted_tags(str(entry.get('title') or ''))}")
        else:
            preview = file.get("outline_preview") or []
            if preview:
                lines.append("  No structural headings detected. Document begins with:")
                for text in preview:
                    lines.append(f"  > {neutralize_untrusted_tags(str(text))}")
        lines.append("")

    def _files_message(self, files: list[dict], omitted: list[dict]) -> str:
        lines = ["<current_uploads>", "The following files were uploaded in this message:", ""]
        for file in files:
            self._format_entry(file, lines)
        if omitted:
            lines.append(f"... ({len(omitted)} more file(s) from this message omitted from this context.)")
            lines.append(f"  Omitted file types: {_omitted_types(omitted)}")
            lines.append("")
        lines.append("</current_uploads>")
        return "\n".join(lines)

    def before_agent(self, state: UploadsMiddlewareState, runtime: Runtime) -> dict:
        messages = list(state.get("messages") or [])
        if not messages or not isinstance(messages[-1], HumanMessage):
            return {"uploaded_files": []}

        last_message = messages[-1]
        thread_id = resolve_thread_id(runtime)
        user_id = runtime_user_bucket(runtime)
        uploads_dir = self._paths.sandbox_uploads_dir(thread_id, user_id=user_id)
        new_files = self._files_from_message(last_message, uploads_dir)
        if not new_files:
            return {"uploaded_files": []}

        selected = new_files[: self._max_files_per_context_section]
        omitted = new_files[self._max_files_per_context_section :]
        for file in selected:
            try:
                outline, preview = extract_outline_for_file(uploads_dir / file["filename"])
            except Exception:
                logger.debug("Upload outline failed for %s", file["filename"], exc_info=True)
                outline, preview = [], []
            file["outline"] = outline
            file["outline_preview"] = preview

        additional_kwargs = dict(last_message.additional_kwargs or {})
        record_original_user_content(additional_kwargs, last_message.content)
        files_message = self._files_message(selected, omitted)
        content = last_message.content
        if isinstance(content, str):
            updated_content: str | list = f"{files_message}\n\n{content}"
        elif isinstance(content, list):
            updated_content = [{"type": "text", "text": f"{files_message}\n\n"}, *content]
        else:
            updated_content = content
        updated = last_message.model_copy(update={"content": updated_content, "additional_kwargs": additional_kwargs})
        return {"uploaded_files": new_files, "messages": [updated]}

    async def abefore_agent(self, state: UploadsMiddlewareState, runtime: Runtime) -> dict:
        return await run_in_executor(None, self.before_agent, state, runtime)

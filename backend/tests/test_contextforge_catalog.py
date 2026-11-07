from langchain_core.tools import StructuredTool

from agentkit.contextforge.cache import (
    get_contextforge_snapshot,
    invalidate_contextforge_cache,
    set_contextforge_snapshot,
)
from agentkit.contextforge.catalog import build_entries, build_entry
from agentkit.tools.contextforge_metadata import is_contextforge_tool, tag_contextforge_tool


def _tool(name: str = "github_search") -> StructuredTool:
    def _run(query: str = "") -> str:
        return query

    return StructuredTool.from_function(
        _run,
        name=name,
        description="Search GitHub code",
    )


def test_catalog_entry_uses_tool_metadata_and_parameters() -> None:
    tool = _tool()
    tag_contextforge_tool(tool, original_name=tool.name)
    assert is_contextforge_tool(tool)
    entry = build_entry(tool, tags=("github",), display_name="GitHub Search")
    assert entry.name == "github_search"
    assert entry.display_name == "GitHub Search"
    assert entry.parameters == ("query",)
    assert "github" in entry.content
    assert len(entry.content_hash) == 64


def test_content_hash_changes_with_description() -> None:
    first = build_entry(_tool())
    second = build_entry(_tool())
    tool = _tool()
    tool.description = "Search GitHub repositories"
    third = build_entry(tool)
    assert first.content_hash == second.content_hash
    assert first.content_hash != third.content_hash


def test_snapshot_cache_is_process_local() -> None:
    invalidate_contextforge_cache("test")
    assert get_contextforge_snapshot() is None
    entries = build_entries([_tool()])
    snapshot = set_contextforge_snapshot(tools=(_tool(),), entries=entries)
    assert get_contextforge_snapshot() is snapshot
    invalidate_contextforge_cache("test")
    assert get_contextforge_snapshot() is None

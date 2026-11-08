"""Catalog-entry building: the retrieval document a tool row becomes.

The contextforge_tools row is the authority, and Task 4 builds its `content` from
the published name, display name, description, tags and parameter names.  These
tests pin that document shape and its content hash; the process-level snapshot
cache that used to wrap it was deleted with the loader that filled it.
"""

from langchain_core.tools import StructuredTool

from agentkit.contextforge.catalog import build_entries, build_entry, build_tool_content


def _tool(name: str = "github_search", description: str = "Search GitHub code") -> StructuredTool:
    def _run(query: str = "") -> str:
        return query

    return StructuredTool.from_function(_run, name=name, description=description)


def test_catalog_entry_uses_tool_metadata_and_parameters() -> None:
    entry = build_entry(_tool(), tags=("github",), display_name="GitHub Search")

    assert entry.name == "github_search"
    assert entry.display_name == "GitHub Search"
    assert entry.parameters == ("query",)
    assert "github" in entry.content
    assert len(entry.content_hash) == 64


def test_content_hash_changes_with_description() -> None:
    first = build_entry(_tool())
    second = build_entry(_tool())
    third = build_entry(_tool(description="Search GitHub repositories"))

    assert first.content_hash == second.content_hash
    assert first.content_hash != third.content_hash


def test_build_entries_keeps_published_order_and_names() -> None:
    entries = build_entries([_tool("github_search"), _tool("gitlab_search")])

    assert [entry.name for entry in entries] == ["github_search", "gitlab_search"]


def test_tool_content_is_truncated_to_the_document_budget() -> None:
    content = build_tool_content(
        name="github_search",
        display_name="GitHub Search",
        description="x" * 500,
        tags=("github",),
        parameters=("query",),
        max_chars=80,
    )

    assert len(content) == 80

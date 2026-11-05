"""Memory library: protocol mapping, URI safety, and sidecar protection."""

from __future__ import annotations

import pytest

from agentkit.agents.memory.openviking.library import (
    InvalidMemoryURI,
    MemoryItemNotFound,
    MemoryLibraryService,
    MemoryUnavailable,
    MemoryWriteConflict,
    parse_overview_toc,
    resolve_memory_uri,
)

USER = "u_abc"


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.records: list[dict] = []
        self.overviews: dict[str, str] = {}
        self.files: dict[str, str] = {}
        self.ls_entries: dict[str, list[dict]] = {}
        self.fail: dict[str, Exception] = {}

    async def find(self, **kwargs):
        self.calls.append(("find", kwargs))
        if error := self.fail.get("find"):
            raise error
        return {"memories": list(self.records)}

    async def overview(self, uri: str) -> str:
        self.calls.append(("overview", uri))
        if error := self.fail.get("overview"):
            raise error
        return self.overviews.get(uri, "")

    async def read(self, uri: str) -> str:
        self.calls.append(("read", uri))
        if error := self.fail.get("read"):
            raise error
        return self.files[uri]

    async def ls(self, uri: str, **kwargs):
        self.calls.append(("ls", uri, kwargs))
        return self.ls_entries.get(uri, [])

    async def write(self, uri: str, content: str, *, mode="replace", options=None):
        self.calls.append(("write", uri, content, mode, options))
        if error := self.fail.get("write"):
            raise error
        self.files[uri] = content
        return {"status": "ok"}

    async def rm(self, uri: str, *, recursive=False) -> None:
        self.calls.append(("rm", uri, recursive))
        if error := self.fail.get("rm"):
            raise error
        self.files.pop(uri, None)


class FakeResolver:
    def __init__(self, client: FakeClient) -> None:
        self.client = client

    async def client_for(self, ov_user: str):
        assert ov_user == USER
        return self.client


def service(client: FakeClient) -> MemoryLibraryService:
    return MemoryLibraryService(FakeResolver(client))


def _uri(path: str = "preferences/alice/style.md") -> str:
    return f"viking://user/{USER}/memories/{path}"


def test_resolve_memory_uri_accepts_only_user_memory_files() -> None:
    parsed = resolve_memory_uri(USER, _uri())
    assert parsed.folder == f"viking://user/{USER}/memories/preferences/alice"
    assert parsed.name == "style.md"

    tools = resolve_memory_uri(USER, f"viking://user/{USER}/memories/tools/x.md")
    assert tools.memory_type == "tools"

    for bad in (
        f"viking://user/{USER}/memories/preferences/.abstract.md",
        f"viking://user/{USER}/memories/preferences/.overview.md",
        f"viking://user/{USER}/memories/preferences/alice/.hidden.md",
        f"viking://user/{USER}/memories/preferences/alice/style.txt",
        f"viking://user/{USER}/memories/skills/x.md",
        f"viking://user/{USER}/memories/profile.md",
        f"viking://user/{USER}/peers/p/memories/preferences/x.md",
        f"viking://user/other/memories/preferences/x.md",
        "viking://~/memories/preferences/x.md",
        "", "not-a-uri",
    ):
        with pytest.raises(InvalidMemoryURI):
            resolve_memory_uri(USER, bad)


def test_parse_overview_toc_keeps_headings_and_safe_links_only() -> None:
    folder = f"viking://user/{USER}/memories/preferences/alice"
    toc = parse_overview_toc(
        "---\ndirectory: x\n---\n# Preferences\n\n- [Style](./style.md)\n"
        "- [Escape](../evil.md)\n- [Site](https://example.com)\n",
        folder=folder,
    )
    assert [(entry.kind, entry.title, entry.uri) for entry in toc] == [
        ("heading", "Preferences", None),
        ("link", "Style", folder + "/style.md"),
        ("link", "Escape", None),
        ("link", "Site", None),
    ]


@pytest.mark.asyncio
async def test_list_items_uses_filter_only_find_and_truncates_summaries() -> None:
    client = FakeClient()
    long_abstract = "x" * 700
    client.records = [
        {"uri": _uri("preferences/alice/style.md"), "abstract": long_abstract, "updated_at": "t1"},
        {"uri": _uri("entities/acme.md"), "abstract": "wrong type"},
        {"uri": _uri("preferences/alice/.overview.md"), "abstract": "sidecar"},
        {"uri": "viking://resources/docs/a.md", "abstract": "resource"},
    ]

    page = await service(client).list_items(USER, "preferences", limit=10)

    assert [item.name for item in page.items] == ["style.md"]
    assert len(page.items[0].abstract) == 500
    assert page.items[0].abstract_truncated is True
    assert page.items[0].updated_at == "t1"
    assert page.has_more is False
    kwargs = client.calls[0][1]
    assert kwargs["query"] == ""
    assert kwargs["target_uri"] == [f"viking://user/{USER}/memories/preferences"]
    assert kwargs["limit"] == 11
    assert kwargs["options"]["level"] == [2]
    assert kwargs["options"]["filter"]["field"] == "context_type"


@pytest.mark.asyncio
async def test_list_items_reports_more_and_sorts_by_uri() -> None:
    client = FakeClient()
    client.records = [
        {"uri": _uri("preferences/alice/z.md"), "abstract": "z"},
        {"uri": _uri("preferences/alice/a.md"), "abstract": "a"},
        {"uri": _uri("preferences/alice/m.md"), "abstract": "m"},
    ]

    page = await service(client).list_items(USER, "preferences", limit=2)

    assert [item.name for item in page.items] == ["a.md", "m.md"]
    assert page.has_more is True


@pytest.mark.asyncio
async def test_detail_returns_overview_toc() -> None:
    client = FakeClient()
    folder = f"viking://user/{USER}/memories/preferences/alice"
    client.overviews[folder] = "# Prefs\n- [Style](./style.md)\n"

    detail = await service(client).detail(USER, _uri())

    assert detail.overview_missing is False
    assert [(entry.kind, entry.uri) for entry in detail.toc] == [
        ("heading", None),
        ("link", folder + "/style.md"),
    ]


@pytest.mark.asyncio
async def test_detail_falls_back_to_ls_without_overview() -> None:
    client = FakeClient()
    folder = f"viking://user/{USER}/memories/trajectories"
    client.ls_entries[folder] = [
        {"name": "run_a.md", "isDir": False},
        {"name": ".overview.md", "isDir": False},
        {"name": "nested", "isDir": True},
    ]

    detail = await service(client).detail(USER, _uri("trajectories/run_a.md"))

    assert detail.overview_missing is True
    assert [entry.title for entry in detail.toc] == ["run_a"]
    assert detail.toc[0].uri == folder + "/run_a.md"


@pytest.mark.asyncio
async def test_content_reads_and_hashes() -> None:
    client = FakeClient()
    client.files[_uri()] = "hello"

    content = await service(client).read_content(USER, _uri())

    assert content.content == "hello"
    assert content.size_bytes == 5
    assert len(content.sha256) == 64


@pytest.mark.asyncio
async def test_update_requires_matching_sha_and_uses_manual_edit_source() -> None:
    client = FakeClient()
    client.files[_uri()] = "hello"
    library = service(client)

    with pytest.raises(MemoryWriteConflict):
        await library.update_content(USER, _uri(), "changed", expected_sha256="0" * 64)

    current = await library.read_content(USER, _uri())
    updated = await library.update_content(
        USER, _uri(), "changed", expected_sha256=current.sha256
    )

    assert updated.content == "changed"
    assert any(call[0] == "write" for call in client.calls)
    write_call = next(call for call in client.calls if call[0] == "write")
    assert write_call[3] == "replace"
    assert write_call[4] == {"edit_source": "manual"}


@pytest.mark.asyncio
async def test_update_rejects_empty_and_oversized_content() -> None:
    client = FakeClient()
    client.files[_uri()] = "hello"
    library = service(client)

    with pytest.raises(ValueError):
        await library.update_content(USER, _uri(), "   ", expected_sha256=None)
    with pytest.raises(ValueError):
        await library.update_content(
            USER, _uri(), "x" * 200_001, expected_sha256=None
        )


@pytest.mark.asyncio
async def test_delete_reads_before_rm_and_is_not_recursive() -> None:
    client = FakeClient()
    client.files[_uri()] = "hello"

    await service(client).delete_item(USER, _uri())

    assert [call[0] for call in client.calls] == ["read", "rm"]
    assert client.calls[1][2] is False


@pytest.mark.asyncio
async def test_missing_memory_and_protocol_errors_are_translated() -> None:
    class NotFound(Exception):
        code = "NOT_FOUND"

    client = FakeClient()
    client.fail["read"] = NotFound()
    with pytest.raises(MemoryItemNotFound):
        await service(client).read_content(USER, _uri())

    client = FakeClient()
    client.fail["read"] = RuntimeError("boom")
    with pytest.raises(MemoryUnavailable):
        await service(client).read_content(USER, _uri())

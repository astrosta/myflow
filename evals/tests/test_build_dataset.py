"""Dataset-build tests.

Both transports are faked: an httpx.MockTransport stands in for the Gateway's
dataset-source GETs and another for the MCP tools/list and Langfuse trace reads.
The builder is pure, so most cases hand build_account_items a BuildSources
directly and prove that no HTTP call happens at all.
"""

from __future__ import annotations

import json
import random
from typing import Any

import httpx
import pytest

from myflow_evals.accounts import AccountCredential, AccountSession
from myflow_evals.build_dataset import (
    BuildSources,
    DatasetItemDraft,
    KnowledgeSource,
    SkillSource,
    TraceSource,
    build_account_items,
    build_report,
    fetch_account_traces,
    load_build_sources,
    stable_item_id,
    write_drafts,
)
from myflow_evals.mcp import McpClient, McpTool

BASE_URLS = ["http://gateway.test/api", "http://gateway.test"]
CSRF_TOKEN = "csrf-token"
RAW_USER_ID = "11111111-1111-4111-8111-111111111111"
HASHED_USER_ID = "9f2c4b6d8e0a1c3e5f7a9b1d3c5e7f90"
PASSWORD = "alice-password"
USERNAME = "alice"

MCP_URL = "https://mcp.test/projects"
LEGACY_URL = "https://mcp.test/legacy"

LANGFUSE_HOST = "https://langfuse.test"
LANGFUSE_ENVIRON = {
    "LANGFUSE_HOST": LANGFUSE_HOST,
    "LANGFUSE_PUBLIC_KEY": "pk-fixture-public",
    "LANGFUSE_SECRET_KEY": "sk-fixture-secret",
}

SEARCH_SCHEMA = {
    "type": "object",
    "required": ["sector", "created_after"],
    "properties": {
        "sector": {"type": "string", "enum": ["solar", "wind"]},
        "created_after": {"type": "string"},
        "limit": {"type": "integer"},
    },
}

PROJECT_TOOLS = [
    {
        "name": "search_projects",
        "description": "Search the project database",
        "inputSchema": SEARCH_SCHEMA,
    },
    {
        "name": "get_project",
        "description": "Load one project",
        "inputSchema": {
            "type": "object",
            "required": ["project_id"],
            "properties": {"project_id": {"type": "string"}},
        },
    },
]

#: GET /api/mcp-servers/{id}/tools as the Gateway projects it: only a row that
#: is both callable and enabled describes a tool the Agent can really call.
PROJECT_TOOL_INDEX = [
    {
        "name": "search_projects",
        "call_name": "mcp__project_db__search_projects",
        "display_name": "search_projects",
        "description": "Search the project database",
        "parameters": SEARCH_SCHEMA,
        "callable": True,
        "enabled": True,
    },
    {
        "name": "get_project",
        "call_name": "mcp__project_db__get_project",
        "display_name": "get_project",
        "description": "Load one project",
        "parameters": {
            "type": "object",
            "required": ["project_id"],
            "properties": {"project_id": {"type": "string"}},
        },
        "callable": True,
        "enabled": True,
    },
    {
        "name": "list_sectors",
        "call_name": "mcp__project_db__list_sectors",
        "display_name": "list_sectors",
        "description": "",
        "parameters": {"type": "object"},
        "callable": True,
        "enabled": True,
    },
]

STREAMABLE_SERVER = {
    "id": "srv-1",
    "name": "project_db",
    "url": MCP_URL,
    "transport": "streamable_http",
    "enabled": True,
    "status": "active",
    "reachable": True,
    "tool_count": 2,
}
SSE_SERVER = {
    "id": "srv-2",
    "name": "legacy_db",
    "url": LEGACY_URL,
    "transport": "sse",
    "enabled": True,
    "status": "active",
    "reachable": True,
    "tool_count": 1,
}
NO_URL_SERVER = {
    "id": "srv-3",
    "name": "no_url_db",
    "url": "",
    "transport": "streamable_http",
    "enabled": True,
    "status": "active",
    "reachable": True,
    "tool_count": 1,
}

PRIVATE_SKILL_ROW = {
    "name": "daily-finance-brief",
    "description": "Turn a story into a numbered daily brief",
    "category": "custom",
    "scope": "private",
    "skill_id": "skill-private-1",
    "git_commit": "alb2c3d",
    "enabled": True,
    "status": "active",
}
SECOND_PRIVATE_SKILL_ROW = {
    "name": "risk-brief",
    "description": "Summarise project risk",
    "category": "custom",
    "scope": "private",
    "skill_id": "skill-private-2",
    "git_commit": None,
    "enabled": True,
    "status": "active",
}

SKILLS = [
    {
        "name": "builtin-helper",
        "description": "A builtin skill",
        "category": "builtin",
        "scope": "builtin",
        "skill_id": "skill-builtin",
        "git_commit": "aaa1111",
        "enabled": True,
        "status": "active",
    },
    {
        "name": "public-helper",
        "description": "A public skill",
        "category": "public",
        "scope": "public",
        "skill_id": "skill-public",
        "enabled": True,
        "status": "active",
    },
    PRIVATE_SKILL_ROW,
    SECOND_PRIVATE_SKILL_ROW,
    {
        "name": "private-without-id",
        "description": "No skill_id",
        "category": "custom",
        "scope": "private",
        "enabled": True,
        "status": "active",
    },
]

BASES = [
    {
        "id": "kb-1",
        "name": "Policy",
        "type": "local",
        "description": "",
        "document_count": 2,
        "chunk_count": 3,
    }
]

DOCUMENTS = {
    "kb-1": [
        {
            "id": "doc-1",
            "knowledge_base_id": "kb-1",
            "filename": "policy.md",
            "mime_type": "text/markdown",
            "size_bytes": 120,
            "status": "ready",
            "chunk_count": 2,
            "error_message": None,
        },
        {
            "id": "doc-2",
            "knowledge_base_id": "kb-1",
            "filename": "sla.md",
            "mime_type": "text/markdown",
            "size_bytes": 80,
            "status": "ready",
            "chunk_count": 1,
            "error_message": None,
        },
    ]
}

CHUNKS = {
    "doc-1": [
        {
            "chunk_id": "doc-1:0",
            "chunk_index": 0,
            "section": "refunds",
            "page": 1,
            "content": "Refunds take five business days.",
            "parent_id": None,
        },
        {
            "chunk_id": "doc-1:1",
            "chunk_index": 1,
            "section": "returns",
            "page": 2,
            "content": "Returns need a receipt.",
            "parent_id": None,
        },
    ],
    "doc-2": [
        {
            "chunk_id": "doc-2:0",
            "chunk_index": 0,
            "section": "support",
            "page": 1,
            "content": "Support answers within one business day.",
            "parent_id": None,
        }
    ],
}

TRACE_ROWS = [
    {
        "id": "trace-error",
        "name": "agent-run",
        "userId": HASHED_USER_ID,
        "input": {"messages": [{"type": "human", "content": "Why did the refund fail?"}]},
        "output": {"type": "ai", "content": ""},
        "metadata": {"category": "kb_single_hop"},
        "tags": ["eval"],
        "level": "ERROR",
        "timestamp": "2026-11-01T00:00:00Z",
    },
    {
        "id": "trace-low",
        "name": "agent-run",
        "userId": HASHED_USER_ID,
        "input": "Where is the project data?",
        "output": {"type": "ai", "content": "No idea"},
        "metadata": {},
        "tags": [],
        "level": "DEFAULT",
        "timestamp": "2026-11-01T00:00:00Z",
        "scores": [{"name": "answer_quality", "value": 0.2}],
    },
    {
        "id": "trace-ok",
        "name": "agent-run",
        "userId": HASHED_USER_ID,
        "input": "Summarize the SLA",
        "output": {"type": "ai", "content": "One business day"},
        "metadata": {},
        "tags": [],
        "level": "DEFAULT",
        "timestamp": "2026-11-01T00:00:00Z",
        "scores": [{"name": "answer_quality", "value": 0.9}],
    },
    {
        "id": "trace-no-input",
        "name": "agent-run",
        "userId": HASHED_USER_ID,
        "input": None,
        "output": None,
        "metadata": {},
        "tags": [],
        "level": "ERROR",
        "timestamp": "2026-11-01T00:00:00Z",
        "scores": [],
    },
]

SEARCH_TOOL = McpTool(
    call_name="mcp__project_db__search_projects",
    name="search_projects",
    description="Search the project database.",
    input_schema=SEARCH_SCHEMA,
)
PROJECT_TOOL = McpTool(
    call_name="mcp__project_db__get_project",
    name="get_project",
    description="Load one project.",
    input_schema={
        "type": "object",
        "required": ["project_id"],
        "properties": {"project_id": {"type": "string"}},
    },
)
THIRD_TOOL = McpTool(
    call_name="mcp__project_db__list_sectors",
    name="list_sectors",
    description="",
    input_schema={"type": "object"},
)
NESTED_TOOL = McpTool(
    call_name="mcp__crm__create_deal",
    name="create_deal",
    description="Create one deal",
    input_schema={
        "type": "object",
        "required": ["filter", "when"],
        "properties": {
            "filter": {
                "type": "object",
                "required": ["sector", "window"],
                "properties": {
                    "sector": {"type": "string", "enum": ["solar", "wind"]},
                    "window": {
                        "type": "object",
                        "required": ["start", "end"],
                        "properties": {"start": {"type": "string"}, "end": {"type": "string"}},
                    },
                },
            },
            "when": {"type": "boolean"},
        },
    },
)

PRIVATE_SKILL = SkillSource(
    skill_id="skill-private-1",
    name="daily-finance-brief",
    description="Turn a story into a numbered daily brief",
    git_commit="alb2c3d",
)
SECOND_SKILL = SkillSource(
    skill_id="skill-private-2",
    name="risk-brief",
    description="",
    git_commit=None,
)
POLICY_DOCUMENT = KnowledgeSource(
    knowledge_base_id="kb-1",
    document_id="doc-1",
    filename="policy.md",
    chunk_id="doc-1:0",
    chunk_content="Refunds take five business days.",
    chunk_count=2,
)
SLA_DOCUMENT = KnowledgeSource(
    knowledge_base_id="kb-1",
    document_id="doc-2",
    filename="sla.md",
    chunk_id="doc-2:0",
    chunk_content="Support answers within one business day.",
    chunk_count=1,
)
FAILED_TRACE = TraceSource(
    trace_id="trace-error", query="Why did the refund fail?", category="kb_single_hop"
)
PLAIN_TRACE = TraceSource(trace_id="trace-low", query="Where is the project data?")


class _FakeGateway:
    """The fixed account's Gateway data, served from memory."""

    def __init__(
        self,
        *,
        servers: list[dict[str, Any]] | None = None,
        skills: list[dict[str, Any]] | None = None,
        bases: list[dict[str, Any]] | None = None,
        documents: dict[str, list[dict[str, Any]]] | None = None,
        chunks: dict[str, list[dict[str, Any]]] | None = None,
        tool_index: dict[str, list[dict[str, Any]] | int] | None = None,
    ) -> None:
        self.servers = [STREAMABLE_SERVER, SSE_SERVER] if servers is None else servers
        self.skills = SKILLS if skills is None else skills
        self.bases = BASES if bases is None else bases
        self.documents = DOCUMENTS if documents is None else documents
        self.chunks = CHUNKS if chunks is None else chunks
        self.tool_index = (
            {STREAMABLE_SERVER["id"]: PROJECT_TOOL_INDEX}
            if tool_index is None
            else tool_index
        )
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/api/mcp-servers":
            return httpx.Response(200, json={"servers": self.servers, "count": len(self.servers)})
        if path.startswith("/api/mcp-servers/") and path.endswith("/tools"):
            server_id = path.split("/")[-2]
            entry = self.tool_index.get(server_id, [])
            if isinstance(entry, int):
                return httpx.Response(entry, json={"detail": "tool index unavailable"})
            return httpx.Response(200, json={"tools": entry, "count": len(entry)})
        if path == "/api/skills":
            return httpx.Response(200, json={"skills": self.skills})
        if path == "/api/knowledge/bases":
            return httpx.Response(200, json={"bases": self.bases, "count": len(self.bases)})
        if path.endswith("/documents"):
            base_id = path.split("/")[-2]
            rows = self.documents.get(base_id, [])
            return httpx.Response(200, json={"documents": rows, "count": len(rows)})
        if path.endswith("/chunks"):
            document_id = path.split("/")[-2]
            rows = self.chunks.get(document_id, [])
            offset = int(request.url.params.get("offset", "0"))
            limit = int(request.url.params.get("limit", str(len(rows) or 1)))
            page = rows[offset : offset + limit]
            next_offset = offset + len(page)
            return httpx.Response(
                200,
                json={
                    "document_id": document_id,
                    "chunks": page,
                    "count": len(rows),
                    "next_offset": next_offset if next_offset < len(rows) else None,
                },
            )
        return httpx.Response(404, json={"detail": "not found"})


class _Mcp:
    """A fake MCP server answering tools/list for one expected url."""

    def __init__(self, tools: list[dict[str, Any]], *, url: str = MCP_URL) -> None:
        self._tools = tools
        self._url = url
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == self._url, f"unexpected MCP url {request.url}"
        self.requests.append(request)
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        assert body["method"] == "tools/list", f"unexpected MCP method {body['method']}"
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": self._tools}}
        )

    @property
    def client(self) -> McpClient:
        return McpClient(transport=httpx.MockTransport(self))


def _session(
    handler: Any,
    base_url: str = BASE_URLS[0],
    *,
    hashed: str = HASHED_USER_ID,
) -> AccountSession:
    client = httpx.Client(
        base_url=base_url,
        transport=httpx.MockTransport(handler),
        headers={"X-CSRF-Token": CSRF_TOKEN},
    )
    client.cookies.set("access_token", "access-token")
    return AccountSession(
        credential=AccountCredential(label="analyst_a", username=USERNAME, password=PASSWORD),
        client=client,
        user_id=RAW_USER_ID,
        hashed_user_id=hashed,
    )


def _offline_session(hashed: str = HASHED_USER_ID) -> AccountSession:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"the draft builder must not make HTTP calls: {request.url}")

    return _session(handler, hashed=hashed)


def _sources(
    *,
    tools: Any = (),
    skills: Any = (),
    knowledge: Any = (),
    traces: Any = (),
    warnings: Any = (),
) -> BuildSources:
    return BuildSources(
        tools=tools,
        skills=skills,
        knowledge=knowledge,
        traces=traces,
        warnings=warnings,
    )


def _projection(tool_name: str, **flags: Any) -> list[dict[str, Any]]:
    """The Gateway tool index with one tool's projection flags overridden."""
    return [
        dict(row, **flags) if row["name"] == tool_name else dict(row)
        for row in PROJECT_TOOL_INDEX
    ]


def _chunk(document_id: str, index: int, content: str) -> dict[str, Any]:
    return {
        "chunk_id": f"{document_id}:{index}",
        "chunk_index": index,
        "section": f"section-{index}",
        "page": 1,
        "content": content,
        "parent_id": None,
    }


def _knowledge_gateway(
    contents: list[str], *, document_id: str = "doc-1"
) -> _FakeGateway:
    """A Gateway serving one knowledge document with the given chunk contents."""
    document = {
        "id": document_id,
        "knowledge_base_id": "kb-1",
        "filename": "policy.md",
        "mime_type": "text/markdown",
        "size_bytes": 120,
        "status": "ready",
        "chunk_count": len(contents),
        "error_message": None,
    }
    chunks = [_chunk(document_id, index, content) for index, content in enumerate(contents)]
    return _FakeGateway(
        servers=[STREAMABLE_SERVER],
        documents={"kb-1": [document]},
        chunks={document_id: chunks},
    )


def _chunk_requests(gateway: _FakeGateway, document_id: str = "doc-1") -> list[httpx.Request]:
    return [
        request
        for request in gateway.requests
        if request.url.path.endswith(f"/documents/{document_id}/chunks")
    ]


def _chunk_pattern(requests: list[httpx.Request]) -> list[tuple[str, str]]:
    return [
        (request.url.params["offset"], request.url.params["limit"])
        for request in requests
    ]


@pytest.mark.parametrize("base_url", BASE_URLS)
def test_load_build_sources_reads_the_fixed_accounts_real_data(base_url: str) -> None:
    gateway = _FakeGateway()
    mcp = _Mcp(PROJECT_TOOLS)

    sources = load_build_sources(_session(gateway, base_url), environ={}, mcp_client=mcp.client)

    assert [tool.call_name for tool in sources.tools] == [
        "mcp__project_db__search_projects",
        "mcp__project_db__get_project",
    ]
    assert [skill.name for skill in sources.skills] == ["daily-finance-brief", "risk-brief"]
    assert [skill.git_commit for skill in sources.skills] == ["alb2c3d", None]
    assert [
        (document.document_id, document.chunk_id, document.chunk_content)
        for document in sources.knowledge
    ] == [
        ("doc-1", "doc-1:0", "Refunds take five business days."),
        ("doc-2", "doc-2:0", "Support answers within one business day."),
    ]
    assert sources.traces == []
    assert {request.url.path for request in gateway.requests} == {
        "/api/mcp-servers",
        "/api/mcp-servers/srv-1/tools",
        "/api/skills",
        "/api/knowledge/bases",
        "/api/knowledge/bases/kb-1/documents",
        "/api/knowledge/bases/kb-1/documents/doc-1/chunks",
        "/api/knowledge/bases/kb-1/documents/doc-2/chunks",
    }
    chunk_requests = [
        request for request in gateway.requests if request.url.path.endswith("/chunks")
    ]
    assert _chunk_pattern(chunk_requests) == [
        ("0", "1"),
        ("0", "2"),
        ("0", "1"),
    ]
    assert all(request.headers["x-csrf-token"] == CSRF_TOKEN for request in gateway.requests)


UNSAFE_TOOL_SCHEMA = {"type": "object", "required": [], "properties": {}}
UNCALLABLE_TOOLS = [
    {
        "name": "search.projects",
        "description": "a dotted tool name no provider accepts",
        "inputSchema": UNSAFE_TOOL_SCHEMA,
    },
    {
        "name": "t" * 200,
        "description": "a tool name beyond the call-name bound",
        "inputSchema": UNSAFE_TOOL_SCHEMA,
    },
    {
        "name": "get_project",
        "description": "a callable tool",
        "inputSchema": UNSAFE_TOOL_SCHEMA,
    },
]


def test_load_build_sources_skips_tools_the_agent_cannot_call() -> None:
    sources = load_build_sources(
        _session(_FakeGateway(servers=[STREAMABLE_SERVER])),
        environ={},
        mcp_client=_Mcp(UNCALLABLE_TOOLS).client,
    )

    drafts = build_account_items(_offline_session(), sources)

    assert [tool.call_name for tool in sources.tools] == ["mcp__project_db__get_project"]
    tool_drafts = [
        draft for draft in drafts if draft.metadata["source_kind"] == "tool"
    ]
    assert [draft.metadata["source_id"] for draft in tool_drafts] == [
        "mcp__project_db__get_project"
    ]
    warnings = " ".join(sources.warnings)
    assert "search.projects" in warnings
    assert "cannot call" in warnings


def test_build_account_items_never_emits_an_uncallable_tool() -> None:
    sources = BuildSources(
        tools=[
            McpTool(
                call_name="mcp__db__bad.tool",
                name="bad.tool",
                description="dotted",
                input_schema=UNSAFE_TOOL_SCHEMA,
            )
        ]
    )

    assert build_account_items(_offline_session(), sources) == []


def test_load_build_sources_skips_servers_it_cannot_call() -> None:
    gateway = _FakeGateway(servers=[STREAMABLE_SERVER, SSE_SERVER, NO_URL_SERVER])
    mcp = _Mcp(PROJECT_TOOLS)

    sources = load_build_sources(_session(gateway), environ={}, mcp_client=mcp.client)

    assert [tool.name for tool in sources.tools] == ["search_projects", "get_project"]
    assert len(sources.warnings) == 2
    assert any("legacy_db" in warning and "sse" in warning for warning in sources.warnings)
    assert any("no_url_db" in warning and "url" in warning for warning in sources.warnings)


def test_load_build_sources_skips_a_disabled_server() -> None:
    gateway = _FakeGateway(servers=[dict(STREAMABLE_SERVER, enabled=False)])
    mcp = _Mcp(PROJECT_TOOLS)

    sources = load_build_sources(_session(gateway), environ={}, mcp_client=mcp.client)

    assert sources.tools == []
    assert mcp.requests == []
    assert len(sources.warnings) == 1
    assert "project_db" in sources.warnings[0]
    assert "disabled" in sources.warnings[0]


def test_load_build_sources_skips_a_server_that_is_still_pending() -> None:
    gateway = _FakeGateway(servers=[dict(STREAMABLE_SERVER, status="pending")])
    mcp = _Mcp(PROJECT_TOOLS)

    sources = load_build_sources(_session(gateway), environ={}, mcp_client=mcp.client)

    assert sources.tools == []
    assert mcp.requests == []
    assert len(sources.warnings) == 1
    assert "project_db" in sources.warnings[0]
    assert "pending" in sources.warnings[0]


@pytest.mark.parametrize(
    ("summary", "reason"),
    [
        (dict(STREAMABLE_SERVER, status="error"), "status=error"),
        (dict(STREAMABLE_SERVER, reachable=False), "unreachable"),
    ],
)
def test_load_build_sources_skips_a_server_that_is_not_effective(
    summary: dict[str, Any], reason: str
) -> None:
    gateway = _FakeGateway(servers=[summary])
    mcp = _Mcp(PROJECT_TOOLS)

    sources = load_build_sources(_session(gateway), environ={}, mcp_client=mcp.client)

    assert sources.tools == []
    assert mcp.requests == []
    assert len(sources.warnings) == 1
    assert "project_db" in sources.warnings[0]
    assert reason in sources.warnings[0]


def test_load_build_sources_requires_an_explicit_enabled_flag() -> None:
    summary = {key: value for key, value in STREAMABLE_SERVER.items() if key != "enabled"}
    gateway = _FakeGateway(servers=[summary])
    mcp = _Mcp(PROJECT_TOOLS)

    sources = load_build_sources(_session(gateway), environ={}, mcp_client=mcp.client)

    assert sources.tools == []
    assert mcp.requests == []
    assert len(sources.warnings) == 1
    assert "project_db" in sources.warnings[0]
    assert "enabled" in sources.warnings[0]


def test_an_sse_server_reports_its_transport_before_a_pending_status() -> None:
    gateway = _FakeGateway(servers=[dict(SSE_SERVER, status="pending")])
    mcp = _Mcp(PROJECT_TOOLS)

    sources = load_build_sources(_session(gateway), environ={}, mcp_client=mcp.client)

    assert sources.tools == []
    assert mcp.requests == []
    assert len(sources.warnings) == 1
    assert "legacy_db" in sources.warnings[0]
    assert "sse" in sources.warnings[0]
    assert "transport" in sources.warnings[0]
    assert "pending" not in sources.warnings[0]


def test_load_build_sources_skips_a_tool_the_gateway_index_marks_uncallable() -> None:
    gateway = _FakeGateway(
        servers=[STREAMABLE_SERVER],
        tool_index={STREAMABLE_SERVER["id"]: _projection("get_project", callable=False)},
    )

    sources = load_build_sources(
        _session(gateway), environ={}, mcp_client=_Mcp(PROJECT_TOOLS).client
    )
    drafts = build_account_items(_offline_session(), sources)

    assert [tool.name for tool in sources.tools] == ["search_projects"]
    assert [
        draft.metadata["source_id"]
        for draft in drafts
        if draft.metadata["source_kind"] == "tool"
    ] == ["mcp__project_db__search_projects"]
    assert len(sources.warnings) == 1
    assert "get_project" in sources.warnings[0]
    assert "callable=false" in sources.warnings[0]


def test_load_build_sources_skips_a_tool_disabled_in_the_gateway_index() -> None:
    gateway = _FakeGateway(
        servers=[STREAMABLE_SERVER],
        tool_index={STREAMABLE_SERVER["id"]: _projection("get_project", enabled=False)},
    )

    sources = load_build_sources(
        _session(gateway), environ={}, mcp_client=_Mcp(PROJECT_TOOLS).client
    )
    drafts = build_account_items(_offline_session(), sources)

    assert [tool.name for tool in sources.tools] == ["search_projects"]
    assert [
        draft.metadata["source_id"]
        for draft in drafts
        if draft.metadata["source_kind"] == "tool"
    ] == ["mcp__project_db__search_projects"]
    assert len(sources.warnings) == 1
    assert "get_project" in sources.warnings[0]
    assert "enabled=false" in sources.warnings[0]


def test_a_projection_row_without_a_call_name_still_matches_its_tool() -> None:
    without_call_name = {"name": "get_project", "callable": True, "enabled": True}
    gateway = _FakeGateway(
        servers=[STREAMABLE_SERVER],
        tool_index={
            STREAMABLE_SERVER["id"]: [
                without_call_name if row["name"] == "get_project" else dict(row)
                for row in PROJECT_TOOL_INDEX
            ]
        },
    )

    sources = load_build_sources(
        _session(gateway), environ={}, mcp_client=_Mcp(PROJECT_TOOLS).client
    )

    assert [tool.name for tool in sources.tools] == ["search_projects", "get_project"]
    assert sources.warnings == ()


def test_a_projection_row_without_an_enabled_flag_is_skipped() -> None:
    without_enabled = {
        key: value for key, value in PROJECT_TOOL_INDEX[1].items() if key != "enabled"
    }
    gateway = _FakeGateway(
        servers=[STREAMABLE_SERVER],
        tool_index={
            STREAMABLE_SERVER["id"]: [
                without_enabled if row["name"] == "get_project" else dict(row)
                for row in PROJECT_TOOL_INDEX
            ]
        },
    )

    sources = load_build_sources(
        _session(gateway), environ={}, mcp_client=_Mcp(PROJECT_TOOLS).client
    )

    assert [tool.name for tool in sources.tools] == ["search_projects"]
    assert len(sources.warnings) == 1
    assert "get_project" in sources.warnings[0]
    assert "no enabled flag" in sources.warnings[0]


def test_load_build_sources_skips_a_listed_tool_the_gateway_index_never_saw() -> None:
    gateway = _FakeGateway(
        servers=[STREAMABLE_SERVER],
        tool_index={
            STREAMABLE_SERVER["id"]: [
                row for row in PROJECT_TOOL_INDEX if row["name"] != "get_project"
            ]
        },
    )

    sources = load_build_sources(
        _session(gateway), environ={}, mcp_client=_Mcp(PROJECT_TOOLS).client
    )

    assert [tool.name for tool in sources.tools] == ["search_projects"]
    assert len(sources.warnings) == 1
    assert "get_project" in sources.warnings[0]
    assert "index" in sources.warnings[0]


def test_a_server_whose_tool_index_fails_costs_only_its_own_tools() -> None:
    broken_url = "https://mcp.test/broken-index"
    gateway = _FakeGateway(
        servers=[
            STREAMABLE_SERVER,
            dict(STREAMABLE_SERVER, id="srv-2", name="broken_db", url=broken_url),
        ],
        tool_index={STREAMABLE_SERVER["id"]: PROJECT_TOOL_INDEX, "srv-2": 503},
    )
    healthy = _Mcp(PROJECT_TOOLS)
    broken = _Mcp(PROJECT_TOOLS, url=broken_url)

    def handler(request: httpx.Request) -> httpx.Response:
        return broken(request) if str(request.url) == broken_url else healthy(request)

    sources = load_build_sources(
        _session(gateway),
        environ={},
        mcp_client=McpClient(transport=httpx.MockTransport(handler)),
    )

    assert [tool.name for tool in sources.tools] == ["search_projects", "get_project"]
    assert len(sources.warnings) == 1
    assert "broken_db" in sources.warnings[0]
    assert "HTTP 503" in sources.warnings[0]


def test_load_build_sources_skips_a_private_skill_that_is_switched_off() -> None:
    gateway = _FakeGateway(
        servers=[STREAMABLE_SERVER], skills=[dict(PRIVATE_SKILL_ROW, enabled=False)]
    )

    sources = load_build_sources(
        _session(gateway), environ={}, mcp_client=_Mcp(PROJECT_TOOLS).client
    )

    assert sources.skills == []
    assert len(sources.warnings) == 1
    assert "daily-finance-brief" in sources.warnings[0]
    assert "enabled" in sources.warnings[0]


def test_load_build_sources_skips_a_private_skill_that_is_not_active() -> None:
    gateway = _FakeGateway(
        servers=[STREAMABLE_SERVER], skills=[dict(PRIVATE_SKILL_ROW, status="draft")]
    )

    sources = load_build_sources(
        _session(gateway), environ={}, mcp_client=_Mcp(PROJECT_TOOLS).client
    )

    assert sources.skills == []
    assert len(sources.warnings) == 1
    assert "daily-finance-brief" in sources.warnings[0]
    assert "draft" in sources.warnings[0]


def test_an_all_effective_account_still_builds_every_source() -> None:
    gateway = _FakeGateway(servers=[STREAMABLE_SERVER])

    sources = load_build_sources(
        _session(gateway), environ={}, mcp_client=_Mcp(PROJECT_TOOLS).client
    )
    drafts = build_account_items(_offline_session(), sources)

    assert sources.warnings == ()
    assert sources.source_totals == {"tool": 2, "skill": 2, "knowledge": 2, "trace": 0}
    assert [draft.metadata["source_kind"] for draft in drafts] == [
        "tool",
        "tool",
        "skill",
        "skill",
        "knowledge",
        "knowledge",
    ]

    report = build_report("myflow-eval", sources, drafts)
    assert report.source_counts == {"tool": 2, "skill": 2, "knowledge": 2, "trace": 0}
    assert "tool: 2 drafts from 2 sources" in report.render()
    assert "skill: 2 drafts from 2 sources" in report.render()


def test_a_failing_server_costs_only_its_own_tools() -> None:
    broken_url = "https://mcp.test/broken"
    gateway = _FakeGateway(
        servers=[
            STREAMABLE_SERVER,
            dict(STREAMABLE_SERVER, id="srv-2", name="broken_db", url=broken_url),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == broken_url:
            return httpx.Response(503, text="upstream down")
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": PROJECT_TOOLS}}
        )

    sources = load_build_sources(
        _session(gateway),
        environ={},
        mcp_client=McpClient(transport=httpx.MockTransport(handler)),
    )

    assert [tool.name for tool in sources.tools] == ["search_projects", "get_project"]
    assert len(sources.warnings) == 1
    assert "broken_db" in sources.warnings[0]
    assert "HTTP 503" in sources.warnings[0]


def test_langfuse_failures_cost_only_the_traces() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="langfuse down")

    sources = load_build_sources(
        _session(_FakeGateway(servers=[STREAMABLE_SERVER])),
        environ=LANGFUSE_ENVIRON,
        mcp_client=_Mcp(PROJECT_TOOLS).client,
        trace_transport=httpx.MockTransport(handler),
    )

    drafts = build_account_items(_offline_session(), sources)

    assert sources.traces == []
    assert any("Langfuse" in warning for warning in sources.warnings)
    assert "trace" not in {draft.metadata["source_kind"] for draft in drafts}
    assert len(drafts) == 6


def test_load_build_sources_skips_trace_paging_unless_included() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": [], "meta": {"totalPages": 1}})

    sources = load_build_sources(
        _session(_FakeGateway(servers=[STREAMABLE_SERVER])),
        environ=LANGFUSE_ENVIRON,
        mcp_client=_Mcp(PROJECT_TOOLS).client,
        trace_transport=httpx.MockTransport(handler),
        include_traces=False,
    )

    drafts = build_account_items(_offline_session(), sources)

    assert requests == []
    assert list(sources.traces) == []
    assert sources.source_totals["trace"] == 0
    assert "trace" not in {draft.metadata["source_kind"] for draft in drafts}


def test_load_build_sources_reads_traces_when_included() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "trace-1", "level": "ERROR", "input": {"query": "why did it fail?"}}
                ],
                "meta": {"totalPages": 1},
            },
        )

    sources = load_build_sources(
        _session(_FakeGateway(servers=[STREAMABLE_SERVER])),
        environ=LANGFUSE_ENVIRON,
        mcp_client=_Mcp(PROJECT_TOOLS).client,
        trace_transport=httpx.MockTransport(handler),
        include_traces=True,
    )

    drafts = build_account_items(_offline_session(), sources)

    assert [trace.trace_id for trace in sources.traces] == ["trace-1"]
    assert "trace" in {draft.metadata["source_kind"] for draft in drafts}

def test_load_build_sources_takes_mcp_auth_and_a_url_override_from_the_secret_map() -> None:
    secret_value = "fixture-header-value"
    override_url = "https://mcp.test/projects?token=query-secret"
    gateway = _FakeGateway(servers=[dict(STREAMABLE_SERVER, url="")])
    mcp = _Mcp(PROJECT_TOOLS, url=override_url)
    environ = {
        "MCP_SERVER_HEADERS": json.dumps(
            {"project_db": {"X-Api-Key": secret_value, "url": override_url}}
        )
    }

    sources = load_build_sources(_session(gateway), environ=environ, mcp_client=mcp.client)

    assert [tool.name for tool in sources.tools] == ["search_projects", "get_project"]
    assert all(request.headers["x-api-key"] == secret_value for request in mcp.requests)
    assert secret_value not in " ".join(sources.warnings)
    assert "query-secret" not in " ".join(sources.warnings)


def test_load_build_sources_caps_each_kind_at_the_limit() -> None:
    gateway = _FakeGateway()
    mcp = _Mcp(PROJECT_TOOLS)

    sources = load_build_sources(
        _session(gateway), limit=1, environ={}, mcp_client=mcp.client
    )

    assert [tool.name for tool in sources.tools] == ["search_projects"]
    assert [skill.name for skill in sources.skills] == ["daily-finance-brief"]
    assert [document.document_id for document in sources.knowledge] == ["doc-1"]
    chunk_requests = [
        request for request in gateway.requests if request.url.path.endswith("/chunks")
    ]
    assert [request.url.path for request in chunk_requests] == [
        "/api/knowledge/bases/kb-1/documents/doc-1/chunks",
        "/api/knowledge/bases/kb-1/documents/doc-1/chunks",
    ]


def test_the_report_shows_true_source_counts_when_the_limit_caps_the_build() -> None:
    gateway = _FakeGateway(servers=[STREAMABLE_SERVER])
    session = _session(gateway)
    tools = [*PROJECT_TOOLS, {"name": "list_sectors", "description": "", "inputSchema": {}}]

    sources = load_build_sources(session, limit=1, environ={}, mcp_client=_Mcp(tools).client)
    drafts = build_account_items(session, sources, limit=1)
    report = build_report("myflow-eval", sources, drafts, limit=1, dry_run=True)

    assert len(sources.tools) == 1
    assert report.source_counts == {"tool": 3, "skill": 2, "knowledge": 2, "trace": 0}
    assert report.draft_counts == {"tool": 1, "skill": 1, "knowledge": 1, "trace": 0}
    assert "tool: 1 drafts from 3 sources" in report.render()


def test_load_build_sources_warns_about_documents_without_a_readable_chunk() -> None:
    gateway = _FakeGateway(servers=[STREAMABLE_SERVER], chunks={})

    sources = load_build_sources(
        _session(gateway), environ={}, mcp_client=_Mcp(PROJECT_TOOLS).client
    )

    assert sources.knowledge == []
    assert len(sources.warnings) == 2
    assert all("chunk" in warning for warning in sources.warnings)


def test_a_full_build_turns_the_fetched_sources_into_drafts() -> None:
    gateway = _FakeGateway(servers=[STREAMABLE_SERVER])
    session = _session(gateway)

    sources = load_build_sources(
        session, environ={}, mcp_client=_Mcp(PROJECT_TOOLS).client
    )
    drafts = build_account_items(session, sources, limit=1)
    report = build_report("myflow-eval", sources, drafts, limit=1, dry_run=True)

    assert [draft.metadata["source_kind"] for draft in drafts] == ["tool", "skill", "knowledge"]
    assert report.draft_counts == {"tool": 1, "skill": 1, "knowledge": 1, "trace": 0}
    assert report.source_counts == {"tool": 2, "skill": 2, "knowledge": 2, "trace": 0}
    assert {draft.metadata["user_id"] for draft in drafts} == {HASHED_USER_ID}
    assert len({draft.id for draft in drafts}) == 3
    assert write_drafts("myflow-eval", drafts, None, dry_run=True) == 0


def test_a_large_document_requests_three_seeded_samples_and_keeps_the_longest() -> None:
    contents = [
        "A table of contents that is much longer than the sampled chunks.",
        "short",
        "The longest sampled chunk, which is the reference answer.",
        "middle",
        "A cover page that was never sampled but is the longest string.",
    ]
    gateway = _knowledge_gateway(contents)

    sources = load_build_sources(
        _session(gateway),
        environ={},
        mcp_client=_Mcp(PROJECT_TOOLS).client,
        rng=random.Random(7),
    )

    assert len(sources.knowledge) == 1
    requests = _chunk_requests(gateway)
    assert _chunk_pattern(requests) == [
        ("0", "1"),
        ("1", "1"),
        ("2", "1"),
        ("3", "1"),
    ]
    sampled_offsets = [request.url.params["offset"] for request in requests[1:]]
    assert len(set(sampled_offsets)) == 3
    document = sources.knowledge[0]
    assert (document.chunk_id, document.chunk_content) == ("doc-1:2", contents[2])


def test_a_sampled_blank_chunk_is_never_chosen_even_when_it_is_the_longest() -> None:
    contents = [" " * 200, "The useful answer.", "Short."]
    gateway = _knowledge_gateway(contents)

    sources = load_build_sources(
        _session(gateway),
        environ={},
        mcp_client=_Mcp(PROJECT_TOOLS).client,
        rng=random.Random(0),
    )

    assert len(sources.knowledge) == 1
    document = sources.knowledge[0]
    assert (document.chunk_id, document.chunk_content) == ("doc-1:1", contents[1])
    assert _chunk_pattern(_chunk_requests(gateway)) == [("0", "1"), ("0", "3")]


def test_a_two_chunk_document_fetches_both_in_one_request_and_keeps_the_longest() -> None:
    contents = ["The first chunk is short.", "The second chunk is clearly longer."]
    gateway = _knowledge_gateway(contents)

    sources = load_build_sources(
        _session(gateway),
        environ={},
        mcp_client=_Mcp(PROJECT_TOOLS).client,
        rng=random.Random(0),
    )

    assert len(sources.knowledge) == 1
    document = sources.knowledge[0]
    assert (document.chunk_id, document.chunk_content) == ("doc-1:1", contents[1])
    requests = _chunk_requests(gateway)
    assert _chunk_pattern(requests) == [("0", "1"), ("0", "2")]
    assert sum(
        request.url.params["offset"] == "0" and request.url.params["limit"] == "2"
        for request in requests
    ) == 1


def test_equal_length_sampled_chunks_tie_break_on_the_smallest_chunk_index() -> None:
    gateway = _knowledge_gateway(["same size", "SAME SIZE"])

    sources = load_build_sources(
        _session(gateway),
        environ={},
        mcp_client=_Mcp(PROJECT_TOOLS).client,
        rng=random.Random(0),
    )

    assert len(sources.knowledge) == 1
    assert sources.knowledge[0].chunk_id == "doc-1:0"


def test_a_document_whose_sampled_chunks_are_all_blank_is_skipped_once() -> None:
    gateway = _knowledge_gateway(["", "\n\t ", "   "])

    sources = load_build_sources(
        _session(gateway),
        environ={},
        mcp_client=_Mcp(PROJECT_TOOLS).client,
        rng=random.Random(0),
    )

    assert sources.knowledge == []
    assert len(sources.warnings) == 1
    assert "no readable chunk" in sources.warnings[0]


def test_the_same_seed_reproduces_the_reference_answer_and_the_item_id() -> None:
    contents = [
        "first",
        "second",
        "third",
        "the fourth chunk is the longest sampled one",
        "fifth",
    ]

    def build_once() -> DatasetItemDraft:
        gateway = _knowledge_gateway(contents)
        sources = load_build_sources(
            _session(gateway),
            environ={},
            mcp_client=_Mcp(PROJECT_TOOLS).client,
            rng=random.Random(11),
        )
        drafts = build_account_items(
            _offline_session(), _sources(knowledge=sources.knowledge)
        )
        assert len(drafts) == 1
        return drafts[0]

    first = build_once()
    second = build_once()

    assert first.expected_output["reference_answer"] == contents[3]
    assert first.expected_output["reference_answer"] == second.expected_output["reference_answer"]
    assert first.id == second.id == stable_item_id("knowledge", "doc-1", 0, HASHED_USER_ID)
    assert first.metadata["chunk_id"] == second.metadata["chunk_id"] == "doc-1:3"


def test_a_large_document_never_issues_more_than_four_chunk_requests() -> None:
    gateway = _knowledge_gateway(["one", "two", "three", "four", "five"])

    sources = load_build_sources(
        _session(gateway),
        environ={},
        mcp_client=_Mcp(PROJECT_TOOLS).client,
        rng=random.Random(7),
    )

    assert len(sources.knowledge) == 1
    assert len(_chunk_requests(gateway)) == 4


def test_build_account_items_emits_one_tool_draft_per_real_tool() -> None:
    sources = _sources(tools=[SEARCH_TOOL, PROJECT_TOOL, THIRD_TOOL])

    drafts = build_account_items(_offline_session(), sources)

    assert len(drafts) == 3
    assert [draft.metadata["source_id"] for draft in drafts] == [
        "mcp__project_db__search_projects",
        "mcp__project_db__get_project",
        "mcp__project_db__list_sectors",
    ]
    assert len({draft.id for draft in drafts}) == 3

    first = drafts[0]
    assert first.id == stable_item_id(
        "tool", "mcp__project_db__search_projects", 0, HASHED_USER_ID
    )
    assert first.input["query"] == "Use the search_projects tool: Search the project database."
    assert first.expected_output["expected_tools"] == ["mcp__project_db__search_projects"]
    assert first.expected_output["expected_calls"] == [
        {
            "tool": "mcp__project_db__search_projects",
            "args": {"sector": "solar", "created_after": ""},
            "check_keys": ["created_after", "sector"],
        }
    ]
    assert first.expected_output["forbidden_tools"] == []
    assert first.expected_output["expect_kb"] is False
    assert first.expected_output["expect_skill"] is False
    assert first.expected_output["judge_rule"]
    assert first.metadata["user_id"] == HASHED_USER_ID
    assert first.metadata["source_kind"] == "tool"
    assert first.metadata["category"] == "single_tool"
    assert first.metadata["source"] == "synthetic_from_tools"
    assert len(first.metadata["tool_schema_hash"]) == 16
    assert drafts[2].expected_output["expected_calls"] == [
        {"tool": "mcp__project_db__list_sectors", "args": {}, "check_keys": []}
    ]


def test_tool_args_recurse_into_a_required_object_only_one_level() -> None:
    sources = _sources(tools=[NESTED_TOOL])

    drafts = build_account_items(_offline_session(), sources)

    call = drafts[0].expected_output["expected_calls"][0]
    assert call["args"] == {"filter": {"sector": "solar", "window": {}}, "when": False}
    assert call["check_keys"] == ["filter", "when"]


def test_build_account_items_emits_skill_drafts_from_private_skill_metadata() -> None:
    sources = _sources(skills=[PRIVATE_SKILL])

    drafts = build_account_items(_offline_session(), sources)

    draft = drafts[0]
    assert draft.id == stable_item_id("skill", "skill-private-1", 0, HASHED_USER_ID)
    assert draft.input["query"] == (
        "Use the daily-finance-brief skill: Turn a story into a numbered daily brief."
    )
    assert draft.expected_output == {
        "expect_skill": True,
        "expected_skills": ["daily-finance-brief"],
        "skill_rules": "",
        "expect_kb": False,
        "expected_tools": [],
    }
    assert draft.metadata["user_id"] == HASHED_USER_ID
    assert draft.metadata["source_id"] == "skill-private-1"
    assert draft.metadata["source_kind"] == "skill"
    assert draft.metadata["category"] == "skill_single"
    assert draft.metadata["source"] == "synthetic_from_skill"
    assert draft.metadata["skill_commit"] == "alb2c3d"


def test_knowledge_drafts_keep_provenance_but_emit_expected_doc_ids_only() -> None:
    sources = _sources(knowledge=[POLICY_DOCUMENT])

    drafts = build_account_items(_offline_session(), sources)

    draft = drafts[0]
    assert draft.id == stable_item_id("knowledge", "doc-1", 0, HASHED_USER_ID)
    assert draft.input["query"] == "According to policy.md, what are the key points?"
    assert draft.expected_output["expect_kb"] is True
    assert draft.expected_output["expected_doc_ids"] == ["doc-1"]
    assert draft.expected_output["reference_answer"] == "Refunds take five business days."
    assert draft.expected_output["key_points"] == []
    assert draft.expected_output["expect_refusal"] is False
    assert "expected_chunk_ids" not in draft.expected_output
    assert "expected_chunk_count" not in draft.expected_output
    assert draft.metadata["document_id"] == "doc-1"
    assert draft.metadata["chunk_id"] == "doc-1:0"
    assert draft.metadata["knowledge_base_id"] == "kb-1"
    assert draft.metadata["kb_version"] == "kb-1:2"
    assert draft.metadata["source_kind"] == "knowledge"
    assert draft.metadata["category"] == "kb_single_hop"
    assert draft.metadata["source"] == "synthetic_from_docs"


def test_trace_drafts_replay_the_query_and_carry_no_expectations() -> None:
    sources = _sources(traces=[FAILED_TRACE, PLAIN_TRACE])

    drafts = build_account_items(_offline_session(), sources)

    first, second = drafts
    assert first.id == stable_item_id("trace", "trace-error", 0, HASHED_USER_ID)
    assert first.input == {"query": "Why did the refund fail?"}
    assert first.expected_output == {}
    assert first.metadata == {
        "user_id": HASHED_USER_ID,
        "source_id": "trace-error",
        "source_kind": "trace",
        "source": "online_trace",
        "trace_id": "trace-error",
        "category": "kb_single_hop",
    }
    assert second.metadata["category"] == "trace_replay"


def test_drafts_carry_the_hashed_user_id_and_never_the_raw_identity() -> None:
    sources = _sources(
        tools=[SEARCH_TOOL],
        skills=[PRIVATE_SKILL],
        knowledge=[POLICY_DOCUMENT],
        traces=[FAILED_TRACE],
    )

    drafts = build_account_items(_offline_session(), sources)

    assert {draft.metadata["user_id"] for draft in drafts} == {HASHED_USER_ID}
    encoded = json.dumps(
        [
            {"input": draft.input, "expected_output": draft.expected_output, "metadata": draft.metadata}
            for draft in drafts
        ]
    )
    assert RAW_USER_ID not in encoded
    assert PASSWORD not in encoded
    assert USERNAME not in encoded


def test_build_account_items_caps_drafts_per_kind() -> None:
    sources = _sources(
        tools=[SEARCH_TOOL, PROJECT_TOOL],
        skills=[PRIVATE_SKILL, SECOND_SKILL],
        knowledge=[POLICY_DOCUMENT, SLA_DOCUMENT],
        traces=[FAILED_TRACE, PLAIN_TRACE],
    )

    drafts = build_account_items(_offline_session(), sources, limit=1)

    assert [draft.metadata["source_kind"] for draft in drafts] == [
        "tool",
        "skill",
        "knowledge",
        "trace",
    ]


def test_stable_item_id_is_idempotent_and_scoped_to_kind_source_ordinal_and_account() -> None:
    first = stable_item_id("tool", "mcp__project_db__search_projects", 0, HASHED_USER_ID)

    assert first == stable_item_id("tool", "mcp__project_db__search_projects", 0, HASHED_USER_ID)
    assert first.startswith("tool-")
    suffix = first.split("-", 1)[1]
    assert len(suffix) == 16
    assert all(character in "0123456789abcdef" for character in suffix)
    assert first != stable_item_id("tool", "mcp__project_db__search_projects", 1, HASHED_USER_ID)
    assert first != stable_item_id("skill", "mcp__project_db__search_projects", 0, HASHED_USER_ID)
    assert first != stable_item_id(
        "tool", "mcp__project_db__search_projects", 0, "another-account-hash"
    )


def test_build_report_counts_real_sources_and_marks_the_limit_and_dry_run() -> None:
    sources = _sources(
        tools=[SEARCH_TOOL, PROJECT_TOOL, THIRD_TOOL],
        skills=[PRIVATE_SKILL],
        warnings=["skipped MCP server 'legacy_db': sse transport is not supported"],
    )
    drafts = build_account_items(_offline_session(), sources, limit=2)

    report = build_report("myflow-eval", sources, drafts, limit=2, dry_run=True)

    assert report.draft_counts == {"tool": 2, "skill": 1, "knowledge": 0, "trace": 0}
    assert report.source_counts == {"tool": 3, "skill": 1, "knowledge": 0, "trace": 0}
    text = report.render()
    assert "tool: 2 drafts from 3 sources" in text
    assert "skill: 1 drafts from 1 sources" in text
    assert "knowledge: 0 drafts from 0 sources" in text
    assert "limit: 2 per kind" in text
    assert "dry-run" in text
    assert "warning: skipped MCP server 'legacy_db'" in text


class _RecordingWriter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[DatasetItemDraft]]] = []

    def upsert_items(self, dataset_name: str, items: Any) -> None:
        self.calls.append((dataset_name, list(items)))


def test_write_drafts_skips_the_writer_for_a_dry_run() -> None:
    drafts = build_account_items(_offline_session(), _sources(tools=[SEARCH_TOOL]))
    writer = _RecordingWriter()

    assert write_drafts("myflow-eval", drafts, writer, dry_run=True) == 0
    assert writer.calls == []

    assert write_drafts("myflow-eval", drafts, writer, dry_run=False) == 1
    assert writer.calls == [("myflow-eval", drafts)]


def test_write_drafts_requires_a_writer_when_not_dry_running() -> None:
    assert write_drafts("myflow-eval", [], None, dry_run=True) == 0

    with pytest.raises(ValueError, match="DatasetWriter"):
        write_drafts("myflow-eval", [], None, dry_run=False)


def test_fetch_account_traces_keeps_only_failed_or_low_scored_traces() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": TRACE_ROWS,
                "meta": {"page": 1, "limit": 100, "totalItems": len(TRACE_ROWS), "totalPages": 1},
            },
        )

    traces = fetch_account_traces(
        HASHED_USER_ID, environ=LANGFUSE_ENVIRON, transport=httpx.MockTransport(handler)
    )

    assert traces == [
        TraceSource(trace_id="trace-error", query="Why did the refund fail?", category="kb_single_hop"),
        TraceSource(trace_id="trace-low", query="Where is the project data?", category=None),
    ]
    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == "/api/public/traces"
    assert request.url.params["userId"] == HASHED_USER_ID
    assert request.url.params["page"] == "1"
    assert request.url.params["limit"] == "100"
    assert request.headers["authorization"].startswith("Basic ")


def test_fetch_account_traces_is_empty_and_offline_without_langfuse_configuration() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no Langfuse request expected without credentials")

    assert (
        fetch_account_traces(
            HASHED_USER_ID, environ={}, transport=httpx.MockTransport(handler)
        )
        == []
    )


def test_fetch_account_traces_stops_at_the_limit() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": TRACE_ROWS,
                "meta": {"page": 1, "limit": 100, "totalItems": len(TRACE_ROWS), "totalPages": 5},
            },
        )

    traces = fetch_account_traces(
        HASHED_USER_ID,
        limit=1,
        environ=LANGFUSE_ENVIRON,
        transport=httpx.MockTransport(handler),
    )

    assert [trace.trace_id for trace in traces] == ["trace-error"]
    assert len(requests) == 1


def test_fetch_account_traces_warns_instead_of_raising_on_a_langfuse_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="langfuse down")

    warnings: list[str] = []

    traces = fetch_account_traces(
        HASHED_USER_ID,
        environ=LANGFUSE_ENVIRON,
        transport=httpx.MockTransport(handler),
        warnings=warnings,
    )

    assert traces == []
    assert len(warnings) == 1
    assert "HTTP 500" in warnings[0]


def test_a_trace_replay_asks_the_last_human_turn() -> None:
    rows = [
        {
            "id": "trace-chat",
            "userId": HASHED_USER_ID,
            "input": {
                "messages": [
                    {"type": "human", "content": "Why did the refund fail?"},
                    {"type": "ai", "content": "Because the card had expired."},
                ]
            },
            "metadata": {},
            "level": "ERROR",
        },
        {
            "id": "trace-roleless",
            "userId": HASHED_USER_ID,
            "input": [{"content": "first question"}, {"content": "second question"}],
            "metadata": {},
            "level": "ERROR",
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": rows, "meta": {"page": 1, "limit": 100, "totalItems": 2, "totalPages": 1}},
        )

    traces = fetch_account_traces(
        HASHED_USER_ID, environ=LANGFUSE_ENVIRON, transport=httpx.MockTransport(handler)
    )

    assert [trace.query for trace in traces] == [
        "Why did the refund fail?",
        "second question",
    ]

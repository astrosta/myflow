import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ToolApiError,
  claimTool,
  createTool,
  deleteTool,
  getTool,
  getToolsStatus,
  listTools,
  previewToolSearch,
  reindexTools,
  setToolEnabled,
  updateTool,
} from "./tools";

/**
 * 契约测试：只钉住会跨端漂移的东西（路径、方法、body、错误体、掩码透传）。
 * 页面行为由组件的 dom 测试覆盖。
 */

const TOOL = {
  id: "tool-1",
  name: "gitlab_search",
  display_name: "GitLab Search",
  description: "搜索 GitLab 项目",
  url: "https://gitlab.example.com/api",
  enabled: true,
  reachable: true,
  integration_type: "REST",
  request_type: "SSE",
  tags: ["gitlab", "search"],
  visibility: "public",
  created_at: "2026-11-01T00:00:00Z",
  updated_at: "2026-11-02T00:00:00Z",
  shadowed_by: null,
  index_status: "indexed",
  owned: true,
  claimable: false,
  can_edit: true,
};

const DETAIL = {
  ...TOOL,
  headers: { Authorization: "****", "X-Team": "platform" },
  input_schema: { type: "object", properties: { q: { type: "string" } } },
  annotations: { readOnlyHint: true },
};

const STATUS = {
  enabled: true,
  reachable: true,
  auth_ok: true,
  admin_events: "connected",
  daily: "running",
  sync: "ready",
  index: "degraded",
  channels: { keyword: "ready", embedding: "ready", rerank: "disabled" },
  indexed_tools: 12,
  total_tools: 13,
  last_sync_at: "2026-11-06T10:00:00Z",
  last_sync_reason: "admin",
  last_sync_duration_ms: 42,
  last_error: null,
  last_retry_at: "2026-11-06T10:05:00Z",
  last_retry_tool: "gitlab_search",
  last_retry_result: "refreshed",
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubFetch(response: Response): ReturnType<typeof vi.fn> {
  const mock = vi.fn(async () => response);
  vi.stubGlobal("fetch", mock);
  return mock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("tools API contract", () => {
  it("listTools maps the page and serializes filters without dropping false", async () => {
    const fetchMock = stubFetch(
      jsonResponse({ tools: [TOOL], count: 1, next_cursor: "50" }),
    );

    const page = await listTools({
      q: "git lab",
      enabled: false,
      integration_type: "REST",
      cursor: "0",
      limit: 20,
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const parsed = new URL(String(url), "http://localhost");
    expect(parsed.pathname).toBe("/api/tools");
    expect(parsed.searchParams.get("q")).toBe("git lab");
    expect(parsed.searchParams.get("enabled")).toBe("false");
    expect(parsed.searchParams.get("integration_type")).toBe("REST");
    expect(parsed.searchParams.get("cursor")).toBe("0");
    expect(parsed.searchParams.get("limit")).toBe("20");
    expect(init.method).toBeUndefined();
    expect(init.credentials).toBe("same-origin");
    expect(page.count).toBe(1);
    expect(page.next_cursor).toBe("50");
    expect(page.tools[0]?.name).toBe("gitlab_search");
  });

  it("listTools defaults the page shape instead of returning undefined fields", async () => {
    stubFetch(jsonResponse({}));

    const page = await listTools();

    expect(page).toEqual({ tools: [], count: 0, next_cursor: null });
  });

  it("getTool encodes the id and keeps masked headers as sent by the gateway", async () => {
    const fetchMock = stubFetch(jsonResponse(DETAIL));

    const tool = await getTool("tool/1 个");

    const [url] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/tools/tool%2F1%20%E4%B8%AA");
    expect(tool.headers.Authorization).toBe("****");
    expect(tool.headers["X-Team"]).toBe("platform");
    expect(tool.input_schema.type).toBe("object");
    expect(tool.annotations.readOnlyHint).toBe(true);
  });

  it("createTool posts JSON through apiFetch", async () => {
    const fetchMock = stubFetch(jsonResponse(DETAIL, 201));

    const tool = await createTool({
      name: "gitlab_search",
      description: "搜索 GitLab 项目",
      integration_type: "REST",
      request_type: "SSE",
      url: "https://gitlab.example.com/api",
      headers: { Authorization: "Bearer secret" },
      tags: ["gitlab"],
      enabled: false,
    });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/tools");
    expect(init.method).toBe("POST");
    const headers = init.headers as Headers;
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(JSON.parse(String(init.body))).toMatchObject({
      name: "gitlab_search",
      integration_type: "REST",
      request_type: "SSE",
      enabled: false,
    });
    expect(tool.id).toBe("tool-1");
  });

  it("updateTool puts the full document and round-trips masked headers", async () => {
    const fetchMock = stubFetch(jsonResponse(DETAIL));

    await updateTool("tool-1", {
      name: "gitlab_search",
      description: "搜索 GitLab 项目",
      integration_type: "REST",
      request_type: "SSE",
      headers: { Authorization: "****" },
      enabled: true,
    });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/tools/tool-1");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(String(init.body)).headers).toEqual({ Authorization: "****" });
  });

  it("claimTool posts to the claim route", async () => {
    const fetchMock = stubFetch(jsonResponse(DETAIL));

    const tool = await claimTool("tool-1");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/tools/tool-1/claim");
    expect(init.method).toBe("POST");
    expect(tool.owned).toBe(true);
  });

  it("setToolEnabled posts to the state route", async () => {
    const fetchMock = stubFetch(jsonResponse({ ...DETAIL, enabled: false }));

    const tool = await setToolEnabled("tool-1", false);

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/tools/tool-1/state");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ enabled: false });
    expect(tool.enabled).toBe(false);
  });

  it("deleteTool resolves on 204 with no JSON body", async () => {
    const fetchMock = stubFetch(new Response(null, { status: 204 }));

    await expect(deleteTool("tool-1")).resolves.toBeUndefined();

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/tools/tool-1");
    expect(init.method).toBe("DELETE");
  });

  it("getToolsStatus surfaces every field the status bar badges", async () => {
    const fetchMock = stubFetch(jsonResponse(STATUS));

    const status = await getToolsStatus();

    const [url] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/tools/status");
    expect(status).toMatchObject({
      enabled: true,
      admin_events: "connected",
      daily: "running",
      sync: "ready",
      index: "degraded",
      indexed_tools: 12,
      total_tools: 13,
      last_sync_duration_ms: 42,
      last_retry_tool: "gitlab_search",
    });
    expect(status.channels).toEqual({ keyword: "ready", embedding: "ready", rerank: "disabled" });
  });

  it("getToolsStatus defaults the fields a disabled gateway omits", async () => {
    stubFetch(jsonResponse({ enabled: false }));

    const status = await getToolsStatus();

    expect(status).toMatchObject({
      daily: "disabled",
      sync: "disabled",
      index: "disabled",
      last_sync_duration_ms: null,
      last_retry_at: null,
      last_retry_tool: null,
      last_retry_result: null,
    });
  });

  it("reindexTools posts and returns sync counters", async () => {
    const fetchMock = stubFetch(
      jsonResponse({ scanned: 13, changed: 2, upserted: 2, deleted: 0, duration_ms: 42, error: null }),
    );

    const stats = await reindexTools();

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/tools/reindex");
    expect(init.method).toBe("POST");
    expect(stats.upserted).toBe(2);
    expect(stats.error).toBeNull();
  });

  it("previewToolSearch keeps the catalog fields a search card carries", async () => {
    const fetchMock = stubFetch(
      jsonResponse({
        query: "搜 gitlab 项目",
        results: [
          {
            tool_id: "tool-1",
            tool_name: "gitlab_search",
            call_name: "cf__gitlab_search",
            description: "搜索 GitLab 项目",
            tags: ["gitlab"],
            parameters: ["q", "per_page"],
            score: 0.91,
            schema_truncated: false,
          },
        ],
        retrieval: {
          index: "milvus",
          channels: ["embedding", "keyword"],
          candidates: 8,
          reranked: true,
          warnings: ["reranker unavailable"],
        },
      }),
    );

    const preview = await previewToolSearch("搜 gitlab 项目", 3);

    const parsed = new URL(String(fetchMock.mock.calls[0]?.[0]), "http://localhost");
    expect(parsed.pathname).toBe("/api/tools/search");
    expect(parsed.searchParams.get("q")).toBe("搜 gitlab 项目");
    expect(parsed.searchParams.get("limit")).toBe("3");
    expect(preview.results[0]).toEqual({
      tool_id: "tool-1",
      tool_name: "gitlab_search",
      call_name: "cf__gitlab_search",
      description: "搜索 GitLab 项目",
      tags: ["gitlab"],
      parameters: ["q", "per_page"],
      score: 0.91,
      schema_truncated: false,
    });
    expect(preview.retrieval).toEqual({
      index: "milvus",
      channels: ["embedding", "keyword"],
      candidates: 8,
      reranked: true,
      warnings: ["reranker unavailable"],
    });
  });

  it("previewToolSearch defaults a retrieval failure instead of hiding it", async () => {
    stubFetch(jsonResponse({ query: "gitlab" }));

    const preview = await previewToolSearch("gitlab");

    expect(preview.results).toEqual([]);
    expect(preview.retrieval).toEqual({
      index: "unavailable",
      channels: [],
      candidates: 0,
      reranked: false,
      warnings: [],
    });
  });

  it("extracts the first errors[] entry on a 422", async () => {
    stubFetch(
      jsonResponse(
        {
          code: "request_validation_error",
          detail: "Request validation failed",
          trace_id: "trace-1",
          errors: [{ loc: ["body", "input_schema"], msg: "must be an object" }],
        },
        422,
      ),
    );

    await expect(createTool({ name: "x", description: "" })).rejects.toMatchObject({
      name: "ToolApiError",
      status: 422,
      code: "request_validation_error",
      detail: "input_schema: must be an object",
      traceId: "trace-1",
    });
  });

  it("maps 404 and 503 to ToolApiError with the gateway code", async () => {
    stubFetch(
      jsonResponse({ code: "tool_not_found", detail: "tool not found", trace_id: "t1" }, 404),
    );
    const missing = getTool("missing");
    await expect(missing).rejects.toBeInstanceOf(ToolApiError);
    await expect(missing).rejects.toMatchObject({
      status: 404,
      code: "tool_not_found",
      detail: "tool not found",
      traceId: "t1",
    });

    stubFetch(
      jsonResponse({ code: "contextforge_unavailable", detail: "admin unreachable" }, 503),
    );
    await expect(getToolsStatus()).rejects.toMatchObject({
      status: 503,
      code: "contextforge_unavailable",
      detail: "admin unreachable",
    });
  });
});

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  McpServerApiError,
  createMcpServer,
  deleteMcpServer,
  getMcpServer,
  getMcpServerStatus,
  listMcpServerTools,
  listMcpServers,
  refreshMcpServer,
  reindexMcpServers,
  setMcpServerEnabled,
  updateMcpServer,
} from "./mcp-servers";

/**
 * 契约测试：只钉住会跨端漂移的东西（路径、方法、body、错误体、掩码透传）。
 *
 * 后端 PUT 的模型是 extra="forbid"，name/enabled/显式 null 的 verify_tls 都会 422，
 * 所以这里逐条钉住"客户端不往 PUT body 里放这些键"——页面行为由 dom 测试覆盖。
 */

const SUMMARY = {
  id: "srv-1",
  name: "anysearch",
  display_name: "AnySearch",
  description: "实时搜索",
  url: "https://api.anysearch.com/mcp",
  url_has_query: true,
  transport: "streamable_http",
  verify_tls: true,
  enabled: true,
  status: "active",
  reachable: true,
  tool_count: 3,
  last_error: null,
  last_connected_at: "2026-11-01T00:00:00Z",
  last_tools_sync_at: "2026-11-02T00:00:00Z",
  created_at: "2026-11-01T00:00:00Z",
  updated_at: "2026-11-02T00:00:00Z",
};

const DETAIL = {
  ...SUMMARY,
  headers: { Authorization: "****", "X-Anysearch-Client": "mcp/1.0.0" },
};

const TOOL = {
  name: "search",
  call_name: "mcp__anysearch__search",
  display_name: "Search",
  description: "搜索",
  parameters: ["q"],
  callable: true,
  enabled: true,
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

function callBody(mock: ReturnType<typeof vi.fn>, index = 0): Record<string, unknown> {
  const init = mock.mock.calls[index]?.[1] as RequestInit;
  return JSON.parse(String(init.body)) as Record<string, unknown>;
}

function callUrl(mock: ReturnType<typeof vi.fn>, index = 0): { pathname: string } {
  const url = mock.mock.calls[index]?.[0] as string;
  return new URL(String(url), "http://localhost");
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("mcp-servers API contract", () => {
  it("creates a streamable server with headers", async () => {
    const mockedFetch = stubFetch(jsonResponse(DETAIL, 201));

    const created = await createMcpServer({
      name: "anysearch",
      url: "https://api.anysearch.com/mcp",
      transport: "streamable_http",
      headers: { Authorization: "Bearer x" },
    });

    expect(mockedFetch).toHaveBeenCalledTimes(1);
    expect(callUrl(mockedFetch).pathname).toBe("/api/mcp-servers");
    const init = mockedFetch.mock.calls[0]?.[1] as RequestInit;
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("same-origin");
    expect(callBody(mockedFetch)).toMatchObject({
      name: "anysearch",
      transport: "streamable_http",
      headers: { Authorization: "Bearer x" },
    });
    expect(created.headers.Authorization).toBe("****");
    expect(created.tool_count).toBe(3);
  });

  it("create sends an explicit false enabled flag instead of dropping it", async () => {
    const mockedFetch = stubFetch(jsonResponse(DETAIL, 201));

    await createMcpServer({
      name: "anysearch",
      url: "https://api.anysearch.com/mcp",
      verify_tls: false,
      enabled: false,
    });

    const body = callBody(mockedFetch);
    expect(body.enabled).toBe(false);
    expect(body.verify_tls).toBe(false);
    expect(body.transport).toBeUndefined();
  });

  it("update never sends name, enabled or a null verify_tls", async () => {
    const mockedFetch = stubFetch(jsonResponse(DETAIL));

    await updateMcpServer("srv-1", {
      display_name: "AnySearch 2",
      description: "",
      url: "https://api.anysearch.com/mcp",
      headers: { Authorization: "****" },
    });

    expect(callUrl(mockedFetch).pathname).toBe("/api/mcp-servers/srv-1");
    const init = mockedFetch.mock.calls[0]?.[1] as RequestInit;
    expect(init.method).toBe("PUT");
    const body = callBody(mockedFetch);
    expect(body).toEqual({
      display_name: "AnySearch 2",
      description: "",
      url: "https://api.anysearch.com/mcp",
      headers: { Authorization: "****" },
    });
    expect(body.name).toBeUndefined();
    expect(body.enabled).toBeUndefined();
    expect(body.verify_tls).toBeUndefined();
  });

  it("update sends verify_tls only when the form changed it", async () => {
    const mockedFetch = stubFetch(jsonResponse(DETAIL));

    await updateMcpServer("srv-1", { verify_tls: false });

    expect(callBody(mockedFetch)).toEqual({ verify_tls: false });
  });

  it("getMcpServer encodes the id and keeps the masked headers", async () => {
    const mockedFetch = stubFetch(jsonResponse(DETAIL));

    const detail = await getMcpServer("srv/1");

    expect(callUrl(mockedFetch).pathname).toBe("/api/mcp-servers/srv%2F1");
    expect(detail.headers).toEqual({ Authorization: "****", "X-Anysearch-Client": "mcp/1.0.0" });
    expect(detail.url_has_query).toBe(true);
  });

  it("listMcpServers maps the page shape and defaults missing fields", async () => {
    const mockedFetch = stubFetch(
      jsonResponse({ servers: [{ id: "srv-1", name: "anysearch", url: "https://x.test/mcp" }], count: 1 }),
    );

    const page = await listMcpServers();

    expect(callUrl(mockedFetch).pathname).toBe("/api/mcp-servers");
    expect((mockedFetch.mock.calls[0]?.[1] as RequestInit).method).toBeUndefined();
    expect(page.count).toBe(1);
    expect(page.servers[0]).toMatchObject({
      id: "srv-1",
      name: "anysearch",
      transport: "streamable_http",
      enabled: true,
      reachable: false,
      tool_count: 0,
      url_has_query: false,
    });
    expect(page.servers[0]).not.toHaveProperty("headers");
  });

  it("strips a query string out of the URL and flags it", async () => {
    stubFetch(
      jsonResponse({
        servers: [
          { id: "srv-1", name: "anysearch", url: "https://x.test/mcp?token=abc&x=1", url_has_query: false },
        ],
        count: 1,
      }),
    );

    const page = await listMcpServers();

    // 查询串永远不进前端状态：即使网关漏了 url_has_query，客户端也不留明文参数。
    expect(page.servers[0]?.url).toBe("https://x.test/mcp");
    expect(page.servers[0]?.url_has_query).toBe(true);
  });

  it("deleteMcpServer resolves on 204", async () => {
    const mockedFetch = stubFetch(new Response(null, { status: 204 }));

    await expect(deleteMcpServer("srv-1")).resolves.toBeUndefined();

    expect(callUrl(mockedFetch).pathname).toBe("/api/mcp-servers/srv-1");
    expect((mockedFetch.mock.calls[0]?.[1] as RequestInit).method).toBe("DELETE");
  });

  it("setMcpServerEnabled posts the flag to /state", async () => {
    const mockedFetch = stubFetch(jsonResponse({ ...DETAIL, enabled: false }));

    const detail = await setMcpServerEnabled("srv-1", false);

    expect(callUrl(mockedFetch).pathname).toBe("/api/mcp-servers/srv-1/state");
    expect(callBody(mockedFetch)).toEqual({ enabled: false });
    expect(detail.enabled).toBe(false);
  });

  it("refreshMcpServer posts to /refresh", async () => {
    const mockedFetch = stubFetch(jsonResponse(DETAIL));

    await refreshMcpServer("srv-1");

    expect(callUrl(mockedFetch).pathname).toBe("/api/mcp-servers/srv-1/refresh");
    expect((mockedFetch.mock.calls[0]?.[1] as RequestInit).method).toBe("POST");
  });

  it("listMcpServerTools reads the read-only projection", async () => {
    const mockedFetch = stubFetch(jsonResponse({ tools: [TOOL], count: 1 }));

    const page = await listMcpServerTools("srv-1");

    expect(callUrl(mockedFetch).pathname).toBe("/api/mcp-servers/srv-1/tools");
    expect(page.count).toBe(1);
    expect(page.tools[0]).toEqual(TOOL);
  });

  it("listMcpServerTools defaults a sparse row instead of returning undefined fields", async () => {
    stubFetch(jsonResponse({ tools: [{ name: "search" }] }));

    const page = await listMcpServerTools("srv-1");

    expect(page.tools[0]).toEqual({
      name: "search",
      call_name: "",
      display_name: null,
      description: "",
      parameters: [],
      callable: false,
      enabled: true,
    });
  });

  it("getMcpServerStatus hits the static /status route and fills defaults", async () => {
    const mockedFetch = stubFetch(
      jsonResponse({ servers_total: 2, servers_active: 1, tools_total: 4, indexed_tools: 3 }),
    );

    const status = await getMcpServerStatus();

    expect(callUrl(mockedFetch).pathname).toBe("/api/mcp-servers/status");
    expect(status).toMatchObject({
      servers_total: 2,
      servers_active: 1,
      servers_pending: 0,
      servers_error: 0,
      servers_disabled: 0,
      servers_reachable: 0,
      tools_total: 4,
      indexed_tools: 3,
      last_sync_at: null,
      last_error: null,
    });
  });

  it("reindexMcpServers posts to /reindex and keeps the failed counter", async () => {
    const mockedFetch = stubFetch(
      jsonResponse({ scanned: 4, changed: 2, upserted: 2, deleted: 0, failed: 1, duration_ms: 9, error: null }),
    );

    const stats = await reindexMcpServers();

    expect(callUrl(mockedFetch).pathname).toBe("/api/mcp-servers/reindex");
    expect((mockedFetch.mock.calls[0]?.[1] as RequestInit).method).toBe("POST");
    expect(stats).toEqual({
      scanned: 4,
      changed: 2,
      upserted: 2,
      deleted: 0,
      failed: 1,
      duration_ms: 9,
      error: null,
    });
  });

  it("surfaces the gateway error body as McpServerApiError", async () => {
    stubFetch(
      jsonResponse(
        {
          code: "mcp_server_name_taken",
          detail: "Request validation failed",
          trace_id: "trace-9",
          errors: [{ loc: ["body", "url"], msg: "url must use https" }],
        },
        422,
      ),
    );

    await expect(
      createMcpServer({ name: "anysearch", url: "http://insecure.test/mcp" }),
    ).rejects.toMatchObject({
      name: "McpServerApiError",
      status: 422,
      code: "mcp_server_name_taken",
      detail: "url: url must use https",
      traceId: "trace-9",
    });
  });

  it("falls back to HTTP status when the error page is not JSON", async () => {
    stubFetch(new Response("<html>bad gateway</html>", { status: 502 }));

    const error = await listMcpServers().catch((cause: unknown) => cause);

    expect(error).toBeInstanceOf(McpServerApiError);
    expect((error as McpServerApiError).detail).toBe("HTTP 502");
    expect((error as McpServerApiError).code).toBeNull();
  });
});
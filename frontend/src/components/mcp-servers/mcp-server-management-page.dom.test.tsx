import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  McpServerApiError,
  createMcpServer,
  deleteMcpServer,
  getMcpServerStatus,
  listMcpServerTools,
  listMcpServers,
  refreshMcpServer,
  reindexMcpServers,
  setMcpServerEnabled,
  type McpServerStatus,
  type McpServerSummary,
  type McpServerTool,
} from "@/core/api/mcp-servers";

import { McpServerManagementPage } from "./mcp-server-management-page";

/**
 * 「MCP 服务」页：列表 + 状态 + 只读工具区 + 启停/刷新/删除/重建索引。
 *
 * 这页曾经是旧网关的「工具管理」：新页面里**不允许**再出现任何工具写操作
 * （新建/编辑/删除/认领），工具只是服务器的只读投影——这也是下面单独钉一条的原因。
 */

vi.mock("@/core/api/mcp-servers", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/core/api/mcp-servers")>();
  return {
    ...actual,
    listMcpServers: vi.fn(),
    getMcpServer: vi.fn(),
    createMcpServer: vi.fn(),
    updateMcpServer: vi.fn(),
    deleteMcpServer: vi.fn(),
    setMcpServerEnabled: vi.fn(),
    refreshMcpServer: vi.fn(),
    listMcpServerTools: vi.fn(),
    getMcpServerStatus: vi.fn(),
    reindexMcpServers: vi.fn(),
  };
});

const mockedList = vi.mocked(listMcpServers);
const mockedCreate = vi.mocked(createMcpServer);
const mockedStatus = vi.mocked(getMcpServerStatus);
const mockedDelete = vi.mocked(deleteMcpServer);
const mockedSetEnabled = vi.mocked(setMcpServerEnabled);
const mockedRefresh = vi.mocked(refreshMcpServer);
const mockedTools = vi.mocked(listMcpServerTools);
const mockedReindex = vi.mocked(reindexMcpServers);

function makeServer(overrides: Partial<McpServerSummary> = {}): McpServerSummary {
  return {
    id: "srv-1",
    name: "anysearch",
    display_name: "AnySearch",
    description: "实时搜索",
    url: "https://api.anysearch.com/mcp",
    url_has_query: false,
    transport: "streamable_http",
    verify_tls: true,
    enabled: true,
    status: "active",
    reachable: true,
    tool_count: 2,
    last_error: null,
    last_connected_at: "2026-11-01T00:00:00Z",
    last_tools_sync_at: "2026-11-02T00:00:00Z",
    created_at: "2026-11-01T00:00:00Z",
    updated_at: "2026-11-02T00:00:00Z",
    ...overrides,
  };
}

function makeStatus(overrides: Partial<McpServerStatus> = {}): McpServerStatus {
  return {
    enabled: true,
    servers_total: 2,
    servers_active: 1,
    servers_pending: 1,
    servers_error: 0,
    servers_disabled: 0,
    servers_reachable: 1,
    tools_total: 4,
    indexed_tools: 3,
    last_sync_at: "2026-11-02T00:00:00Z",
    last_error: null,
    ...overrides,
  };
}

function makeTool(overrides: Partial<McpServerTool> = {}): McpServerTool {
  return {
    name: "search",
    call_name: "mcp__anysearch__search",
    display_name: "Search",
    description: "搜索网络",
    parameters: ["q"],
    callable: true,
    enabled: true,
    ...overrides,
  };
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  const view = render(
    <QueryClientProvider client={client}>
      <McpServerManagementPage />
    </QueryClientProvider>,
  );
  return { ...view, client };
}

beforeEach(() => {
  mockedList.mockResolvedValue({ servers: [makeServer()], count: 1 });
  mockedCreate.mockResolvedValue({
    ...makeServer({ id: "srv-2", name: "anysearch-2" }),
    headers: {},
  });
  mockedStatus.mockResolvedValue(makeStatus());
  mockedTools.mockResolvedValue({ tools: [makeTool()], count: 1 });
  mockedDelete.mockResolvedValue(undefined);
  mockedSetEnabled.mockResolvedValue({
    ...makeServer({ enabled: false, status: "disabled" }),
    headers: {},
  });
  mockedRefresh.mockResolvedValue({ ...makeServer({ tool_count: 3 }), headers: {} });
  mockedReindex.mockResolvedValue({
    scanned: 4,
    changed: 1,
    upserted: 1,
    deleted: 0,
    failed: 0,
    duration_ms: 12,
    error: null,
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("MCP 服务列表", () => {
  it("列出服务状态、可达性与工具数量，并画出汇总状态", async () => {
    const view = renderPage();

    expect(await view.findByRole("heading", { name: "MCP 服务" })).toBeTruthy();
    const card = await view.findByRole("article", { name: "anysearch" });
    expect(within(card).getByText("AnySearch")).toBeTruthy();
    expect(within(card).getByText("实时搜索")).toBeTruthy();
    expect(within(card).getByText("https://api.anysearch.com/mcp")).toBeTruthy();
    expect(within(card).getByText("STREAMABLEHTTP")).toBeTruthy();
    expect(within(card).getByText("可达")).toBeTruthy();
    expect(within(card).getByText("工具 2")).toBeTruthy();
    expect(within(card).getByText("active")).toBeTruthy();

    const status = await view.findByRole("region", { name: "MCP 服务状态" });
    expect(within(status).getByText(/2 个服务/)).toBeTruthy();
    expect(within(status).getByText(/4 个工具，已索引 3 个/)).toBeTruthy();
  });

  it("last_error 直接展示出来，并标出不可达", async () => {
    mockedList.mockResolvedValue({
      servers: [
        makeServer({
          status: "error",
          reachable: false,
          last_error: "initialize failed: 401 Unauthorized",
        }),
      ],
      count: 1,
    });
    const view = renderPage();

    const card = await view.findByRole("article", { name: "anysearch" });
    expect(within(card).getByText("initialize failed: 401 Unauthorized")).toBeTruthy();
    expect(within(card).getByText("不可达")).toBeTruthy();
  });

  it("隐藏的查询串不装作不存在：列表给出提示", async () => {
    mockedList.mockResolvedValue({ servers: [makeServer({ url_has_query: true })], count: 1 });
    const view = renderPage();

    const card = await view.findByRole("article", { name: "anysearch" });
    expect(within(card).getByText(/查询串/)).toBeTruthy();
  });

  it("没有服务时给出引导，而不是一片空白", async () => {
    mockedList.mockResolvedValue({ servers: [], count: 0 });
    const view = renderPage();

    expect(await view.findByText("还没有 MCP 服务")).toBeTruthy();
    expect(view.getByRole("button", { name: "新建服务" })).toBeTruthy();
  });

  it("状态还没回来时不说成「未启用」", async () => {
    mockedStatus.mockReturnValue(new Promise<McpServerStatus>(() => {}));
    const view = renderPage();

    const status = await view.findByRole("region", { name: "MCP 服务状态" });
    expect(within(status).getByText("MCP 状态读取中")).toBeTruthy();
    expect(within(status).queryByText("MCP 未启用")).toBeNull();
  });

  it("列表请求失败时显示错误文案", async () => {
    mockedList.mockRejectedValue(new Error("网关 502"));
    const view = renderPage();

    expect((await view.findByRole("alert")).textContent).toContain("网关 502");
  });
});

describe("MCP 服务操作", () => {
  it("启停走 state 端点，而不是 PUT", async () => {
    const view = renderPage();
    const card = await view.findByRole("article", { name: "anysearch" });

    fireEvent.click(within(card).getByRole("switch", { name: "停用 anysearch" }));

    await waitFor(() => expect(mockedSetEnabled).toHaveBeenCalledWith("srv-1", false));
  });

  it("启停开关的标签带上服务器名，多个服务之间读屏也能区分", async () => {
    mockedList.mockResolvedValue({
      servers: [
        makeServer(),
        makeServer({ id: "srv-2", name: "another", display_name: "Another", enabled: false }),
      ],
      count: 2,
    });
    const view = renderPage();

    await view.findByRole("article", { name: "anysearch" });
    // 逐个叫名字，而不是二十个一模一样的"停用服务"
    expect(view.getByRole("switch", { name: "停用 anysearch" })).toBeTruthy();
    expect(view.getByRole("switch", { name: "启用 another" })).toBeTruthy();
    expect(view.queryByRole("switch", { name: "停用服务" })).toBeNull();
  });

  it("单个服务刷新后失效工具与状态", async () => {
    const view = renderPage();
    const card = await view.findByRole("article", { name: "anysearch" });
    const invalidate = vi.spyOn(view.client, "invalidateQueries");

    fireEvent.click(within(card).getByRole("button", { name: "刷新 anysearch" }));

    await waitFor(() => expect(mockedRefresh).toHaveBeenCalledWith("srv-1"));
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ["mcp-servers", "srv-1", "tools"] }));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["mcp-servers", "status"] });
  });

  it("重建索引会失效状态与工具缓存，并回报统计", async () => {
    const view = renderPage();
    await view.findByRole("article", { name: "anysearch" });
    const invalidate = vi.spyOn(view.client, "invalidateQueries");

    fireEvent.click(view.getByRole("button", { name: "重建索引" }));

    await waitFor(() => expect(mockedReindex).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ["mcp-servers", "status"] }));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["mcp-servers"] });
    expect(await view.findByRole("status")).toHaveProperty("textContent", "扫描 4，变更 1，写入 1，删除 0");
  });

  it("删除要先确认，确认后调用 DELETE 并失效列表", async () => {
    const view = renderPage();
    const card = await view.findByRole("article", { name: "anysearch" });
    const invalidate = vi.spyOn(view.client, "invalidateQueries");

    fireEvent.click(within(card).getByRole("button", { name: "删除 anysearch" }));
    const dialog = await view.findByRole("dialog", { name: "删除 MCP 服务" });
    expect(mockedDelete).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "确认删除" }));

    await waitFor(() => expect(mockedDelete).toHaveBeenCalledWith("srv-1"));
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ["mcp-servers"] }));
  });

  it("删除失败时错误显示在确认框里（遮罩后面没人看得见）", async () => {
    mockedDelete.mockRejectedValueOnce(
      new McpServerApiError(409, "mcp_server_busy", "服务器正在同步，请稍后再试", "trace-7"),
    );
    const view = renderPage();
    const card = await view.findByRole("article", { name: "anysearch" });

    fireEvent.click(within(card).getByRole("button", { name: "删除 anysearch" }));
    const dialog = await view.findByRole("dialog", { name: "删除 MCP 服务" });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认删除" }));

    const alert = await within(dialog).findByRole("alert");
    expect(alert.textContent).toBe("服务器正在同步，请稍后再试");
    // 框还开着，用户可以直接重试或取消
    expect(view.getByRole("dialog", { name: "删除 MCP 服务" })).toBeTruthy();
  });

  it("删除确认框聚焦确认按钮，Escape 取消后焦点回到删除按钮", async () => {
    const view = renderPage();
    const card = await view.findByRole("article", { name: "anysearch" });
    const deleteButton = within(card).getByRole("button", { name: "删除 anysearch" });

    deleteButton.focus();
    fireEvent.click(deleteButton);
    const dialog = await view.findByRole("dialog", { name: "删除 MCP 服务" });
    const confirm = within(dialog).getByRole("button", { name: "确认删除" });
    await waitFor(() => expect(document.activeElement).toBe(confirm));

    fireEvent.keyDown(document, { key: "Escape" });

    await waitFor(() => expect(view.queryByRole("dialog", { name: "删除 MCP 服务" })).toBeNull());
    expect(mockedDelete).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(deleteButton);
  });
});

describe("工具区", () => {
  it("展开后只读展示工具，没有任何新建/编辑/删除/认领入口", async () => {
    const view = renderPage();
    const card = await view.findByRole("article", { name: "anysearch" });

    fireEvent.click(within(card).getByRole("button", { name: "查看 anysearch 的工具" }));

    const tools = await view.findByRole("region", { name: "anysearch 的工具" });
    expect(await within(tools).findByText("mcp__anysearch__search")).toBeTruthy();
    expect(within(tools).getByText("搜索网络")).toBeTruthy();
    expect(mockedTools).toHaveBeenCalledWith("srv-1");

    for (const forbidden of ["新建工具", "编辑工具", "删除工具", "认领工具", "认领"]) {
      expect(view.queryByText(forbidden)).toBeNull();
    }
    expect(view.queryByRole("button", { name: /认领/ })).toBeNull();
    expect(view.queryByRole("button", { name: /编辑工具/ })).toBeNull();
  });

  it("工具为空时说明服务器还没同步出工具", async () => {
    mockedTools.mockResolvedValue({ tools: [], count: 0 });
    const view = renderPage();
    const card = await view.findByRole("article", { name: "anysearch" });

    fireEvent.click(within(card).getByRole("button", { name: "查看 anysearch 的工具" }));

    expect(await view.findByText("这个服务器还没有同步出工具")).toBeTruthy();
  });
});

describe("编辑器入口", () => {
  it("新建服务按钮打开新建对话框", async () => {
    const view = renderPage();
    await view.findByRole("article", { name: "anysearch" });

    fireEvent.click(view.getByRole("button", { name: "新建服务" }));

    expect(await view.findByRole("dialog", { name: "新建 MCP 服务" })).toBeTruthy();
  });

  it("新建成功后失效列表与详情，并把对话框关掉", async () => {
    const view = renderPage();
    await view.findByRole("article", { name: "anysearch" });
    const invalidate = vi.spyOn(view.client, "invalidateQueries");

    fireEvent.click(view.getByRole("button", { name: "新建服务" }));
    const dialog = await view.findByRole("dialog", { name: "新建 MCP 服务" });
    fireEvent.change(within(dialog).getByLabelText("服务名称"), {
      target: { value: "anysearch-2" },
    });
    fireEvent.change(within(dialog).getByLabelText("服务地址"), {
      target: { value: "https://api.anysearch.com/mcp" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "创建" }));

    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ["mcp-servers", "srv-2"] }),
    );
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["mcp-servers"], exact: true });
    await waitFor(() =>
      expect(view.queryByRole("dialog", { name: "新建 MCP 服务" })).toBeNull(),
    );
  });

  it("编辑按钮打开已经加载好详情的对话框", async () => {
    const view = renderPage();
    const card = await view.findByRole("article", { name: "anysearch" });

    fireEvent.click(within(card).getByRole("button", { name: "编辑 anysearch" }));

    expect(await view.findByRole("dialog", { name: "编辑 MCP 服务" })).toBeTruthy();
  });
});

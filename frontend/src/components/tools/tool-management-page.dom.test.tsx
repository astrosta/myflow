import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  claimTool,
  createTool,
  deleteTool,
  getToolsStatus,
  listTools,
  previewToolSearch,
  reindexTools,
  setToolEnabled,
  type ContextForgeTool,
  type ToolStatus,
} from "@/core/api/tools";

import { ToolManagementPage } from "./tool-management-page";

/**
 * 「工具管理」页：所有登录用户可用。API 换成 vi.fn()，
 * 断言加载/空/错误态、状态栏、CRUD 与检索预览的调用。
 */

vi.mock("@/core/api/tools", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/core/api/tools")>();
  return {
    ...actual,
    listTools: vi.fn(),
    claimTool: vi.fn(),
    createTool: vi.fn(),
    updateTool: vi.fn(),
    deleteTool: vi.fn(),
    setToolEnabled: vi.fn(),
    getToolsStatus: vi.fn(),
    reindexTools: vi.fn(),
    previewToolSearch: vi.fn(),
  };
});

const mockedList = vi.mocked(listTools);
const mockedStatus = vi.mocked(getToolsStatus);
const mockedClaim = vi.mocked(claimTool);
const mockedCreate = vi.mocked(createTool);
const mockedDelete = vi.mocked(deleteTool);
const mockedSetEnabled = vi.mocked(setToolEnabled);
const mockedReindex = vi.mocked(reindexTools);
const mockedPreview = vi.mocked(previewToolSearch);

function makeTool(overrides: Partial<ContextForgeTool> = {}): ContextForgeTool {
  return {
    id: "tool-1",
    name: "gitlab_search",
    display_name: "GitLab Search",
    description: "搜索 GitLab 项目",
    url: "https://gitlab.example.com/api",
    enabled: true,
    reachable: true,
    integration_type: "REST",
    request_type: "SSE",
    tags: ["gitlab"],
    visibility: "public",
    created_at: "2026-11-01T00:00:00Z",
    updated_at: "2026-11-02T00:00:00Z",
    shadowed_by: null,
    index_status: "indexed",
    owned: true,
    claimable: false,
    can_edit: true,
    ...overrides,
  };
}

function makeStatus(overrides: Partial<ToolStatus> = {}): ToolStatus {
  return {
    enabled: true,
    reachable: true,
    auth_ok: true,
    admin_events: "connected",
    daily: "running",
    sync: "ready",
    index: "ready",
    indexed_tools: 12,
    total_tools: 13,
    last_sync_at: "2026-11-06T10:00:00Z",
    last_sync_reason: "admin",
    last_sync_duration_ms: 42,
    last_error: null,
    last_retry_at: null,
    last_retry_tool: null,
    last_retry_result: null,
    channels: { keyword: "ready", embedding: "ready", rerank: "degraded" },
    ...overrides,
  };
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ToolManagementPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockedList.mockResolvedValue({ tools: [makeTool()], count: 1, next_cursor: null });
  mockedStatus.mockResolvedValue(makeStatus());
  mockedClaim.mockResolvedValue({ ...makeTool(), headers: {}, input_schema: {}, annotations: {} });
  mockedCreate.mockResolvedValue({ ...makeTool({ id: "tool-2", name: "new_tool" }), headers: {}, input_schema: {}, annotations: {} });
  mockedDelete.mockResolvedValue(undefined);
  mockedSetEnabled.mockResolvedValue({ ...makeTool({ enabled: false }), headers: {}, input_schema: {}, annotations: {} });
  mockedReindex.mockResolvedValue({ scanned: 13, changed: 1, upserted: 1, deleted: 0, duration_ms: 12, error: null });
  mockedPreview.mockResolvedValue({
    query: "gitlab",
    results: [
      {
        tool_id: "tool-1",
        tool_name: "gitlab_search",
        call_name: "cf__gitlab_search",
        description: "搜索 GitLab 项目",
        tags: ["gitlab"],
        parameters: ["q", "per_page"],
        score: 0.9,
        schema_truncated: false,
      },
    ],
    retrieval: {
      index: "milvus",
      channels: ["embedding", "keyword"],
      candidates: 3,
      reranked: false,
      warnings: ["reranker unavailable"],
    },
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("工具管理页", () => {
  it("普通用户也能打开工具管理并加载列表", async () => {
    renderPage();

    expect(await screen.findByRole("heading", { name: "工具管理" })).toBeTruthy();
    expect(await screen.findByRole("article", { name: "gitlab_search" })).toBeTruthy();
    expect(mockedList).toHaveBeenCalledTimes(1);
  });

  it("加载中显示骨架，空列表给出创建入口", async () => {
    mockedList.mockReturnValue(new Promise(() => undefined));

    renderPage();

    expect(screen.getByLabelText("加载工具列表")).toBeTruthy();
  });

  it("列表为空时提示还没有工具", async () => {
    mockedList.mockResolvedValue({ tools: [], count: 0, next_cursor: null });

    renderPage();

    expect(await screen.findByText("还没有自己的工具")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "新建工具" }).length).toBeGreaterThan(0);
  });

  it("请求失败显示错误信息", async () => {
    mockedList.mockRejectedValue(new Error("ContextForge 不可达"));

    renderPage();

    expect((await screen.findByRole("alert")).textContent).toContain("ContextForge 不可达");
  });

  it("状态栏渲染连接、管理事件、同步、索引、通道与计数", async () => {
    renderPage();

    const status = await screen.findByRole("region", { name: "工具状态" });
    expect(await within(status).findByText("ContextForge 已启用")).toBeTruthy();
    expect(within(status).getByText("管理事件已连接")).toBeTruthy();
    expect(within(status).getByText("同步 ready")).toBeTruthy();
    expect(within(status).getByText("索引 ready")).toBeTruthy();
    expect(within(status).getByText("关键词 ready")).toBeTruthy();
    expect(within(status).getByText("向量 ready")).toBeTruthy();
    expect(within(status).getByText("重排 degraded")).toBeTruthy();
    expect(within(status).getByText("12 / 13 已索引")).toBeTruthy();
  });

  it("状态栏渲染每日兜底同步、同步耗时与上次重试", async () => {
    mockedStatus.mockResolvedValue(
      makeStatus({
        daily: "stopped",
        sync: "syncing",
        index: "degraded",
        last_sync_duration_ms: 137,
        last_retry_at: "2026-11-06T11:00:00Z",
        last_retry_tool: "gitlab_search",
        last_retry_result: "refreshed",
      }),
    );

    renderPage();

    const status = await screen.findByRole("region", { name: "工具状态" });
    expect(await within(status).findByText("每日同步 stopped")).toBeTruthy();
    expect(within(status).getByText("同步 syncing")).toBeTruthy();
    expect(within(status).getByText("索引 degraded")).toBeTruthy();
    expect(within(status).getByText("耗时 137 ms")).toBeTruthy();
    expect(within(status).getByText("上次重试 gitlab_search（refreshed）")).toBeTruthy();
  });

  it("没有重试记录时不渲染重试行", async () => {
    renderPage();

    const status = await screen.findByRole("region", { name: "工具状态" });
    await within(status).findByText("同步 ready");
    expect(within(status).queryByText(/上次重试/)).toBeNull();
  });

  it("工具行显示启用、可达、索引与遮蔽状态", async () => {
    mockedList.mockResolvedValue({
      tools: [
        makeTool({ shadowed_by: "gitlab_v2", reachable: false, index_status: "stale" }),
      ],
      count: 1,
      next_cursor: null,
    });

    renderPage();

    const row = await screen.findByRole("article", { name: "gitlab_search" });
    expect(within(row).getByText("已启用")).toBeTruthy();
    expect(within(row).getByText("不可达")).toBeTruthy();
    expect(within(row).getByText("索引 stale")).toBeTruthy();
    expect(within(row).getByText("被 gitlab_v2 遮蔽")).toBeTruthy();
  });

  it("新建工具打开编辑器，保存后刷新列表", async () => {
    renderPage();

    await screen.findByRole("article", { name: "gitlab_search" });
    fireEvent.click(screen.getAllByRole("button", { name: "新建工具" })[0]!);

    const dialog = await screen.findByRole("dialog", { name: "新建工具" });
    fireEvent.change(within(dialog).getByLabelText("工具名称"), {
      target: { value: "new_tool" },
    });
    fireEvent.change(within(dialog).getByLabelText("描述"), {
      target: { value: "新工具" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "创建" }));

    await waitFor(() => expect(mockedCreate).toHaveBeenCalledTimes(1));
    expect(mockedCreate.mock.calls[0]?.[0]).toMatchObject({
      name: "new_tool",
      description: "新工具",
    });
    await waitFor(() => expect(mockedList.mock.calls.length).toBeGreaterThan(1));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "新建工具" })).toBeNull());
  });

  it("未归属工具可以认领", async () => {
    mockedList.mockResolvedValue({
      tools: [makeTool({ owned: false, claimable: true, can_edit: false })],
      count: 1,
      next_cursor: null,
    });

    renderPage();

    const row = await screen.findByRole("article", { name: "gitlab_search" });
    expect(within(row).getByText("未归属")).toBeTruthy();
    fireEvent.click(within(row).getByRole("button", { name: "认领工具" }));

    await waitFor(() => expect(mockedClaim).toHaveBeenCalledWith("tool-1"));
  });

  it("启用开关调用 setToolEnabled", async () => {
    renderPage();

    const row = await screen.findByRole("article", { name: "gitlab_search" });
    const toggle = within(row).getByRole("switch", { name: "停用工具" });
    expect(toggle.getAttribute("aria-checked")).toBe("true");

    fireEvent.click(toggle);

    await waitFor(() => expect(mockedSetEnabled).toHaveBeenCalledWith("tool-1", false));
  });

  it("删除先确认，确认框里带工具名", async () => {
    renderPage();

    const row = await screen.findByRole("article", { name: "gitlab_search" });
    fireEvent.click(within(row).getByRole("button", { name: "删除工具" }));

    const dialog = await screen.findByRole("dialog", { name: "删除工具" });
    expect(within(dialog).getByText(/gitlab_search/)).toBeTruthy();
    expect(mockedDelete).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "确认删除" }));

    await waitFor(() => expect(mockedDelete).toHaveBeenCalledWith("tool-1"));
  });

  it("检索预览显示命中与警告", async () => {
    renderPage();

    await screen.findByRole("article", { name: "gitlab_search" });
    fireEvent.change(screen.getByLabelText("检索预览查询"), {
      target: { value: "gitlab" },
    });
    fireEvent.click(screen.getByRole("button", { name: "预览检索" }));

    await waitFor(() => expect(mockedPreview).toHaveBeenCalledWith("gitlab", 5));
    const preview = await screen.findByRole("region", { name: "检索预览结果" });
    expect(within(preview).getByText("gitlab_search")).toBeTruthy();
    // 命中卡带的是数据库行里的 call_name 与参数，模型看到的就是这两个名字。
    expect(within(preview).getByText("cf__gitlab_search")).toBeTruthy();
    expect(within(preview).getByText("参数 2")).toBeTruthy();
    expect(within(preview).getByText("reranker unavailable")).toBeTruthy();
  });

  it("重建索引调用 reindexTools 并显示结果", async () => {
    renderPage();

    await screen.findByRole("article", { name: "gitlab_search" });
    fireEvent.click(screen.getByRole("button", { name: "重建索引" }));

    await waitFor(() => expect(mockedReindex).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/扫描 13/)).toBeTruthy();
  });
});
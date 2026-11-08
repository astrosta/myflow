import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  McpServerApiError,
  createMcpServer,
  getMcpServer,
  updateMcpServer,
  type McpServerDetail,
} from "@/core/api/mcp-servers";

import { McpServerEditorDialog } from "./mcp-server-editor-dialog";

/**
 * 新建/编辑 MCP 服务。
 *
 * 三条约束直接来自网关契约，也正好是最容易写坏的地方：
 * - name 创建后不可改（编辑时只读），enabled 走 /state（PUT 不带它）；
 * - headers 只有掩码 ****，提交必须原样发回；
 * - URL 的查询串不回传，所以 url_has_query 时要提示"不写查询串会沿用已保存的那段"。
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

const mockedCreate = vi.mocked(createMcpServer);
const mockedGet = vi.mocked(getMcpServer);
const mockedUpdate = vi.mocked(updateMcpServer);

function makeDetail(overrides: Partial<McpServerDetail> = {}): McpServerDetail {
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
    headers: { Authorization: "****", "X-Anysearch-Client": "mcp/1.0.0" },
    ...overrides,
  };
}

function renderEditor(props: { serverId?: string | null; initialServer?: { name: string } | null } = {}) {
  const onClose = vi.fn();
  const onSaved = vi.fn();
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  const view = render(
    <QueryClientProvider client={client}>
      <McpServerEditorDialog
        initialServer={props.initialServer ?? null}
        onClose={onClose}
        onSaved={onSaved}
        serverId={props.serverId ?? null}
      />
    </QueryClientProvider>,
  );
  return { ...view, onClose, onSaved };
}

/**
 * 带触发按钮的 harness：焦点归还要能以"打开它的那个按钮"为锚点断言，
 * 所以打开/关闭状态必须由外层持有，而不是组件自己。
 */
function EditorHarness({ serverId = null }: { serverId?: string | null }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)} type="button">
        打开编辑器
      </button>
      {open ? (
        <McpServerEditorDialog
          onClose={() => setOpen(false)}
          onSaved={() => setOpen(false)}
          serverId={serverId}
        />
      ) : null}
    </>
  );
}

function renderHarness(serverId: string | null = null) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <EditorHarness serverId={serverId} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockedGet.mockResolvedValue(makeDetail());
  mockedCreate.mockResolvedValue(makeDetail({ id: "srv-2", name: "anysearch-2" }));
  mockedUpdate.mockResolvedValue(makeDetail());
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("新建 MCP 服务", () => {
  it("只提供 streamable_http 与 sse 两种传输，标签是 STREAMABLEHTTP / SSE", async () => {
    const view = renderEditor();

    const dialog = view.getByRole("dialog", { name: "新建 MCP 服务" });
    const transport = within(dialog).getByLabelText("传输方式") as HTMLSelectElement;
    const options = Array.from(transport.options).map((option) => ({
      label: option.textContent,
      value: option.value,
    }));

    expect(options).toEqual([
      { label: "STREAMABLEHTTP", value: "streamable_http" },
      { label: "SSE", value: "sse" },
    ]);
    // 新建时不存在"详情"请求
    expect(mockedGet).not.toHaveBeenCalled();
  });

  it("填完必填项就能创建，transport/headers/verify_tls/enabled 都带上", async () => {
    const view = renderEditor();
    const dialog = view.getByRole("dialog", { name: "新建 MCP 服务" });

    fireEvent.change(within(dialog).getByLabelText("服务名称"), {
      target: { value: "anysearch" },
    });
    fireEvent.change(within(dialog).getByLabelText("服务地址"), {
      target: { value: "https://api.anysearch.com/mcp" },
    });
    fireEvent.change(within(dialog).getByLabelText("传输方式"), { target: { value: "sse" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "添加请求头" }));
    fireEvent.change(within(dialog).getByLabelText("请求头名称 1"), {
      target: { value: "Authorization" },
    });
    fireEvent.change(within(dialog).getByLabelText("请求头值 1"), {
      target: { value: "Bearer abc" },
    });
    fireEvent.click(within(dialog).getByLabelText("校验 TLS 证书"));

    fireEvent.click(within(dialog).getByRole("button", { name: "创建" }));

    await waitFor(() => expect(mockedCreate).toHaveBeenCalledTimes(1));
    expect(mockedCreate).toHaveBeenCalledWith({
      name: "anysearch",
      display_name: null,
      description: "",
      url: "https://api.anysearch.com/mcp",
      transport: "sse",
      headers: { Authorization: "Bearer abc" },
      verify_tls: false,
      enabled: true,
    });
    await waitFor(() => expect(view.onSaved).toHaveBeenCalledTimes(1));
  });

  it("必填项缺失时留在本地拦住，不发请求", async () => {
    const view = renderEditor();
    const dialog = view.getByRole("dialog", { name: "新建 MCP 服务" });

    fireEvent.click(within(dialog).getByRole("button", { name: "创建" }));

    expect(await within(dialog).findByText("请填写服务名称")).toBeTruthy();
    expect(within(dialog).getByText("服务地址必须以 http:// 或 https:// 开头")).toBeTruthy();
    expect(mockedCreate).not.toHaveBeenCalled();
  });

  it("网关错误显示在表单里，不静默吞掉", async () => {
    mockedCreate.mockRejectedValueOnce(
      new McpServerApiError(422, "mcp_server_url_rejected", "url: 私网地址不允许", "trace-1"),
    );
    const view = renderEditor();
    const dialog = view.getByRole("dialog", { name: "新建 MCP 服务" });

    fireEvent.change(within(dialog).getByLabelText("服务名称"), { target: { value: "anysearch" } });
    fireEvent.change(within(dialog).getByLabelText("服务地址"), {
      target: { value: "https://127.0.0.1/mcp" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "创建" }));

    expect(await within(dialog).findByRole("alert")).toHaveProperty("textContent", "url: 私网地址不允许");
    expect(view.onSaved).not.toHaveBeenCalled();
  });
});

describe("编辑 MCP 服务", () => {
  it("服务名称只读，掩码请求头原样显示", async () => {
    const view = renderEditor({ serverId: "srv-1", initialServer: { name: "anysearch" } });

    const dialog = await view.findByRole("dialog", { name: "编辑 MCP 服务" });
    const name = (await within(dialog).findByLabelText("服务名称")) as HTMLInputElement;
    expect(name.value).toBe("anysearch");
    expect(name.disabled).toBe(true);
    expect((within(dialog).getByLabelText("请求头值 1") as HTMLInputElement).value).toBe("****");
    expect(within(dialog).getByLabelText("显示名称")).toHaveProperty("value", "AnySearch");
  });

  it("保存时把 **** 原样发回，且不带 name/enabled", async () => {
    const view = renderEditor({ serverId: "srv-1", initialServer: { name: "anysearch" } });
    const dialog = await view.findByRole("dialog", { name: "编辑 MCP 服务" });
    await within(dialog).findByLabelText("请求头值 1");

    fireEvent.change(within(dialog).getByLabelText("显示名称"), {
      target: { value: "AnySearch 2" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }));

    await waitFor(() => expect(mockedUpdate).toHaveBeenCalledTimes(1));
    const [serverId, input] = mockedUpdate.mock.calls[0] as [string, Record<string, unknown>];
    expect(serverId).toBe("srv-1");
    expect(input).toEqual({
      display_name: "AnySearch 2",
      description: "实时搜索",
      url: "https://api.anysearch.com/mcp",
      transport: "streamable_http",
      headers: { Authorization: "****", "X-Anysearch-Client": "mcp/1.0.0" },
      verify_tls: true,
      // 没勾选时不删查询串；只有勾上才会发 clear_query: true（见下面的用例）。
      clear_query: false,
    });
    await waitFor(() => expect(view.onSaved).toHaveBeenCalledTimes(1));
  });

  it("URL 带着被隐藏的查询串时给出提示：同源沿用、换主机自动丢弃", async () => {
    mockedGet.mockResolvedValue(makeDetail({ url_has_query: true }));
    const view = renderEditor({ serverId: "srv-1", initialServer: { name: "anysearch" } });

    const dialog = await view.findByRole("dialog", { name: "编辑 MCP 服务" });
    const hint = await within(dialog).findByText(/沿用已保存的那段/);

    expect(hint.textContent).toContain("主机");
    expect(hint.textContent).toContain("不会把旧凭据发到新地址");
  });

  it("勾选“删除已保存的查询串”后保存会发 clear_query: true", async () => {
    mockedGet.mockResolvedValue(makeDetail({ url_has_query: true }));
    const view = renderEditor({ serverId: "srv-1", initialServer: { name: "anysearch" } });

    const dialog = await view.findByRole("dialog", { name: "编辑 MCP 服务" });
    const toggle = await within(dialog).findByRole("checkbox", {
      name: /删除已保存的查询串/,
    });
    expect((toggle as HTMLInputElement).checked).toBe(false);
    fireEvent.click(toggle);
    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }));

    await waitFor(() => expect(mockedUpdate).toHaveBeenCalledTimes(1));
    const [, input] = mockedUpdate.mock.calls[0] as [string, Record<string, unknown>];
    expect(input.clear_query).toBe(true);
    expect(input.url).toBe("https://api.anysearch.com/mcp");
  });

  it("没有查询串的普通 URL 不显示那条提示", async () => {
    const view = renderEditor({ serverId: "srv-1", initialServer: { name: "anysearch" } });
    const dialog = await view.findByRole("dialog", { name: "编辑 MCP 服务" });
    await within(dialog).findByLabelText("服务名称");

    expect(within(dialog).queryByText(/查询串/)).toBeNull();
  });
});
describe("对话框焦点", () => {
  it("打开时焦点落在第一个该填的字段（新建是服务名称）", async () => {
    const view = renderHarness();
    const trigger = view.getByRole("button", { name: "打开编辑器" });
    trigger.focus();
    fireEvent.click(trigger);

    const dialog = await view.findByRole("dialog", { name: "新建 MCP 服务" });
    expect(document.activeElement).toBe(within(dialog).getByLabelText("服务名称"));
  });

  it("编辑时焦点落在服务地址（名称只读，聚焦它没有意义）", async () => {
    const view = renderHarness("srv-1");
    fireEvent.click(view.getByRole("button", { name: "打开编辑器" }));
    const dialog = await view.findByRole("dialog", { name: "编辑 MCP 服务" });
    await within(dialog).findByLabelText("服务地址");

    await waitFor(() =>
      expect(document.activeElement).toBe(within(dialog).getByLabelText("服务地址")),
    );
  });

  it("Tab 在框内循环，Escape 关闭，焦点回到触发按钮", async () => {
    const view = renderHarness();
    const trigger = view.getByRole("button", { name: "打开编辑器" });
    trigger.focus();
    fireEvent.click(trigger);

    const dialog = await view.findByRole("dialog", { name: "新建 MCP 服务" });
    const close = within(dialog).getByRole("button", { name: "关闭" });
    const submit = within(dialog).getByRole("button", { name: "创建" });

    // 第一个元素上 Shift+Tab 回卷到最后一个
    close.focus();
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(submit);

    // 最后一个元素上 Tab 回卷到第一个：焦点不会跑到背后的页面上
    fireEvent.keyDown(document, { key: "Tab" });
    expect(document.activeElement).toBe(close);

    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(view.queryByRole("dialog", { name: "新建 MCP 服务" })).toBeNull());
    expect(document.activeElement).toBe(trigger);
  });
});

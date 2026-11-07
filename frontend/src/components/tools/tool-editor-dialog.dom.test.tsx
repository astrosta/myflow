import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createTool,
  getTool,
  ToolApiError,
  updateTool,
  type ContextForgeToolDetail,
} from "@/core/api/tools";

import { ToolEditorDialog } from "./tool-editor-dialog";

vi.mock("@/core/api/tools", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/core/api/tools")>();
  return {
    ...actual,
    getTool: vi.fn(),
    createTool: vi.fn(),
    updateTool: vi.fn(),
  };
});

const mockedGet = vi.mocked(getTool);
const mockedCreate = vi.mocked(createTool);
const mockedUpdate = vi.mocked(updateTool);

const DETAIL: ContextForgeToolDetail = {
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
  headers: { Authorization: "****", "X-Team": "platform" },
  input_schema: { type: "object", properties: { q: { type: "string" } } },
  annotations: { readOnlyHint: true },
};

function renderDialog(ui: ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

function fillRequired(dialog: HTMLElement, name = "new_tool", description = "新工具") {
  fireEvent.change(within(dialog).getByLabelText("工具名称"), { target: { value: name } });
  fireEvent.change(within(dialog).getByLabelText("描述"), { target: { value: description } });
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("工具编辑器", () => {
  it("名称和描述必填，缺字段不发请求", () => {
    const onSaved = vi.fn();
    renderDialog(<ToolEditorDialog onClose={vi.fn()} onSaved={onSaved} />);

    const dialog = screen.getByRole("dialog", { name: "新建工具" });
    fireEvent.click(within(dialog).getByRole("button", { name: "创建" }));

    expect(within(dialog).getByText("请填写工具名称")).toBeTruthy();
    expect(within(dialog).getByText("请填写描述")).toBeTruthy();
    expect(mockedCreate).not.toHaveBeenCalled();
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("Schema 不是合法 JSON 时在字段旁报错且不发请求", () => {
    renderDialog(<ToolEditorDialog onClose={vi.fn()} onSaved={vi.fn()} />);

    const dialog = screen.getByRole("dialog", { name: "新建工具" });
    fillRequired(dialog);
    fireEvent.change(within(dialog).getByLabelText("输入 Schema（JSON）"), {
      target: { value: "{not json" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "创建" }));

    expect(within(dialog).getByText("输入 Schema 不是合法 JSON")).toBeTruthy();
    expect(mockedCreate).not.toHaveBeenCalled();
  });

  it("编辑时预填详情与掩码 header", async () => {
    mockedGet.mockResolvedValue(DETAIL);
    renderDialog(<ToolEditorDialog onClose={vi.fn()} onSaved={vi.fn()} toolId="tool-1" />);

    expect(await screen.findByDisplayValue("gitlab_search")).toBeTruthy();
    expect(screen.getByLabelText("描述")).toHaveProperty("value", "搜索 GitLab 项目");
    expect(screen.getByLabelText("工具地址")).toHaveProperty(
      "value",
      "https://gitlab.example.com/api",
    );
    expect(screen.getByLabelText("请求头值 1")).toHaveProperty("value", "****");
    expect(screen.getByLabelText("请求头值 2")).toHaveProperty("value", "platform");
    expect(screen.getByLabelText("输入 Schema（JSON）")).toHaveProperty(
      "value",
      JSON.stringify(DETAIL.input_schema, null, 2),
    );
  });

  it("提交时把网关返回的 **** 原样发回", async () => {
    mockedGet.mockResolvedValue(DETAIL);
    mockedUpdate.mockResolvedValue(DETAIL);
    const onSaved = vi.fn();
    renderDialog(
      <ToolEditorDialog onClose={vi.fn()} onSaved={onSaved} toolId="tool-1" />,
    );

    const dialog = await screen.findByRole("dialog", { name: "编辑工具" });
    await within(dialog).findByDisplayValue("gitlab_search");
    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }));

    await waitFor(() => expect(mockedUpdate).toHaveBeenCalledTimes(1));
    expect(mockedUpdate.mock.calls[0]?.[0]).toBe("tool-1");
    expect(mockedUpdate.mock.calls[0]?.[1].headers).toEqual({
      Authorization: "****",
      "X-Team": "platform",
    });
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(DETAIL));
  });

  it("422 的 errors[] 被映射到具体字段", async () => {
    mockedGet.mockResolvedValue(DETAIL);
    mockedUpdate.mockRejectedValue(
      new ToolApiError(
        422,
        "request_validation_error",
        "input_schema: must be an object",
        "trace-1",
      ),
    );
    renderDialog(<ToolEditorDialog onClose={vi.fn()} onSaved={vi.fn()} toolId="tool-1" />);

    const dialog = await screen.findByRole("dialog", { name: "编辑工具" });
    await within(dialog).findByDisplayValue("gitlab_search");
    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }));

    expect(await within(dialog).findByText("must be an object")).toBeTruthy();
    expect(within(dialog).getByRole("alert").textContent).toContain(
      "input_schema: must be an object",
    );
  });

  it("新建成功后把详情交回上层", async () => {
    mockedCreate.mockResolvedValue({ ...DETAIL, id: "tool-2", name: "new_tool" });
    const onSaved = vi.fn();
    renderDialog(<ToolEditorDialog onClose={vi.fn()} onSaved={onSaved} />);

    const dialog = screen.getByRole("dialog", { name: "新建工具" });
    fillRequired(dialog);
    fireEvent.click(within(dialog).getByRole("button", { name: "创建" }));

    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(mockedCreate.mock.calls[0]?.[0]).toMatchObject({
      name: "new_tool",
      description: "新工具",
      integration_type: "REST",
      enabled: true,
    });
  });
});

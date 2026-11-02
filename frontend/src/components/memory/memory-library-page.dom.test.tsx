import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  deleteMemoryItem,
  getMemoryContent,
  getMemoryDetail,
  listMemoryItems,
  listMemoryTypes,
  saveMemoryContent,
  type MemoryContentView,
  type MemoryDetailView,
  type MemoryItemView,
} from "@/core/api/memory-items";
import { MemoryApiError } from "@/core/api/memory";

import { MemoryLibraryPage } from "./memory-library-page";

vi.mock("@/core/api/memory-items", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/core/api/memory-items")>();
  return {
    ...actual,
    listMemoryTypes: vi.fn(),
    listMemoryItems: vi.fn(),
    getMemoryDetail: vi.fn(),
    getMemoryContent: vi.fn(),
    saveMemoryContent: vi.fn(),
    deleteMemoryItem: vi.fn(),
  };
});

const mockedTypes = vi.mocked(listMemoryTypes);
const mockedList = vi.mocked(listMemoryItems);
const mockedDetail = vi.mocked(getMemoryDetail);
const mockedContent = vi.mocked(getMemoryContent);
const mockedSave = vi.mocked(saveMemoryContent);
const mockedDelete = vi.mocked(deleteMemoryItem);

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

const FOLDER = "viking://user/u1/memories/preferences/alice";
const STYLE_URI = FOLDER + "/style.md";
const REPLY_URI = FOLDER + "/reply.md";

function makeItem(uri: string, name: string, abstract: string): MemoryItemView {
  return {
    uri,
    name,
    folder: FOLDER,
    memory_type: "preferences",
    abstract,
    abstract_truncated: false,
    updated_at: "2026-11-02T00:00:00Z",
  };
}

function makeTypes() {
  mockedTypes.mockResolvedValue({
    items: [
      { type: "preferences", label: "偏好" },
      { type: "entities", label: "实体" },
      { type: "events", label: "事件" },
      { type: "cases", label: "案例" },
      { type: "trajectories", label: "轨迹" },
      { type: "experiences", label: "经验" },
    ],
  });
}

function makeDetail(): MemoryDetailView {
  return {
    uri: STYLE_URI,
    folder: FOLDER,
    memory_type: "preferences",
    name: "style.md",
    overview: "# Preferences Overview\n- [Style](./style.md)",
    toc: [
      { kind: "heading", title: "Preferences Overview", depth: 1, uri: null, navigable: false },
      { kind: "link", title: "style", depth: 2, uri: STYLE_URI, navigable: true },
      { kind: "link", title: "reply", depth: 2, uri: REPLY_URI, navigable: true },
    ],
    overview_missing: false,
  };
}

function makeContent(uri: string, body: string, sha = "a".repeat(64)): MemoryContentView {
  return { uri, content: body, sha256: sha, size_bytes: body.length };
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryLibraryPage />
    </QueryClientProvider>,
  );
}

describe("记忆管理页", () => {
  it("按类型展示摘要，详情用 overview 渲染目录，点击目录项读取正文", async () => {
    makeTypes();
    mockedList.mockResolvedValue({
      type: "preferences",
      items: [makeItem(STYLE_URI, "style.md", "偏好简洁，先给结论。")],
      has_more: false,
      limit: 50,
    });
    mockedDetail.mockResolvedValue(makeDetail());
    mockedContent.mockImplementation(async (uri) =>
      makeContent(uri, uri === STYLE_URI ? "偏好先给结论。" : "回复要短。"),
    );

    renderPage();

    // 六类中文标签都在筛选栏里
    expect(await screen.findByRole("button", { name: "偏好" })).toBeTruthy();
    for (const label of ["实体", "事件", "案例", "轨迹", "经验"]) {
      expect(screen.getByRole("button", { name: label })).toBeTruthy();
    }

    // 列表只显示摘要
    const summary = await screen.findByText("偏好简洁，先给结论。");
    expect(summary).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /style/ }));

    // 详情目录来自 .overview.md
    const detail = await screen.findByRole("region", { name: "记忆详情" });
    expect(await within(detail).findByText("Preferences Overview")).toBeTruthy();
    expect(await within(detail).findByText("偏好先给结论。")).toBeTruthy();

    // 点击目录里的另一条记忆 → 读取它的完整内容
    fireEvent.click(within(detail).getByRole("button", { name: "reply" }));
    expect(await within(detail).findByText("回复要短。")).toBeTruthy();
    expect(mockedContent).toHaveBeenCalledWith(REPLY_URI);

    // 切换类型会重新拉列表
    fireEvent.click(screen.getByRole("button", { name: "实体" }));
    await waitFor(() => expect(mockedList).toHaveBeenCalledWith("entities"));
  });

  it("可以编辑保存并删除具体记忆", async () => {
    makeTypes();
    mockedList.mockResolvedValue({
      type: "preferences",
      items: [makeItem(STYLE_URI, "style.md", "偏好简洁")],
      has_more: false,
      limit: 50,
    });
    mockedDetail.mockResolvedValue(makeDetail());
    mockedContent.mockResolvedValue(makeContent(STYLE_URI, "偏好先给结论。"));
    mockedSave.mockResolvedValue(makeContent(STYLE_URI, "偏好结论优先。", "b".repeat(64)));
    mockedDelete.mockResolvedValue();
    const confirm = vi.fn(() => true);
    vi.stubGlobal("confirm", confirm);

    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /style/ }));
    await screen.findByText("偏好先给结论。");

    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    const editor = screen.getByLabelText("记忆正文") as HTMLTextAreaElement;
    expect(editor.value).toBe("偏好先给结论。");
    fireEvent.change(editor, { target: { value: "偏好结论优先。" } });
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));

    await waitFor(() =>
      expect(mockedSave).toHaveBeenCalledWith(STYLE_URI, "偏好结论优先。", "a".repeat(64)),
    );
    expect(await screen.findByText("已保存；摘要会在 OpenViking 重新生成后刷新。")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    await waitFor(() => expect(mockedDelete).toHaveBeenCalledWith(STYLE_URI));
    expect(confirm).toHaveBeenCalled();
    expect(await screen.findByText("记忆已删除。")).toBeTruthy();
  });

  it("编辑与删除作用于右侧当前查看的那条记忆", async () => {
    makeTypes();
    mockedList.mockResolvedValue({
      type: "preferences",
      items: [makeItem(STYLE_URI, "style.md", "偏好简洁")],
      has_more: false,
      limit: 50,
    });
    mockedDetail.mockResolvedValue(makeDetail());
    mockedContent.mockImplementation(async (uri) =>
      makeContent(uri, uri === STYLE_URI ? "偏好先给结论。" : "回复要短。"),
    );
    mockedDelete.mockResolvedValue();
    vi.stubGlobal("confirm", vi.fn(() => true));

    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /style/ }));
    await screen.findByText("偏好先给结论。");
    fireEvent.click(screen.getByRole("button", { name: "reply" }));
    await screen.findByText("回复要短。");

    fireEvent.click(screen.getByRole("button", { name: "删除" }));

    await waitFor(() => expect(mockedDelete).toHaveBeenCalledWith(REPLY_URI));
  });

  it("长期记忆未启用时整页给出提示", async () => {
    mockedTypes.mockRejectedValue(
      new MemoryApiError(503, "memory_runtime_unavailable", "disabled"),
    );

    renderPage();

    expect(
      await screen.findByText("长期记忆未启用或暂不可用，请稍后重试。"),
    ).toBeTruthy();
    expect(screen.getByRole("heading", { name: "记忆管理" })).toBeTruthy();
  });
});

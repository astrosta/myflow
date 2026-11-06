import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  deleteMemoryItem,
  getMemoryContent,
  listMemoryItems,
  listMemoryTypes,
  saveMemoryContent,
  type MemoryContentView,
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
    getMemoryContent: vi.fn(),
    saveMemoryContent: vi.fn(),
    deleteMemoryItem: vi.fn(),
  };
});

const mockedTypes = vi.mocked(listMemoryTypes);
const mockedList = vi.mocked(listMemoryItems);
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
      { type: "tools", label: "工具" },
    ],
  });
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
  it("左侧只显示当前类型的记忆目录，右侧只显示选中记忆的标题和正文", async () => {
    makeTypes();
    mockedList.mockResolvedValue({
      type: "preferences",
      items: [
        makeItem(STYLE_URI, "style.md", "偏好简洁，先给结论。"),
        makeItem(REPLY_URI, "reply.md", "回复尽量简短。"),
      ],
      has_more: false,
      limit: 50,
    });
    mockedContent.mockImplementation(async (uri) =>
      makeContent(uri, uri === STYLE_URI ? "偏好先给结论。" : "回复要短。"),
    );

    renderPage();

    // 后端会把可管理的七类中文标签都放进筛选栏；skills 不在其中
    expect(await screen.findByRole("button", { name: "偏好" })).toBeTruthy();
    for (const label of ["实体", "事件", "案例", "轨迹", "经验", "工具"]) {
      expect(screen.getByRole("button", { name: label })).toBeTruthy();
    }

    const directory = await screen.findByRole("region", { name: "记忆目录" });
    const content = screen.getByRole("region", { name: "记忆内容" });
    expect(
      directory.compareDocumentPosition(content) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBe(Node.DOCUMENT_POSITION_FOLLOWING);

    // 左侧只列当前类型的记忆目录，不展示 abstract 或 overview 卡片
    expect(await within(directory).findByText("style")).toBeTruthy();
    expect(within(directory).getByText("reply")).toBeTruthy();
    for (const abstract of ["偏好简洁，先给结论。", "回复尽量简短。"]) {
      expect(within(directory).queryByText(abstract)).toBeNull();
    }
    expect(within(directory).queryByText("Preferences Overview")).toBeNull();
    expect(screen.queryByRole("heading", { name: "目录" })).toBeNull();

    fireEvent.click(within(directory).getByRole("button", { name: /style/ }));
    expect(within(content).getByRole("heading", { name: "style" })).toBeTruthy();
    expect(await within(content).findByText("偏好先给结论。")).toBeTruthy();
    // 右侧不展示类型/路径等元数据，也没有目录项
    expect(within(content).queryByText(/viking:/)).toBeNull();
    expect(within(content).queryByText("reply")).toBeNull();

    // 点击左侧另一条记忆 → 右侧读取它的完整内容
    fireEvent.click(within(directory).getByRole("button", { name: /reply/ }));
    expect(await within(content).findByText("回复要短。")).toBeTruthy();
    expect(within(content).getByRole("heading", { name: "reply" })).toBeTruthy();
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
      items: [
        makeItem(STYLE_URI, "style.md", "偏好简洁"),
        makeItem(REPLY_URI, "reply.md", "回复简短"),
      ],
      has_more: false,
      limit: 50,
    });
    mockedContent.mockImplementation(async (uri) =>
      makeContent(uri, uri === STYLE_URI ? "偏好先给结论。" : "回复要短。"),
    );
    mockedDelete.mockResolvedValue();
    vi.stubGlobal("confirm", vi.fn(() => true));

    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /style/ }));
    await screen.findByText("偏好先给结论。");
    fireEvent.click(screen.getByRole("button", { name: /reply/ }));
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

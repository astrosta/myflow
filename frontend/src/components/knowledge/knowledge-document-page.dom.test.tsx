import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { AnchorHTMLAttributes, ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  getKnowledgeBase,
  getKnowledgeDocument,
  getKnowledgeStatus,
  listKnowledgeDocumentChunks,
  type KnowledgeBase,
  type KnowledgeDocument,
  type KnowledgeStatus,
} from "@/core/api/knowledge";

import { KnowledgeDocumentPage } from "./knowledge-document-page";

vi.mock("next/link", () => ({
  default: ({
    href,
    children,
    ...rest
  }: AnchorHTMLAttributes<HTMLAnchorElement> & { href: string; children: ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

vi.mock("@/core/api/knowledge", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/core/api/knowledge")>();
  return {
    ...actual,
    getKnowledgeStatus: vi.fn(),
    getKnowledgeBase: vi.fn(),
    getKnowledgeDocument: vi.fn(),
    listKnowledgeDocumentChunks: vi.fn(),
  };
});

const mockedStatus = vi.mocked(getKnowledgeStatus);
const mockedBase = vi.mocked(getKnowledgeBase);
const mockedDocument = vi.mocked(getKnowledgeDocument);
const mockedChunks = vi.mocked(listKnowledgeDocumentChunks);

const BASE_ID = "base-1";
const DOCUMENT_ID = "doc-1";

function makeStatus(enabled = true): KnowledgeStatus {
  return { enabled } as KnowledgeStatus;
}

function makeBase(): KnowledgeBase {
  return {
    id: BASE_ID,
    name: "运维手册",
    type: "operations",
    description: null,
    document_count: 1,
    chunk_count: 2,
    created_at: "2026-10-30T00:00:00Z",
    updated_at: "2026-10-30T00:00:00Z",
  };
}

function makeDocument(status: KnowledgeDocument["status"] = "ready"): KnowledgeDocument {
  return {
    id: DOCUMENT_ID,
    knowledge_base_id: BASE_ID,
    filename: "runbook.md",
    mime_type: "text/markdown",
    size_bytes: 2048,
    status,
    chunk_count: 2,
    error_message: null,
    created_at: "2026-10-30T00:00:00Z",
    updated_at: "2026-10-30T01:00:00Z",
  };
}

function primeReadyData() {
  mockedStatus.mockResolvedValue(makeStatus());
  mockedBase.mockResolvedValue(makeBase());
  mockedDocument.mockResolvedValue(makeDocument());
  mockedChunks.mockResolvedValue({
    document_id: DOCUMENT_ID,
    count: 2,
    next_offset: null,
    chunks: [
      {
        chunk_id: DOCUMENT_ID + ":0",
        chunk_index: 0,
        section: "安装",
        page: 1,
        content: "## 安装步骤\n\n- 第一步",
      },
      {
        chunk_id: DOCUMENT_ID + ":1",
        chunk_index: 1,
        section: "回滚",
        page: 2,
        content: "执行回滚命令。",
        parent_id: DOCUMENT_ID + ":p0",
      },
    ],
  });
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <KnowledgeDocumentPage baseId={BASE_ID} documentId={DOCUMENT_ID} />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("知识库文档详情", () => {
  it("展示元数据和按 chunk_index 返回的 Markdown 切片", async () => {
    primeReadyData();

    renderPage();

    expect(await screen.findByRole("heading", { name: "runbook.md" })).toBeTruthy();
    expect(screen.getByText("已就绪")).toBeTruthy();
    expect(screen.getByText("所属知识库").closest("div")?.textContent).toContain("运维手册");
    expect(screen.getByText("知识库类型").closest("div")?.textContent).toContain("运维手册");
    expect(screen.getByText("2.0 KiB")).toBeTruthy();
    expect(screen.getByText("共 2 个分片")).toBeTruthy();
    expect(await screen.findByRole("heading", { name: "安装步骤", level: 2 })).toBeTruthy();
    expect(screen.getByText("切片 1")).toBeTruthy();
    expect(screen.getByText("安装")).toBeTruthy();
    expect(screen.getByText("第 1 页")).toBeTruthy();
    // 父块只显示归属标记，不在详情页展开父块全文，避免同一父块重复显示。
    expect(screen.getByText("父块 " + DOCUMENT_ID + ":p0")).toBeTruthy();
    expect(screen.getAllByText(/^父块 /)).toHaveLength(1);
    expect(mockedChunks).toHaveBeenCalledWith(BASE_ID, DOCUMENT_ID, 0, 20);
  });

  it("处理中只显示状态提示，不请求切片", async () => {
    mockedStatus.mockResolvedValue(makeStatus());
    mockedBase.mockResolvedValue(makeBase());
    mockedDocument.mockResolvedValue(makeDocument("embedding"));

    renderPage();

    expect(
      await screen.findByText("文档正在处理，完成后这里会自动展示切片内容。"),
    ).toBeTruthy();
    expect(mockedChunks).not.toHaveBeenCalled();
  });

  it("可以加载下一页切片", async () => {
    primeReadyData();
    mockedChunks
      .mockResolvedValueOnce({
        document_id: DOCUMENT_ID,
        count: 2,
        next_offset: 1,
        chunks: [
          {
            chunk_id: DOCUMENT_ID + ":0",
            chunk_index: 0,
            section: "安装",
            page: 1,
            content: "第一页切片",
          },
        ],
      })
      .mockResolvedValueOnce({
        document_id: DOCUMENT_ID,
        count: 2,
        next_offset: null,
        chunks: [
          {
            chunk_id: DOCUMENT_ID + ":1",
            chunk_index: 1,
            section: "回滚",
            page: 2,
            content: "第二页切片",
          },
        ],
      });

    renderPage();

    expect(await screen.findByText("第一页切片")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "加载更多" }));

    expect(await screen.findByText("第二页切片")).toBeTruthy();
    await waitFor(() =>
      expect(mockedChunks).toHaveBeenLastCalledWith(BASE_ID, DOCUMENT_ID, 1, 20),
    );
  });
});

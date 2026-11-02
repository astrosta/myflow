import { cleanup, render, screen } from "@testing-library/react";
import type { AnchorHTMLAttributes, ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { KnowledgeDocument } from "@/core/api/knowledge";

import { DocumentList } from "./document-list";

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

const document: KnowledgeDocument = {
  id: "doc-1",
  knowledge_base_id: "base-1",
  filename: "runbook.md",
  mime_type: "text/markdown",
  size_bytes: 128,
  status: "ready",
  chunk_count: 3,
  error_message: null,
  created_at: "2026-10-30T00:00:00Z",
  updated_at: "2026-10-30T01:00:00Z",
};

afterEach(cleanup);

describe("知识库文档列表", () => {
  it("点击文档名称进入该文档详情页", () => {
    render(
      <DocumentList documents={[document]} onReindex={vi.fn()} onDelete={vi.fn()} />,
    );

    const link = screen.getByRole("link", { name: "runbook.md" });
    expect(link.getAttribute("href")).toBe(
      "/workspace/knowledge/base-1/documents/doc-1",
    );
  });
});
